"""Export court cases with documents to denormalized JSON format.

This module provides utilities to export complete case information from the
PostgreSQL database, download associated files from Google Cloud Storage, and
create denormalized JSON files for easy consumption.
"""

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pandas as pd
from google.cloud import storage
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tqdm import tqdm

from lawsuit_parser.utils.db import fetch_from_postgres
from lawsuit_parser.utils.gcs import extract_blob_name

logger = logging.getLogger(__name__)

# Port of the "scrapping" Cloud SQL instance (court case crawl data) that
# CaseExporter always targets, regardless of state - see load_db_config /
# fetch_from_postgres in db.py.
SCRAPPING_DB_PORT = 5433


def _pg_bigint_array(values: list[int]) -> str:
    """Render ints as a Postgres ``ARRAY[...]::bigint[]`` literal.

    fetch_from_postgres caches results by hashing the literal query text, so
    embedding the id list directly (rather than a bound parameter) is what
    lets a repeated/resumed bulk export reuse a prior query's cached result
    instead of re-hitting the DB. Values are cast to int first, so this is
    safe to embed even though it's string formatting.
    """
    return "ARRAY[" + ",".join(str(int(v)) for v in values) + "]::bigint[]"


class CaseExporter:
    """Export court cases with all related data and files."""

    def __init__(
        self,
        engine: Engine,
        output_dir: Path | str,
        gcs_bucket_name: str = "courts_crawl",
        schema: str = "courts_final",
        table_prefix: str = "ny_",
        extract_text: bool = False,
        use_gpu: bool = True,
        download_files: bool = False,
        extraction_root: Path | str = "data/extraction",
    ):
        """Initialize the case exporter.

        Args:
            engine: SQLAlchemy engine connected to the scrapping database (port 5433).
            output_dir: Directory where case JSON files and PDFs will be saved.
            gcs_bucket_name: GCS bucket name where documents are stored (default: courts_crawl).
            schema: Postgres schema holding the crawl tables (default: courts_final,
                where the former ``public.court_cases``/``public.court_documents``
                data now lives).
            table_prefix: Per-state table prefix, e.g. "ny_" for
                ``ny_cases_after_search``/``ny_docket_documents``, "fl_" for the
                Florida tables, etc. Pass "" for un-prefixed tables.
            extract_text: If True, run Docling over every downloaded PDF and
                save its full structured output under extraction_root (see
                ``_extract_case_text``). Off by default: it's a slow, GPU/CPU-
                heavy extra step most callers don't need just to get the
                PDFs + DB metadata. Requires download_files=True (see below).
            use_gpu: Whether Docling should use GPU acceleration when
                ``extract_text=True``. Ignored otherwise.
            download_files: If False (the default), skip downloading
                PDFs/confirmations from GCS entirely - only the DB-sourced
                JSON metadata is written (no ``documents/``/``confirmations/``
                files, no PDF metadata via pdfinfo, no Docling text
                extraction regardless of ``extract_text``). Much faster for
                building a metadata-only sample (e.g. training-data
                selection) before committing to the slower file-download
                pass. Pass True to also download files.
            extraction_root: Root directory to write Docling output under
                when extract_text=True - mirrors the event-extraction
                pipeline's own data_root ("data/cases")/output_root
                ("data/extraction") split (see get_docling_dir). Each
                case's output goes under
                extraction_root/case_<id>/docling/, exactly parallel to its
                case_<id> directory under output_dir - if output_dir nests
                under a source subfolder (e.g. data/cases/ny_sample),
                extraction_root should carry the same subfolder (e.g.
                data/extraction/ny_sample) to keep the two trees aligned;
                this is not derived automatically. Ignored unless
                extract_text=True.
        """
        self.engine = engine
        self.output_dir = Path(output_dir)
        self.gcs_bucket_name = gcs_bucket_name
        self.schema = schema
        self.table_prefix = table_prefix
        self.extract_text = extract_text
        self.use_gpu = use_gpu
        self.download_files = download_files
        self.extraction_root = Path(extraction_root)
        if extract_text and not download_files:
            logger.warning(
                "extract_text=True has no effect when download_files=False "
                "(there are no local PDFs to extract text from)"
            )
        # Extract state code from table_prefix (e.g., "ny_" -> "ny")
        self.state_code = table_prefix.rstrip("_") if table_prefix else ""
        self.cases_table = f"{schema}.{table_prefix}cases_after_search"
        self.documents_table = f"{schema}.{table_prefix}docket_documents"
        # Historical archive of case snapshots (same shape as cases_table,
        # minus documents_scrapped_at) - not case-specific data on its own,
        # but every past snapshot of *this* case is relevant metadata that
        # cases_table alone doesn't carry (e.g. earlier case_status values).
        self.case_history_table = f"{schema}.{table_prefix}cases"
        # OCR transcriptions of document pages, linked from documents_table
        # via ocr_transcription_id. Currently empty for NY (OCR pipeline
        # not yet populating it) but the export should still surface any
        # rows that do exist rather than silently dropping them.
        self.transcriptions_table = f"{schema}.{table_prefix}docket_documents_transcriptions"
        self.storage_client = storage.Client()
        self.bucket = self.storage_client.bucket(gcs_bucket_name)

    def export_case_by_id(self, case_id: int, skip_if_exists: bool = True) -> tuple[Path, bool]:
        """Export a case by its database ID ({table_prefix}cases_after_search.id).

        Args:
            case_id: The integer ID from the cases table's ``id`` column.
            skip_if_exists: If True, skip export if the JSON file already exists
                (allows resuming interrupted exports). Default: True.

        Returns:
            Tuple of (path to JSON file, whether it was skipped).
        """
        # Check if case already exported
        case_dir = self.output_dir / f"case_{case_id}"
        json_path = case_dir / f"case_{case_id}.json"

        if skip_if_exists and json_path.exists():
            return json_path, True  # Skipped

        # Query case data
        case_query = text(f"""
            SELECT
                id,
                docket_id,
                query_link,
                case_id,
                case_link,
                case_received_date,
                efiling_status,
                case_status,
                caption,
                court,
                court_id,
                case_type,
                documents_scrapped_at,
                created_at,
                updated_at
            FROM {self.cases_table}
            WHERE id = :case_id
        """)

        with self.engine.connect() as conn:
            result = conn.execute(case_query, {"case_id": case_id})
            case_row = result.fetchone()

        if not case_row:
            raise ValueError(f"Case with id={case_id} not found")

        # Convert to dict
        case_data = dict(case_row._mapping)

        # Query documents for this case. Joined on docket_id, not case_id:
        # case_id (the human-readable docket number, e.g. "622075/2025") is
        # not unique across courts - two unrelated cases in different
        # counties can share the same case_id - so joining on it risks
        # pulling in another case's documents. docket_id is the actual
        # unique identifier the scraper assigns per case.
        docs_query = text(f"""
            SELECT
                id,
                docket_id,
                case_id,
                assigned_judge,
                document_doc_index,
                document_name,
                document_details,
                document_link,
                document_bucket_link,
                filed_by,
                filed_create,
                filed_received,
                document_status,
                document_confirmation_title,
                document_confirmation_link,
                document_confirmation_bucket_link,
                document_confirmation_link_id,
                ocr_created,
                ocr_transcription_id,
                created_at,
                updated_at
            FROM {self.documents_table}
            WHERE docket_id = :docket_id
            ORDER BY id
        """)

        with self.engine.connect() as conn:
            result = conn.execute(docs_query, {"docket_id": case_data["docket_id"]})
            docs_rows = result.fetchall()

        # Convert documents to list of dicts
        documents = [dict(row._mapping) for row in docs_rows]

        if not documents:
            raise ValueError(f"Case with id={case_id} has no documents, skipping export")

        # Historical snapshots of this same case (same docket_id) from the
        # crawl's archive table - e.g. earlier case_status/efiling_status
        # values recorded before the most recent scrape.
        case_history_query = text(f"""
            SELECT *
            FROM {self.case_history_table}
            WHERE docket_id = :docket_id
            ORDER BY created_at
        """)

        with self.engine.connect() as conn:
            result = conn.execute(case_history_query, {"docket_id": case_data["docket_id"]})
            case_history = [dict(row._mapping) for row in result.fetchall()]

        # OCR transcriptions of document pages (case_id here is the numeric
        # cases_table.id, not the text docket number - see
        # docs/court_tables_relationships.md). Grouped by case_file_id
        # (-> documents_table.id) and attached to each document below.
        transcriptions_query = text(f"""
            SELECT *
            FROM {self.transcriptions_table}
            WHERE case_id = :case_id
            ORDER BY case_file_id, page
        """)

        with self.engine.connect() as conn:
            result = conn.execute(transcriptions_query, {"case_id": case_id})
            transcription_rows = [dict(row._mapping) for row in result.fetchall()]

        transcriptions_by_doc: dict[int, list[dict[str, Any]]] = {}
        for row in transcription_rows:
            transcriptions_by_doc.setdefault(row["case_file_id"], []).append(row)

        # Ensure output directory exists (may already exist from skip check above)
        case_dir.mkdir(parents=True, exist_ok=True)

        # Download PDF files and extract their metadata (skipped entirely in
        # metadata-only mode - see download_files on __init__).
        pdf_metadata_by_doc: dict[int, dict[str, Any]] = {}
        text_paths_by_doc: dict[int, dict[str, str]] = {}
        if self.download_files:
            pdf_metadata_by_doc = self._download_case_files(documents, case_dir)

            # Optional Docling text extraction, run only once the PDFs are on
            # disk to extract from (see _extract_case_text and the
            # extract_text flag on __init__).
            if self.extract_text:
                text_paths_by_doc = self._extract_case_text(documents, case_dir)

        # Create denormalized structure
        denormalized_case = self._create_denormalized_json(
            case_data, documents, case_history, transcriptions_by_doc, text_paths_by_doc, pdf_metadata_by_doc
        )

        # Save JSON
        json_path = case_dir / f"case_{case_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(denormalized_case, f, indent=2, default=str)

        return json_path, False  # Successfully exported (not skipped)

    def export_cases_bulk(
        self,
        case_ids: list[int] | None = None,
        skip_if_exists: bool = True,
    ) -> dict[str, int]:
        """Export many cases using bulk queries instead of one round-trip per case.

        export_case_by_id issues 4 separate queries per case (case row,
        documents, history, transcriptions) - fine for a single case, but
        for a batch it means 4*N round trips. This instead pulls each of
        the 4 tables once (filtered to case_ids' rows when given, or every
        row otherwise), loads them into DataFrames, groups the related rows
        in memory (documents/history by docket_id, transcriptions by
        case_id), and only then loops over cases to write each one's JSON -
        4 queries total regardless of how many cases are exported.

        Args:
            case_ids: Case IDs to export (cases_table.id). If None, exports
                every case in the table - only sensible combined with a
                metadata-only exporter (download_files=False), since
                downloading files for an entire table is rarely intended.
            skip_if_exists: If True, skip a case whose JSON file already
                exists (allows resuming an interrupted batch). Default True.

        Returns:
            Dict of counts: total, successful, skipped, failed, no_documents.
        """
        cases_query = f"""
            SELECT
                id, docket_id, query_link, case_id, case_link,
                case_received_date, efiling_status, case_status, caption,
                court, court_id, case_type, documents_scrapped_at,
                created_at, updated_at
            FROM {self.cases_table}
            {"WHERE id = ANY(" + _pg_bigint_array(case_ids) + ")" if case_ids is not None else ""}
        """
        print(f"Fetching {len(case_ids) if case_ids is not None else 'all'} case rows (cached)...")
        cases_df = fetch_from_postgres(cases_query, port=SCRAPPING_DB_PORT)

        stats = {"total": len(cases_df), "successful": 0, "skipped": 0, "failed": 0, "no_documents": 0}
        if cases_df.empty:
            return stats

        docket_ids = cases_df["docket_id"].dropna().unique().tolist()
        all_case_ids = cases_df["id"].tolist()

        docs_query = f"""
            SELECT
                id, docket_id, case_id, assigned_judge, document_doc_index,
                document_name, document_details, document_link,
                document_bucket_link, filed_by, filed_create, filed_received,
                document_status, document_confirmation_title,
                document_confirmation_link, document_confirmation_bucket_link,
                document_confirmation_link_id, ocr_created,
                ocr_transcription_id, created_at, updated_at
            FROM {self.documents_table}
            WHERE docket_id = ANY({_pg_bigint_array(docket_ids)})
            ORDER BY id
        """
        history_query = f"""
            SELECT * FROM {self.case_history_table}
            WHERE docket_id = ANY({_pg_bigint_array(docket_ids)})
            ORDER BY created_at
        """
        transcriptions_query = f"""
            SELECT * FROM {self.transcriptions_table}
            WHERE case_id = ANY({_pg_bigint_array(all_case_ids)})
            ORDER BY case_file_id, page
        """
        print(f"Fetched {len(cases_df)} cases. Fetching documents for {len(docket_ids)} dockets (cached)...")
        docs_df = fetch_from_postgres(docs_query, port=SCRAPPING_DB_PORT)
        print(f"Fetched {len(docs_df)} documents. Fetching case history (cached)...")
        history_df = fetch_from_postgres(history_query, port=SCRAPPING_DB_PORT)
        print(f"Fetched {len(history_df)} history rows. Fetching transcriptions (cached)...")
        transcriptions_df = fetch_from_postgres(transcriptions_query, port=SCRAPPING_DB_PORT)
        print(f"Fetched {len(transcriptions_df)} transcription rows. Building per-case JSON...")

        docs_by_docket = {k: v for k, v in docs_df.groupby("docket_id")}
        history_by_docket = {k: v for k, v in history_df.groupby("docket_id")}
        transcriptions_by_case = {k: v for k, v in transcriptions_df.groupby("case_id")}

        for case_row in tqdm(cases_df.to_dict("records"), desc="Exporting cases", unit="case"):
            case_id = case_row["id"]
            docket_id = case_row["docket_id"]

            case_dir = self.output_dir / f"case_{case_id}"
            json_path = case_dir / f"case_{case_id}.json"
            if skip_if_exists and json_path.exists():
                stats["skipped"] += 1
                continue

            doc_group = docs_by_docket.get(docket_id)
            documents = doc_group.to_dict("records") if doc_group is not None else []
            if not documents:
                logger.warning(f"Case {case_id} has no documents, skipping")
                stats["no_documents"] += 1
                continue

            history_group = history_by_docket.get(docket_id)
            case_history = history_group.to_dict("records") if history_group is not None else []

            transcription_group = transcriptions_by_case.get(case_id)
            transcription_rows = (
                transcription_group.to_dict("records") if transcription_group is not None else []
            )
            transcriptions_by_doc: dict[int, list[dict[str, Any]]] = {}
            for row in transcription_rows:
                transcriptions_by_doc.setdefault(row["case_file_id"], []).append(row)

            try:
                case_dir.mkdir(parents=True, exist_ok=True)

                pdf_metadata_by_doc: dict[int, dict[str, Any]] = {}
                text_paths_by_doc: dict[int, dict[str, str]] = {}
                if self.download_files:
                    pdf_metadata_by_doc = self._download_case_files(documents, case_dir)
                    if self.extract_text:
                        text_paths_by_doc = self._extract_case_text(documents, case_dir)

                denormalized_case = self._create_denormalized_json(
                    case_row, documents, case_history, transcriptions_by_doc,
                    text_paths_by_doc, pdf_metadata_by_doc,
                )
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(denormalized_case, f, indent=2, default=str)
                stats["successful"] += 1
            except Exception as e:
                logger.warning(f"Failed to export case {case_id}: {e}")
                stats["failed"] += 1

        return stats

    def _create_denormalized_json(
        self,
        case_data: dict[str, Any],
        documents: list[dict[str, Any]],
        case_history: list[dict[str, Any]],
        transcriptions_by_doc: dict[int, list[dict[str, Any]]],
        text_paths_by_doc: dict[int, dict[str, str]],
        pdf_metadata_by_doc: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a denormalized JSON structure.

        Args:
            case_data: Dictionary containing case information.
            documents: List of dictionaries containing document information.
            case_history: Historical snapshots of this case from the crawl's
                archive table, oldest first.
            transcriptions_by_doc: OCR transcription rows keyed by the
                document id they belong to (case_file_id).
            text_paths_by_doc: Docling-extracted ``.txt`` paths (relative to
                the case directory) keyed by document id - see
                _extract_case_text. Empty when extract_text=False.
            pdf_metadata_by_doc: PDF file metadata (author, creation date, etc.)
                keyed by document id. Contains 'document_metadata' and
                'confirmation_metadata' when available.

        Returns:
            Denormalized dictionary ready for JSON serialization.
        """
        # Process documents to include local file paths, any OCR
        # transcriptions recorded for them, and any Docling-extracted text.
        processed_docs = []
        for doc in documents:
            processed_doc = doc.copy()

            # Add local file path references and the full gs:// URI (the
            # DB only stores a relative blob path, e.g.
            # "document_link/document_xyz.pdf" - the URI is what a
            # metadata-only export needs to download the file later).
            if doc.get("document_bucket_link"):
                filename = self._extract_filename_from_gcs_path(
                    doc["document_bucket_link"]
                )
                processed_doc["local_document_path"] = f"documents/{filename}"
                processed_doc["gcs_document_uri"] = self._to_gcs_uri(
                    doc["document_bucket_link"]
                )

            if doc.get("document_confirmation_bucket_link"):
                filename = self._extract_filename_from_gcs_path(
                    doc["document_confirmation_bucket_link"]
                )
                processed_doc["local_confirmation_path"] = f"confirmations/{filename}"
                processed_doc["gcs_confirmation_uri"] = self._to_gcs_uri(
                    doc["document_confirmation_bucket_link"]
                )

            processed_doc["transcriptions"] = transcriptions_by_doc.get(doc["id"], [])

            doc_text_paths = text_paths_by_doc.get(doc["id"], {})
            if doc_text_paths.get("document_text_path"):
                processed_doc["local_document_text_path"] = doc_text_paths["document_text_path"]
            if doc_text_paths.get("confirmation_text_path"):
                processed_doc["local_confirmation_text_path"] = doc_text_paths["confirmation_text_path"]

            # Add PDF metadata if available
            doc_pdf_metadata = pdf_metadata_by_doc.get(doc["id"], {})
            if doc_pdf_metadata.get("document_metadata"):
                processed_doc["pdf_metadata"] = doc_pdf_metadata["document_metadata"]
            if doc_pdf_metadata.get("confirmation_metadata"):
                processed_doc["confirmation_pdf_metadata"] = doc_pdf_metadata["confirmation_metadata"]

            processed_docs.append(processed_doc)

        # Create denormalized structure
        return {
            "case_info": case_data,
            "documents": processed_docs,
            "case_history": case_history,
            "summary": {
                "total_documents": len(documents),
                "case_id": case_data.get("case_id"),
                "docket_id": case_data.get("docket_id"),
                "caption": case_data.get("caption"),
                "court": case_data.get("court"),
                "case_status": case_data.get("case_status"),
                "total_history_snapshots": len(case_history),
                "files_downloaded": self.download_files,
                "text_extraction_enabled": self.extract_text,
                "exported_at": datetime.utcnow().isoformat(),
            },
        }

    def _download_case_files(self, documents: list[dict[str, Any]], case_dir: Path) -> dict[int, dict[str, Any]]:
        """Download all files for a case from GCS and extract PDF metadata.

        Args:
            documents: List of document dictionaries.
            case_dir: Directory where files should be saved.

        Returns:
            Dictionary keyed by document id, containing 'document_metadata'
            and 'confirmation_metadata' for each document's PDFs.
        """
        # Create subdirectories
        docs_dir = case_dir / "documents"
        confirm_dir = case_dir / "confirmations"
        docs_dir.mkdir(exist_ok=True)
        confirm_dir.mkdir(exist_ok=True)

        pdf_metadata_by_doc = {}

        for doc in documents:
            doc_metadata = {}

            # Download main document
            if doc.get("document_bucket_link"):
                gcs_path = doc["document_bucket_link"]
                filename = self._extract_filename_from_gcs_path(gcs_path)
                local_path = docs_dir / filename

                try:
                    self.download_from_gcs_to_file(gcs_path, local_path)
                    # Extract PDF metadata
                    metadata = self._extract_pdf_metadata(local_path)
                    if metadata:
                        doc_metadata["document_metadata"] = metadata
                except Exception as e:
                    logger.warning(f"Failed to download {gcs_path}: {e}")

            # Download confirmation document
            if doc.get("document_confirmation_bucket_link"):
                gcs_path = doc["document_confirmation_bucket_link"]
                filename = self._extract_filename_from_gcs_path(gcs_path)
                local_path = confirm_dir / filename

                try:
                    self.download_from_gcs_to_file(gcs_path, local_path)
                    # Extract PDF metadata
                    metadata = self._extract_pdf_metadata(local_path)
                    if metadata:
                        doc_metadata["confirmation_metadata"] = metadata
                except Exception as e:
                    logger.warning(f"Failed to download {gcs_path}: {e}")

            # Store metadata for this document if any was extracted
            if doc_metadata:
                pdf_metadata_by_doc[doc["id"]] = doc_metadata

        return pdf_metadata_by_doc

    def _extract_case_text(
        self, documents: list[dict[str, Any]], case_dir: Path
    ) -> dict[int, dict[str, str]]:
        """Extract a plain-text version of every downloaded PDF using Docling.

        For each document/confirmation PDF found on disk, saves a ``.txt``
        file with the same stem alongside it (e.g. ``documents/document_xyz.pdf``
        -> ``documents/document_xyz.txt``) - a 1:1 text counterpart for
        ML pipelines that just want to glob PDF/text pairs. Docling's full
        structured output (``.docling.json``/``.md``) is also saved, under
        ``extraction_root/case_<id>/docling/documents`` or
        ``.../docling/confirmations`` (see
        lawsuit_parser.parsers.batch.get_docling_dir) - the same layout the
        event-extraction pipeline expects, so a later run over this same
        case reuses these outputs instead of re-parsing.

        This is Docling's full layout/OCR pipeline, so it's slow and
        GPU/CPU-heavy - only called when extract_text=True (see __init__).

        Args:
            documents: Document rows (as returned by the documents query).
            case_dir: This case's output directory (PDFs already downloaded
                into case_dir/documents and case_dir/confirmations).

        Returns:
            Dict keyed by document id, each value holding whichever of
            "document_text_path"/"confirmation_text_path" (paths relative
            to case_dir) were successfully extracted.
        """
        from lawsuit_parser.parsers.batch import get_docling_dir
        from lawsuit_parser.parsers.pdf_parser import parse_pdf_document

        text_paths_by_doc: dict[int, dict[str, str]] = {}

        for doc in documents:
            doc_text_paths: dict[str, str] = {}

            for link_field, dir_name, path_key in (
                ("document_bucket_link", "documents", "document_text_path"),
                ("document_confirmation_bucket_link", "confirmations", "confirmation_text_path"),
            ):
                gcs_path = doc.get(link_field)
                if not gcs_path:
                    continue

                filename = self._extract_filename_from_gcs_path(gcs_path)
                pdf_path = case_dir / dir_name / filename
                if not pdf_path.exists():
                    continue  # download failed or was skipped - nothing to extract

                text_path = pdf_path.with_suffix(".txt")
                relative_text_path = f"{dir_name}/{text_path.name}"

                if text_path.exists():
                    doc_text_paths[path_key] = relative_text_path
                    continue

                try:
                    parsed = parse_pdf_document(
                        pdf_path,
                        use_gpu=self.use_gpu,
                        docling_dir=get_docling_dir(pdf_path, self.output_dir, self.extraction_root),
                    )
                    text_path.write_text(parsed.raw_text, encoding="utf-8")
                    doc_text_paths[path_key] = relative_text_path
                except Exception as e:
                    logger.warning(f"Failed to extract text from {pdf_path}: {e}")

            if doc_text_paths:
                text_paths_by_doc[doc["id"]] = doc_text_paths

        return text_paths_by_doc

    def download_from_gcs_to_file(self, gcs_path: str, local_path: Path):
        """Download a file from GCS to local path.

        Args:
            gcs_path: GCS path (can be URL or path format).
            local_path: Local file path where to save.
        """
        if local_path.exists():
            return

        # Extract blob name from GCS path
        blob_name = extract_blob_name(gcs_path)

        if not blob_name:
            return

        # Prefix with state code (e.g., "ny/document_link/...")
        if self.state_code:
            blob_name = f"{self.state_code}/{blob_name}"

        # Download from GCS
        blob = self.bucket.blob(blob_name)

        try:
            blob.download_to_filename(str(local_path))
        except Exception as e:
            raise Exception(f"Failed to download {blob_name}: {e}") from e

    def download_from_gcs_to_bytes(self, gcs_path: str) -> bytes | None:
        """Download a file from GCS and return as bytes.

        Args:
            gcs_path: GCS path (can be URL or path format).

        Returns:
            File contents as bytes, or None if download failed.

        Raises:
            Exception: If download fails.
        """
        # Extract blob name from GCS path
        blob_name = extract_blob_name(gcs_path)

        if not blob_name:
            raise Exception(f"Could not extract blob name from {gcs_path}")

        # Prefix with state code (e.g., "ny/document_link/...")
        if self.state_code:
            blob_name = f"{self.state_code}/{blob_name}"

        # Download from GCS
        blob = self.bucket.blob(blob_name)

        if not blob.exists():
            raise FileNotFoundError(
                f"File not found in GCS: gs://{self.gcs_bucket_name}/{blob_name}"
            )

        return blob.download_as_bytes()

    def _to_gcs_uri(self, gcs_path: str) -> str | None:
        """Resolve a DB-stored path into a full downloadable gs:// URI.

        Mirrors the blob-name resolution in download_from_gcs_to_file (same
        state-code prefixing) without actually downloading anything, so a
        metadata-only export (download_files=False) still records enough to
        fetch the file later with `gsutil cp <uri> .` or the GCS client.

        Args:
            gcs_path: Path as stored in the DB (relative or already a full
                gs:// / https:// URL).

        Returns:
            Full "gs://bucket/blob" URI, or None if unresolvable.
        """
        blob_name = extract_blob_name(gcs_path)
        if not blob_name:
            return None
        if self.state_code:
            blob_name = f"{self.state_code}/{blob_name}"
        return f"gs://{self.gcs_bucket_name}/{blob_name}"

    def _extract_filename_from_gcs_path(self, gcs_path: str) -> str:
        """Extract a safe filename from a GCS path.

        Args:
            gcs_path: GCS path.

        Returns:
            Sanitized filename suitable for local filesystem.
        """
        # Get blob name
        blob_name = extract_blob_name(gcs_path)
        if not blob_name:
            # Fallback to hash of path
            import hashlib

            return f"file_{hashlib.md5(gcs_path.encode()).hexdigest()}.pdf"

        # Get last part of path
        filename = blob_name.split("/")[-1]

        # URL decode
        filename = unquote(filename)

        # Sanitize filename - remove or replace problematic characters.
        # Includes "/" and "\\" because unquote() can turn an encoded "%2F"
        # into a literal path separator, which would otherwise silently
        # create a bogus nested directory under the local output path.
        filename = re.sub(r'[<>:"|?*/\\]', "_", filename)

        # Ensure it has an extension
        if not filename.endswith(".pdf"):
            filename += ".pdf"

        return filename

    def _extract_pdf_metadata(self, pdf_path: Path) -> dict[str, Any] | None:
        """Extract metadata from a PDF file using pdfinfo command.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            Dictionary with PDF metadata, or None if extraction fails.
        """
        if not pdf_path.exists():
            return None

        try:
            # Try using pdfinfo command-line tool (part of poppler-utils)
            result = subprocess.run(
                ["pdfinfo", str(pdf_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                metadata = {}
                for line in result.stdout.split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        key = key.strip()
                        value = value.strip()
                        if value:  # Only include non-empty values
                            metadata[key] = value
                return metadata if metadata else None

        except FileNotFoundError:
            # pdfinfo not available - silently skip PDF metadata extraction
            logger.debug("pdfinfo command not found. Install poppler-utils to extract PDF metadata.")
            return None
        except subprocess.TimeoutExpired:
            logger.warning(f"PDF metadata extraction timed out for {pdf_path}")
            return None
        except Exception as e:
            logger.warning(f"Failed to extract PDF metadata from {pdf_path}: {e}")
            return None