"""Stage 1: Metadata Extraction.

Extracts metadata from database, PDF files, and Docling parsed documents.
Produces files_scan.json and gliner_config.json artifacts.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import BaseStage
from ..models import (
    Actor,
    CMECFMetadata,
    DatabaseMetadata,
    DoclingMetadata,
    DocumentMetadata,
    ExtractedDate,
    FilesScan,
    GLiNERConfig,
    GLiNERLabels,
    PartyDiscovered,
    PDFMetadata,
)
from ..utils import (
    extract_cm_ecf_header,
    extract_dates_from_text,
    extract_pdf_metadata,
    find_party_aliases,
    normalize_party_name,
    normalize_role,
)


class Stage1Metadata(BaseStage):
    """Stage 1: Extract metadata from all available sources.

    This stage scans:
    1. Database (if available) for case and document metadata
    2. PDF files for file metadata
    3. Docling parsed files for headers and document structure
    4. All sources for dates using regex patterns

    Outputs:
    - files_scan.json: Complete metadata scan
    - gliner_config.json: GLiNER configuration with dynamic actor labels
    """

    stage_number = 1
    stage_name = "metadata"

    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute Stage 1 for a given case.

        Args:
            case_id: Case identifier
            config: Stage-specific configuration
        """
        print(f"\n{'='*60}")
        print(f"Stage 1: Metadata Extraction - {case_id}")
        print(f"{'='*60}\n")

        # Extract configuration
        extract_from_database = config.get("extract_from_database", True)
        extract_from_pdfs = config.get("extract_from_pdfs", True)
        extract_from_docling = config.get("extract_from_docling", True)
        date_patterns = config.get("date_patterns", [])

        # Initialize results
        database_metadata = None
        documents = []
        parties_discovered = []
        all_dates = []

        # 1. Extract from database (if enabled)
        if extract_from_database:
            print("→ Extracting metadata from database...")
            database_metadata = self._extract_from_database(case_id)
            if database_metadata:
                # Add parties from database
                for plaintiff in database_metadata.plaintiff:
                    parties_discovered.append(
                        PartyDiscovered(
                            name=plaintiff,
                            role="plaintiff",
                            source="database",
                            aliases=find_party_aliases(plaintiff)
                        )
                    )
                for defendant in database_metadata.defendant:
                    parties_discovered.append(
                        PartyDiscovered(
                            name=defendant,
                            role="defendant",
                            source="database",
                            aliases=find_party_aliases(defendant)
                        )
                    )

        # 2. Find all case documents
        case_dir = self.get_case_dir(case_id)
        pdf_files = list(case_dir.glob("*.pdf")) if case_dir.exists() else []
        docling_files = list(case_dir.glob("*.docling.json")) if case_dir.exists() else []
        parsed_files = list(case_dir.glob("*.json")) if case_dir.exists() else []

        # Remove .docling.json from parsed_files
        parsed_files = [f for f in parsed_files if not f.name.endswith(".docling.json")]

        print(f"  Found {len(pdf_files)} PDF files")
        print(f"  Found {len(docling_files)} Docling files")
        print(f"  Found {len(parsed_files)} parsed JSON files")

        # 3. Extract metadata for each document
        doc_id_counter = 0
        for pdf_path in pdf_files:
            doc_id = f"doc_{doc_id_counter:03d}"
            doc_id_counter += 1

            print(f"\n→ Processing {pdf_path.name} (doc_id={doc_id})...")

            doc_metadata = DocumentMetadata(
                doc_id=doc_id,
                file_name=pdf_path.name,
            )

            # Extract PDF metadata (if enabled)
            if extract_from_pdfs:
                print(f"  Extracting PDF metadata...")
                pdf_meta = extract_pdf_metadata(pdf_path)
                if pdf_meta:
                    doc_metadata.pdf_metadata = PDFMetadata(**pdf_meta)

            # Extract Docling metadata (if enabled)
            if extract_from_docling:
                docling_path = pdf_path.with_suffix(".pdf.docling.json")
                if docling_path.exists():
                    print(f"  Extracting Docling metadata...")
                    docling_meta = self._extract_from_docling(docling_path)
                    if docling_meta:
                        doc_metadata.docling_metadata = docling_meta

                        # Extract CM/ECF metadata if available
                        if docling_meta.cm_ecf:
                            doc_metadata.document_number = docling_meta.cm_ecf.document_number
                            doc_metadata.filing_date = docling_meta.cm_ecf.filing_date

                # Try to load parsed JSON for additional metadata
                parsed_path = pdf_path.with_suffix(".pdf.json")
                if not parsed_path.exists():
                    parsed_path = pdf_path.with_suffix(".json")

                if parsed_path.exists():
                    try:
                        parsed_data = self.load_json(parsed_path)
                        if "title" in parsed_data and not doc_metadata.document_title:
                            doc_metadata.document_title = parsed_data["title"]
                    except Exception as e:
                        print(f"  Warning: Failed to load parsed JSON: {e}")

            # Extract dates from document
            if date_patterns:
                # Try to load canonical text
                text_path = self.get_documents_dir(case_id) / f"{doc_id}.txt"
                if text_path.exists():
                    text = self.load_text(text_path)
                else:
                    # Fallback: try to get text from parsed JSON or Docling
                    text = self._get_document_text(pdf_path, doc_id)

                if text:
                    print(f"  Extracting dates from text...")
                    dates = extract_dates_from_text(text, date_patterns)
                    for date_text, start, end in dates:
                        extracted_date = ExtractedDate(
                            text=date_text,
                            source="document_body",
                            type="event_date",
                            doc_id=doc_id,
                            char_start=start,
                            char_end=end,
                        )
                        doc_metadata.extracted_dates.append(extracted_date)
                        all_dates.append(extracted_date)

            documents.append(doc_metadata)

        print(f"\n→ Extracted metadata for {len(documents)} documents")
        print(f"→ Discovered {len(parties_discovered)} parties")
        print(f"→ Found {len(all_dates)} dates")

        # 4. Create files_scan artifact
        files_scan = FilesScan(
            case_id=case_id,
            scan_timestamp=datetime.now(),
            database_metadata=database_metadata,
            documents=documents,
            parties_discovered=parties_discovered,
            all_dates=all_dates,
        )

        self.save_artifact(case_id, "files_scan.json", files_scan)

        # 5. Generate GLiNER config
        print("\n→ Generating GLiNER configuration...")
        gliner_config = self._generate_gliner_config(files_scan, config)
        self.save_artifact(case_id, "gliner_config.json", gliner_config)

        print(f"\n{'='*60}")
        print(f"Stage 1 Complete!")
        print(f"{'='*60}\n")

    def validate_inputs(self, case_id: str) -> bool:
        """Check if case directory exists.

        Args:
            case_id: Case identifier

        Returns:
            True if case directory exists
        """
        case_dir = self.get_case_dir(case_id)
        if not case_dir.exists():
            print(f"Error: Case directory not found: {case_dir}")
            return False
        return True

    def get_outputs(self, case_id: str) -> list[Path]:
        """Return paths to stage outputs.

        Args:
            case_id: Case identifier

        Returns:
            List of output file paths
        """
        events_dir = self.get_events_dir(case_id)
        return [
            events_dir / "files_scan.json",
            events_dir / "gliner_config.json",
        ]

    def _extract_from_database(self, case_id: str) -> DatabaseMetadata | None:
        """Extract metadata from PostgreSQL database.

        Args:
            case_id: Case identifier

        Returns:
            Database metadata, or None if database not available
        """
        try:
            from ...utils.db import fetch_from_postgres

            # Try to query case metadata (adjust query based on actual schema)
            # This is a placeholder - adjust based on your actual database schema
            query = f"""
                SELECT
                    case_number,
                    court,
                    status,
                    filed_date
                FROM cases
                WHERE case_id = '{case_id}'
                LIMIT 1
            """

            try:
                df = fetch_from_postgres(query)
                if df.empty:
                    print(f"  No database record found for case {case_id}")
                    return None

                row = df.iloc[0]
                metadata = DatabaseMetadata(
                    case_number=row.get("case_number"),
                    court=row.get("court"),
                    status=row.get("status"),
                    case_filed_date=str(row.get("filed_date")) if row.get("filed_date") else None,
                )

                # Query parties (adjust based on actual schema)
                party_query = f"""
                    SELECT name, role
                    FROM parties
                    WHERE case_id = '{case_id}'
                """
                party_df = fetch_from_postgres(party_query)

                for _, party_row in party_df.iterrows():
                    role = normalize_role(party_row["role"])
                    if role == "plaintiff":
                        metadata.plaintiff.append(party_row["name"])
                    elif role == "defendant":
                        metadata.defendant.append(party_row["name"])

                print(f"  ✓ Loaded database metadata for {case_id}")
                return metadata

            except Exception as e:
                print(f"  Database query failed: {e}")
                print(f"  (This is OK if database is not set up or schema differs)")
                return None

        except ImportError:
            print(f"  Database module not available")
            return None

    def _extract_from_docling(self, docling_path: Path) -> DoclingMetadata | None:
        """Extract metadata from Docling JSON file.

        Args:
            docling_path: Path to .docling.json file

        Returns:
            Docling metadata
        """
        try:
            docling_data = self.load_json(docling_path)

            # Extract title (from first heading or document metadata)
            title = None
            if "main-text" in docling_data:
                for item in docling_data["main-text"][:5]:  # Check first 5 items
                    if item.get("type") == "title" or item.get("label") == "title":
                        title = item.get("text", "").strip()
                        if title:
                            break

            # Extract header (from first page)
            header = None
            if "main-text" in docling_data:
                for item in docling_data["main-text"][:10]:  # Check first 10 items
                    if item.get("label") == "page_header":
                        header = item.get("text", "").strip()
                        if header:
                            break

            # Try to extract CM/ECF metadata from header
            cm_ecf = None
            if header:
                cm_ecf_data = extract_cm_ecf_header(header)
                if cm_ecf_data:
                    cm_ecf = CMECFMetadata(**cm_ecf_data)

            return DoclingMetadata(
                title=title,
                header=header,
                cm_ecf=cm_ecf,
            )

        except Exception as e:
            print(f"  Warning: Failed to extract Docling metadata: {e}")
            return None

    def _get_document_text(self, pdf_path: Path, doc_id: str) -> str:
        """Get document text for date extraction.

        Args:
            pdf_path: Path to PDF file
            doc_id: Document ID

        Returns:
            Document text
        """
        # Try parsed JSON first
        parsed_path = pdf_path.with_suffix(".pdf.json")
        if not parsed_path.exists():
            parsed_path = pdf_path.with_suffix(".json")

        if parsed_path.exists():
            try:
                parsed_data = self.load_json(parsed_path)
                if "raw_text" in parsed_data:
                    return parsed_data["raw_text"]
                if "paragraphs" in parsed_data:
                    return "\n\n".join(parsed_data["paragraphs"])
            except Exception:
                pass

        # Try Docling JSON
        docling_path = pdf_path.with_suffix(".pdf.docling.json")
        if docling_path.exists():
            try:
                docling_data = self.load_json(docling_path)
                if "main-text" in docling_data:
                    texts = [item.get("text", "") for item in docling_data["main-text"]]
                    return "\n".join(texts)
            except Exception:
                pass

        return ""

    def _generate_gliner_config(self, files_scan: FilesScan, config: dict[str, Any]) -> GLiNERConfig:
        """Generate GLiNER configuration based on discovered parties.

        Args:
            files_scan: Files scan artifact
            config: Stage configuration

        Returns:
            GLiNER configuration
        """
        # Get static labels from config (from stage_2 config)
        static_labels = config.get("static_labels", [
            "temporal expression",
            "legal action or event",
            "court",
            "geographic location",
            "monetary amount",
            "document reference",
        ])

        # Generate dynamic labels for discovered parties
        dynamic_labels = []
        actors = []

        for party in files_scan.parties_discovered:
            # Create GLiNER label with role and name
            label = f"{party.role} ({party.name})"
            dynamic_labels.append(label)

            # Create actor
            actor = Actor(
                canonical_name=party.name,
                role=party.role,
                aliases=party.aliases,
                gliner_label=label,
            )
            actors.append(actor)

        # Add generic actor labels
        dynamic_labels.extend(["attorney", "witness", "judge"])

        labels = GLiNERLabels(
            static=static_labels,
            dynamic=dynamic_labels,
        )

        return GLiNERConfig(
            model=config.get("model", "urchade/gliner_multi-v2.1"),
            threshold=config.get("threshold", 0.5),
            batch_size=config.get("batch_size", 8),
            labels=labels,
            actors=actors,
        )
