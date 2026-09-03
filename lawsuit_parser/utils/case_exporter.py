"""Export court cases with documents to denormalized JSON format.

This module provides utilities to export complete case information from the
PostgreSQL database, download associated files from Google Cloud Storage, and
create denormalized JSON files for easy consumption.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pandas as pd
from google.cloud import storage
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lawsuit_parser.utils.gcs import extract_blob_name


class CaseExporter:
    """Export court cases with all related data and files."""

    def __init__(
        self,
        engine: Engine,
        output_dir: Path | str,
        gcs_bucket_name: str = "court-docs",
        schema: str = "courts_final",
        table_prefix: str = "ny_",
        extract_text: bool = False,
        use_gpu: bool = True,
    ):
        """Initialize the case exporter.

        Args:
            engine: SQLAlchemy engine connected to the scrapping database (port 5433).
            output_dir: Directory where case JSON files and PDFs will be saved.
            gcs_bucket_name: GCS bucket name where documents are stored (default: court-docs).
            schema: Postgres schema holding the crawl tables (default: courts_final,
                where the former ``public.court_cases``/``public.court_documents``
                data now lives).
            table_prefix: Per-state table prefix, e.g. "ny_" for
                ``ny_cases_after_search``/``ny_docket_documents``, "fl_" for the
                Florida tables, etc. Pass "" for un-prefixed tables.
            extract_text: If True, run Docling over every downloaded PDF and
                save a plain-text ``.txt`` counterpart next to it (plus
                Docling's full structured output under ``docling/``) - see
                ``_extract_case_text``. Off by default: it's a slow, GPU/CPU-
                heavy extra step most callers don't need just to get the
                PDFs + DB metadata.
            use_gpu: Whether Docling should use GPU acceleration when
                ``extract_text=True``. Ignored otherwise.
        """
        self.engine = engine
        self.output_dir = Path(output_dir)
        self.gcs_bucket_name = gcs_bucket_name
        self.schema = schema
        self.table_prefix = table_prefix
        self.extract_text = extract_text
        self.use_gpu = use_gpu
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

    def export_case_by_id(self, case_id: int) -> Path:
        """Export a case by its database ID ({table_prefix}cases_after_search.id).

        Args:
            case_id: The integer ID from the cases table's ``id`` column.

        Returns:
            Path to the created JSON file.
        """
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

        # Create output directory for this case
        case_dir = self.output_dir / f"case_{case_id}"
        case_dir.mkdir(parents=True, exist_ok=True)

        # Download PDF files
        self._download_case_files(documents, case_dir)

        # Optional Docling text extraction, run only once the PDFs are on
        # disk to extract from (see _extract_case_text and the
        # extract_text flag on __init__).
        text_paths_by_doc: dict[int, dict[str, str]] = {}
        if self.extract_text:
            text_paths_by_doc = self._extract_case_text(documents, case_dir)

        # Create denormalized structure
        denormalized_case = self._create_denormalized_json(
            case_data, documents, case_history, transcriptions_by_doc, text_paths_by_doc
        )

        # Save JSON
        json_path = case_dir / f"case_{case_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(denormalized_case, f, indent=2, default=str)

        return json_path

    def _create_denormalized_json(
        self,
        case_data: dict[str, Any],
        documents: list[dict[str, Any]],
        case_history: list[dict[str, Any]],
        transcriptions_by_doc: dict[int, list[dict[str, Any]]],
        text_paths_by_doc: dict[int, dict[str, str]],
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

        Returns:
            Denormalized dictionary ready for JSON serialization.
        """
        # Process documents to include local file paths, any OCR
        # transcriptions recorded for them, and any Docling-extracted text.
        processed_docs = []
        for doc in documents:
            processed_doc = doc.copy()

            # Add local file path references
            if doc.get("document_bucket_link"):
                filename = self._extract_filename_from_gcs_path(
                    doc["document_bucket_link"]
                )
                processed_doc["local_document_path"] = f"documents/{filename}"

            if doc.get("document_confirmation_bucket_link"):
                filename = self._extract_filename_from_gcs_path(
                    doc["document_confirmation_bucket_link"]
                )
                processed_doc["local_confirmation_path"] = f"confirmations/{filename}"

            processed_doc["transcriptions"] = transcriptions_by_doc.get(doc["id"], [])

            doc_text_paths = text_paths_by_doc.get(doc["id"], {})
            if doc_text_paths.get("document_text_path"):
                processed_doc["local_document_text_path"] = doc_text_paths["document_text_path"]
            if doc_text_paths.get("confirmation_text_path"):
                processed_doc["local_confirmation_text_path"] = doc_text_paths["confirmation_text_path"]

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
                "text_extraction_enabled": self.extract_text,
                "exported_at": datetime.utcnow().isoformat(),
            },
        }

    def _download_case_files(self, documents: list[dict[str, Any]], case_dir: Path):
        """Download all files for a case from GCS.

        Args:
            documents: List of document dictionaries.
            case_dir: Directory where files should be saved.
        """
        # Create subdirectories
        docs_dir = case_dir / "documents"
        confirm_dir = case_dir / "confirmations"
        docs_dir.mkdir(exist_ok=True)
        confirm_dir.mkdir(exist_ok=True)

        for doc in documents:
            # Download main document
            if doc.get("document_bucket_link"):
                gcs_path = doc["document_bucket_link"]
                filename = self._extract_filename_from_gcs_path(gcs_path)
                local_path = docs_dir / filename

                try:
                    self.download_from_gcs_to_file(gcs_path, local_path)
                except Exception as e:
                    print(f"Warning: Failed to download {gcs_path}: {e}")

            # Download confirmation document
            if doc.get("document_confirmation_bucket_link"):
                gcs_path = doc["document_confirmation_bucket_link"]
                filename = self._extract_filename_from_gcs_path(gcs_path)
                local_path = confirm_dir / filename

                try:
                    self.download_from_gcs_to_file(gcs_path, local_path)
                except Exception as e:
                    print(f"Warning: Failed to download {gcs_path}: {e}")

    def _extract_case_text(
        self, documents: list[dict[str, Any]], case_dir: Path
    ) -> dict[int, dict[str, str]]:
        """Extract a plain-text version of every downloaded PDF using Docling.

        For each document/confirmation PDF found on disk, saves a ``.txt``
        file with the same stem alongside it (e.g. ``documents/document_xyz.pdf``
        -> ``documents/document_xyz.txt``) - a 1:1 text counterpart for
        ML pipelines that just want to glob PDF/text pairs. Docling's full
        structured output (``.docling.json``/``.md``) is also saved, under
        ``docling/documents`` or ``docling/confirmations`` (see
        lawsuit_parser.parsers.batch.get_docling_dir) - the same layout the
        event-extraction pipeline expects, so a later run over this same
        case directory reuses these outputs instead of re-parsing.

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
                        docling_dir=get_docling_dir(pdf_path),
                    )
                    text_path.write_text(parsed.raw_text, encoding="utf-8")
                    doc_text_paths[path_key] = relative_text_path
                except Exception as e:
                    print(f"Warning: Failed to extract text from {pdf_path}: {e}")

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
            print(f"Skipping existing file: {local_path}")
            return

        # Extract blob name from GCS path
        blob_name = extract_blob_name(gcs_path)

        if not blob_name:
            print(f"Warning: Could not extract blob name from {gcs_path}")
            return

        # Download from GCS
        blob = self.bucket.blob(blob_name)

        try:
            blob.download_to_filename(str(local_path))
            print(f"Downloaded: {blob_name} -> {local_path}")
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

        # Download from GCS
        blob = self.bucket.blob(blob_name)

        if not blob.exists():
            raise FileNotFoundError(
                f"File not found in GCS: gs://{self.gcs_bucket_name}/{blob_name}"
            )

        return blob.download_as_bytes()

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