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
    ):
        """Initialize the case exporter.

        Args:
            engine: SQLAlchemy engine connected to the scrapping database (port 5433).
            output_dir: Directory where case JSON files and PDFs will be saved.
            gcs_bucket_name: GCS bucket name where documents are stored (default: court-docs).
        """
        self.engine = engine
        self.output_dir = Path(output_dir)
        self.gcs_bucket_name = gcs_bucket_name
        self.storage_client = storage.Client()
        self.bucket = self.storage_client.bucket(gcs_bucket_name)

    def export_case_by_id(self, case_id: int) -> Path:
        """Export a case by its database ID (court_cases.id).

        Args:
            case_id: The integer ID from court_cases.id column.

        Returns:
            Path to the created JSON file.
        """
        # Query case data
        case_query = text("""
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
            FROM public.court_cases
            WHERE id = :case_id
        """)

        with self.engine.connect() as conn:
            result = conn.execute(case_query, {"case_id": case_id})
            case_row = result.fetchone()

        if not case_row:
            raise ValueError(f"Case with id={case_id} not found")

        # Convert to dict
        case_data = dict(case_row._mapping)

        # Query documents for this case
        docs_query = text("""
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
            FROM public.court_documents
            WHERE case_id = :case_id
            ORDER BY id
        """)

        with self.engine.connect() as conn:
            result = conn.execute(docs_query, {"case_id": case_data["case_id"]})
            docs_rows = result.fetchall()

        # Convert documents to list of dicts
        documents = [dict(row._mapping) for row in docs_rows]

        if not documents:
            raise ValueError(f"Case with id={case_id} has no documents, skipping export")

        # Create denormalized structure
        denormalized_case = self._create_denormalized_json(case_data, documents)

        # Create output directory for this case
        case_dir = self.output_dir / f"case_{case_id}"
        case_dir.mkdir(parents=True, exist_ok=True)

        # Download PDF files
        self._download_case_files(documents, case_dir)

        # Save JSON
        json_path = case_dir / f"case_{case_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(denormalized_case, f, indent=2, default=str)

        return json_path

    def _create_denormalized_json(
        self, case_data: dict[str, Any], documents: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create a denormalized JSON structure.

        Args:
            case_data: Dictionary containing case information.
            documents: List of dictionaries containing document information.

        Returns:
            Denormalized dictionary ready for JSON serialization.
        """
        # Process documents to include local file paths
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

            processed_docs.append(processed_doc)

        # Create denormalized structure
        return {
            "case_info": case_data,
            "documents": processed_docs,
            "summary": {
                "total_documents": len(documents),
                "case_id": case_data.get("case_id"),
                "docket_id": case_data.get("docket_id"),
                "caption": case_data.get("caption"),
                "court": case_data.get("court"),
                "case_status": case_data.get("case_status"),
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

        # Sanitize filename - remove or replace problematic characters
        filename = re.sub(r'[<>:"|?*]', "_", filename)

        # Ensure it has an extension
        if not filename.endswith(".pdf"):
            filename += ".pdf"

        return filename