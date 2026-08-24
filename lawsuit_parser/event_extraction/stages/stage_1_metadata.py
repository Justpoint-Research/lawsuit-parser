"""Stage 1: Metadata Extraction.

Extracts metadata from database, PDF files, Docling parsed documents, and
e-filing confirmation notices. Produces actors.json, products.json,
files_scan.json, and gliner_config.json artifacts.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ...parsers.batch import get_docling_dir
from ..base import BaseStage
from ..llm_validation import (
    identify_document_title_with_llm,
    identify_document_title_with_nuextract,
    identify_products_with_llm,
    identify_products_with_nuextract,
    validate_actors_with_llm,
    validate_actors_with_nuextract,
)
from ..models import (
    Actor,
    ActorsArtifact,
    CMECFMetadata,
    ConfirmationMetadata,
    DatabaseMetadata,
    DoclingMetadata,
    DocumentMetadata,
    DocumentReference,
    ExtractedDate,
    FilesScan,
    GLiNERConfig,
    GLiNERLabels,
    PDFMetadata,
)
from ..utils import (
    extract_cm_ecf_header,
    extract_confirmation_details,
    extract_dates_from_text,
    extract_document_signature,
    extract_pdf_metadata,
    find_document_references,
    find_litigation_captions,
    find_party_aliases,
    find_title_candidates,
    normalize_party_name,
    normalize_role,
    parse_caption_block,
)

# Generic role placeholders added to the roster when no named individual
# was discovered for that role, so GLiNER still has a label to search for.
GENERIC_ACTOR_ROLES: dict[str, str] = {
    "witness": "Witness",
    "attorney": "Attorney",
    "judge": "Judge",
    "court_clerk": "Court Clerk",
}

# Used to pick which document's text to sample for the LLM product-
# identification prompt (see Stage1Metadata._select_product_context_document)
# - a document scoring above the minimum is more likely to actually describe
# the accused product/harm than, say, a bare notice of appearance.
_PRODUCT_CONTEXT_KEYWORDS = (
    "drug", "device", "medication", "medical device", "chemical", "carcinogen",
    "defect", "injury", "warning", "label", "adverse", "side effect", "fda",
    "manufactured", "ingredient", "exposure", "toxic", "hazard", "product liability",
)
_PRODUCT_CONTEXT_MIN_SCORE = 3


class Stage1Metadata(BaseStage):
    """Stage 1: Extract metadata from all available sources.

    This stage scans:
    1. Database (if available) for plaintiff/defendant parties
    2. PDF files for file metadata
    3. Docling parsed files for headers, document structure, each document's
       case-caption block (plaintiff/defendant names), and its own filing
       number/"signature" (a CM/ECF document number, or an e-filing
       system's own document-number stamp - not tied to any one state)
    4. Matching e-filing confirmation notices (confirmations/) for filer,
       assigned judge, court clerk, and filing timestamp - metadata only;
       entity detection in Stage 2 still runs on documents/ alone
    5. Every document's own text for citations of other documents by
       filing number (e.g. "Doc. No. 7"), resolved against every
       document's signature from step 3 to link doc_id <-> doc_id
       cross-references in both directions (see referenced_documents /
       referenced_by on DocumentMetadata)
    6. All sources for dates using regex patterns
    7. Case context (caption, court, case type) and litigation-caption hints
       (e.g. "In Re Depo-Provera Litigation") for an LLM identification of
       the accused medical substance/drug/medical device/cosmetic product
       and the defendant(s) it's attributed to - a reading-comprehension
       task with no fixed textual format, so this runs LLM-first rather
       than validating a regex-built candidate list (see
       llm_validation.identify_products_with_llm)

    The actor roster assembled from 1, 3, and 4 is optionally sanity-checked
    by a local Ollama model (see llm_validation.validate_actors_with_llm)
    before being written out and turned into GLiNER labels; the product
    roster from step 7 joins it there.

    Outputs:
    - actors.json: Every actor identified in the case (name/designation + role)
    - products.json: The accused product(s), if identified, and the
      defendant(s) each is attributed to
    - files_scan.json: Per-document metadata scan, including each document's
      filing-number signature and its cross-document reference links
    - gliner_config.json: GLiNER configuration with dynamic actor/product labels
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
        extract_from_confirmations = config.get("extract_from_confirmations", True)
        date_patterns = config.get("date_patterns", [])
        identify_document_titles = config.get("identify_document_titles", True)
        # Shared by every LLM-assisted step below (title/actor/product
        # identification): which backend, model, and endpoint to use.
        backend = config.get("llm_backend", "ollama")
        llm_model = config.get("llm_model", "gemma4:e4b")
        llm_base_url = config.get("llm_base_url", "http://localhost:11434")
        identify_title = (
            identify_document_title_with_nuextract if backend == "nuextract" else identify_document_title_with_llm
        )

        # Initialize results
        database_metadata = None
        documents = []
        actors: list[Actor] = []
        all_dates = []
        doc_texts: dict[str, str] = {}  # doc_id -> canonical text, reused for product identification below
        litigation_caption_hits: dict[str, list[str]] = {}  # subject name -> doc_ids it was found in

        # 1. Extract from database (if enabled)
        if extract_from_database:
            print("→ Extracting metadata from database...")
            database_metadata = self._extract_from_database(case_id)
            if database_metadata:
                for plaintiff in database_metadata.plaintiff:
                    self._add_actor(actors, plaintiff, "plaintiff", "database")
                for defendant in database_metadata.defendant:
                    self._add_actor(actors, defendant, "defendant", "database")

        # 2. Find all case documents, oldest filing first (see
        # _document_sort_key) - this is also the order doc_id numbers
        # (doc_000, doc_001, ...) get assigned in below, so doc_id order
        # is a chronological reading order, not arbitrary filesystem order.
        documents_dir = self.get_documents_dir(case_id)
        pdf_files = list(documents_dir.glob("*.pdf")) if documents_dir.exists() else []
        pdf_files = sorted(pdf_files, key=self._document_sort_key)
        parsed_files = list(documents_dir.glob("*.json")) if documents_dir.exists() else []

        # Docling outputs (.docling.json, .md) live under a case-level
        # docling/documents/ directory, mirrored from documents/ - see
        # lawsuit_parser.parsers.batch.get_docling_dir.
        docling_dir = self.get_case_dir(case_id) / "docling" / "documents"
        docling_files = list(docling_dir.glob("*.docling.json")) if docling_dir.exists() else []

        confirmations_dir = self.get_confirmations_dir(case_id)
        confirmation_files = list(confirmations_dir.glob("*.json")) if confirmations_dir.exists() else []

        print(f"  Found {len(pdf_files)} PDF files")
        print(f"  Found {len(docling_files)} Docling files")
        print(f"  Found {len(parsed_files)} parsed JSON files")
        print(f"  Found {len(confirmation_files)} confirmation files")

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
                docling_path = get_docling_dir(pdf_path) / f"{pdf_path.stem}.docling.json"
                docling_data = None
                if docling_path.exists():
                    try:
                        docling_data = self.load_json(docling_path)
                    except Exception as e:
                        print(f"  Warning: Failed to load Docling JSON: {e}")

                if docling_data is not None:
                    print(f"  Extracting Docling metadata...")
                    docling_meta, docling_dates, first_page_text = self._extract_from_docling(docling_data, date_patterns)
                    if docling_meta:
                        doc_metadata.docling_metadata = docling_meta

                        # Final title: an LLM reads the page-1 text plus
                        # Docling's own detected title (if any) and the
                        # heuristic candidate lines (see
                        # utils.find_title_candidates) and decides the
                        # actual title - neither signal alone is reliable
                        # enough on its own (Docling rarely tags a "title"
                        # element on these filings; the heuristic can't
                        # tell a title from a party-name line by itself).
                        if identify_document_titles and (
                            first_page_text or docling_meta.title_candidates or docling_meta.title
                        ):
                            print(f"  Identifying document title (backend={backend})...")
                            title = identify_title(
                                text_excerpt=first_page_text[:3000],
                                candidates=docling_meta.title_candidates,
                                docling_title=docling_meta.title,
                                model=llm_model,
                                base_url=llm_base_url,
                            )
                            if title:
                                doc_metadata.document_title = title
                                print(f"  Title: {title}")

                        # Extract CM/ECF (or NYSCEF) metadata if available
                        if docling_meta.cm_ecf:
                            doc_metadata.document_number = docling_meta.cm_ecf.document_number
                            doc_metadata.filing_date = docling_meta.cm_ecf.filing_date

                        # Some state e-filing systems (e.g. NY's NYSCEF)
                        # don't carry their document number in the CM/ECF-
                        # style header - fall back to the system's own stamp.
                        if not doc_metadata.document_number and docling_meta.document_signature:
                            doc_metadata.document_number = docling_meta.document_signature

                    # Timestamps found in the header and first page
                    for extracted_date in docling_dates:
                        extracted_date.doc_id = doc_id
                        doc_metadata.extracted_dates.append(extracted_date)
                        all_dates.append(extracted_date)

                    # Plaintiff(s)/defendant(s) named in this document's own
                    # case-caption block (the fullest, most reliable source -
                    # the DB caption is often truncated, e.g. "... et al.").
                    caption_parties = self._extract_caption_actors(docling_data)
                    for plaintiff in caption_parties["plaintiffs"]:
                        self._add_actor(actors, plaintiff, "plaintiff", "caption", doc_id)
                    for defendant in caption_parties["defendants"]:
                        self._add_actor(actors, defendant, "defendant", "caption", doc_id)

                # Try to load parsed JSON for additional metadata
                parsed_path = pdf_path.with_suffix(".json")

                if parsed_path.exists():
                    try:
                        parsed_data = self.load_json(parsed_path)
                        legacy_title = parsed_data.get("title")
                        # Some upstream parsers default "title" to the input
                        # filename when they found no real title - that's not
                        # a title, it's the file_name field again under a
                        # different name. Only trust it if it isn't that.
                        if (
                            legacy_title
                            and legacy_title != pdf_path.stem
                            and not doc_metadata.document_title
                        ):
                            doc_metadata.document_title = legacy_title
                    except Exception as e:
                        print(f"  Warning: Failed to load parsed JSON: {e}")

            # Extract metadata from the matching e-filing confirmation notice
            # (same file name under confirmations/ - see get_confirmations_dir).
            # The confirmation is only ever a metadata source here: entity
            # detection in Stage 2 still runs on the documents/ files alone.
            if extract_from_confirmations:
                confirmation_parsed_path = (
                    self.get_confirmations_dir(case_id) / pdf_path.name
                ).with_suffix(".json")

                if confirmation_parsed_path.exists():
                    try:
                        confirmation_data = self.load_json(confirmation_parsed_path)
                        details = extract_confirmation_details(
                            confirmation_data.get("paragraphs", [])
                        )
                    except Exception as e:
                        print(f"  Warning: Failed to load confirmation metadata: {e}")
                        details = {}

                    if details:
                        print(f"  Extracting confirmation metadata...")
                        doc_metadata.confirmation_metadata = ConfirmationMetadata(**details)
                        if not doc_metadata.filed_by and details.get("filer_name"):
                            doc_metadata.filed_by = details["filer_name"]

                        if details.get("assigned_judge"):
                            self._add_actor(actors, details["assigned_judge"], "judge", "confirmation", doc_id)
                        if details.get("filer_name"):
                            self._add_actor(actors, details["filer_name"], "counsel", "confirmation", doc_id)
                        if details.get("court_clerk"):
                            self._add_actor(actors, details["court_clerk"], "court_clerk", "confirmation", doc_id)

                        if date_patterns and details.get("notice_timestamp"):
                            extracted_date = ExtractedDate(
                                text=details["notice_timestamp"],
                                source="confirmation",
                                type="filing_date",
                                doc_id=doc_id,
                            )
                            doc_metadata.extracted_dates.append(extracted_date)
                            all_dates.append(extracted_date)

            # Load canonical text once, reused for date extraction (below,
            # gated on date_patterns), cross-document reference detection,
            # and litigation-caption scanning for product identification
            # (both unconditional - neither depends on date_patterns).
            text_path = self.get_documents_dir(case_id) / f"{doc_id}.txt"
            if text_path.exists():
                text = self.load_text(text_path)
            else:
                # Fallback: try to get text from parsed JSON or Docling
                text = self._get_document_text(pdf_path, doc_id)
            if text:
                doc_texts[doc_id] = text

            # Extract dates from document
            if date_patterns and text:
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

            # Cross-document references: other documents this one cites by
            # NYSCEF/document number (see utils.find_document_references).
            # Resolved to doc_id in a second pass below, once every
            # document's own document_number (its "signature") is known.
            if text:
                for ref_number, start, end in find_document_references(text):
                    if doc_metadata.document_number and ref_number == doc_metadata.document_number:
                        continue  # this document's own self-identifying stamp, not a citation
                    doc_metadata.referenced_documents.append(DocumentReference(
                        doc_number=ref_number,
                        char_start=start,
                        char_end=end,
                    ))
                if doc_metadata.referenced_documents:
                    cited = sorted({r.doc_number for r in doc_metadata.referenced_documents})
                    print(f"  Cites document number(s): {', '.join(cited)}")

            # Litigation-caption hint for product identification below (see
            # utils.find_litigation_captions): coordinated/MDL proceedings
            # often name the accused product right in the caption.
            if text:
                for subject in find_litigation_captions(text):
                    litigation_caption_hits.setdefault(subject, []).append(doc_id)

            documents.append(doc_metadata)

        print(f"\n→ Extracted metadata for {len(documents)} documents")
        print(f"→ Discovered {len(actors)} actors")
        print(f"→ Found {len(all_dates)} dates")

        # 3.5. Resolve cross-document references now that every document's
        # own document_number (its "signature") is known, and build the
        # reverse index (referenced_by) so a document can be looked up by
        # what cites it, not just what it cites.
        print("\n→ Resolving cross-document references...")
        docs_by_id = {doc.doc_id: doc for doc in documents}
        doc_number_to_id = {doc.document_number: doc.doc_id for doc in documents if doc.document_number}
        resolved_count = 0
        for doc in documents:
            for ref in doc.referenced_documents:
                target_id = doc_number_to_id.get(ref.doc_number)
                if not target_id or target_id == doc.doc_id:
                    continue
                ref.doc_id = target_id
                resolved_count += 1
                target = docs_by_id[target_id]
                if doc.doc_id not in target.referenced_by:
                    target.referenced_by.append(doc.doc_id)
        print(f"  Resolved {resolved_count} references to documents in this case")

        caption, court, case_type = self._load_case_context(case_id)

        # 4. Validate the discovered actor roster with an LLM (optional)
        if config.get("validate_actors_with_llm", True) and actors:
            print(f"\n→ Validating actor roster with LLM (backend={backend})...")
            validator = validate_actors_with_nuextract if backend == "nuextract" else validate_actors_with_llm
            actors = validator(
                actors,
                caption=caption,
                court=court,
                model=llm_model,
                base_url=llm_base_url,
            )
            print(f"  Roster after validation: {len(actors)} actors")

        # 4.5. Identify the accused product(s) - the medical substance/drug/
        # medical device/cosmetic product the plaintiff blames for harm,
        # attributed to a defendant, if determinable. No fixed textual
        # format to regex an arbitrary product name from (unlike a caption
        # block or a citation), so this is LLM-first: caption/court/case
        # type, any "In Re ... Litigation" hits from step 3, and an excerpt
        # of whichever document reads most like it describes the product/
        # harm (see _select_product_context_document).
        products: list[Actor] = []
        if config.get("extract_products", True):
            print("\n→ Identifying accused product(s)...")
            sample_doc_id, sample_text = self._select_product_context_document(documents, doc_texts)
            defendant_names = [a.canonical_name for a in actors if a.role == "defendant"]

            identifier = identify_products_with_nuextract if backend == "nuextract" else identify_products_with_llm
            results = identifier(
                caption=caption,
                court=court,
                case_type=case_type,
                litigation_caption_candidates=list(litigation_caption_hits.keys()),
                defendant_names=defendant_names,
                text_sample=sample_text,
                model=llm_model,
                base_url=llm_base_url,
            )

            for result in results:
                doc_ids = set(litigation_caption_hits.get(result["name"], []))
                if sample_doc_id:
                    doc_ids.add(sample_doc_id)
                products.append(Actor(
                    canonical_name=result["name"],
                    role=result["product_type"],
                    is_named=True,
                    source="llm",
                    aliases=result["aliases"],
                    doc_ids=sorted(doc_ids),
                    attributed_to=result["attributed_to"],
                ))
                attribution = f" (attributed to {', '.join(result['attributed_to'])})" if result["attributed_to"] else ""
                print(f"  {result['product_type']}: {result['name']}{attribution}")

            if not products:
                print("  No accused product identified")

        products_artifact = ActorsArtifact(case_id=case_id, actors=products)
        self.save_artifact(case_id, "products.json", products_artifact)

        # 5. Fill in generic role placeholders for roles with no named
        # individual, so GLiNER still has a label to search for.
        for role, designation in GENERIC_ACTOR_ROLES.items():
            if not any(a.role == role for a in actors):
                actors.append(Actor(
                    canonical_name=designation,
                    role=role,
                    is_named=False,
                    source="generic",
                ))

        actors_artifact = ActorsArtifact(case_id=case_id, actors=actors)
        self.save_artifact(case_id, "actors.json", actors_artifact)

        # 6. Create files_scan artifact
        files_scan = FilesScan(
            case_id=case_id,
            scan_timestamp=datetime.now(),
            database_metadata=database_metadata,
            documents=documents,
            all_dates=all_dates,
        )

        self.save_artifact(case_id, "files_scan.json", files_scan)

        # 7. Generate GLiNER config from the combined actor + product roster
        print("\n→ Generating GLiNER configuration...")
        combined_roster = ActorsArtifact(case_id=case_id, actors=actors + products)
        gliner_config = self._generate_gliner_config(combined_roster, config)
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
            events_dir / "actors.json",
            events_dir / "products.json",
            events_dir / "files_scan.json",
            events_dir / "gliner_config.json",
        ]

    def _add_actor(
        self,
        actors: list[Actor],
        name: str,
        role: str,
        source: str,
        doc_id: str | None = None,
    ) -> None:
        """Add a discovered actor, merging into an existing entry (same
        normalized name + role) instead of duplicating it across sources
        or documents.

        Args:
            actors: Roster to add to (mutated in place)
            name: Actor's name as discovered
            role: e.g. 'plaintiff', 'defendant', 'judge', 'court_clerk', 'counsel'
            source: Where this was discovered ('database', 'caption', 'confirmation')
            doc_id: Document this instance was found in, if any
        """
        name = name.strip()
        if not name:
            return

        normalized = normalize_party_name(name)
        for actor in actors:
            if actor.role == role and normalize_party_name(actor.canonical_name) == normalized:
                if doc_id and doc_id not in actor.doc_ids:
                    actor.doc_ids.append(doc_id)
                return

        actors.append(Actor(
            canonical_name=name,
            role=role,
            is_named=True,
            source=source,
            aliases=find_party_aliases(name),
            doc_ids=[doc_id] if doc_id else [],
        ))

    def _load_case_context(self, case_id: str) -> tuple[str | None, str | None, str | None]:
        """Load case-level caption/court/case_type strings, for LLM context
        (actor roster validation and product identification).

        Args:
            case_id: Case identifier

        Returns:
            (caption, court, case_type) tuple, any of which may be None
        """
        case_json = self.get_case_dir(case_id) / f"{case_id}.json"
        if not case_json.exists():
            return None, None, None

        try:
            case_data = self.load_json(case_json)
        except Exception:
            return None, None, None

        case_info = case_data.get("case_info", {})
        return case_info.get("caption"), case_info.get("court"), case_info.get("case_type")

    def _select_product_context_document(
        self,
        documents: list[DocumentMetadata],
        doc_texts: dict[str, str],
        max_chars: int = 4000,
    ) -> tuple[str | None, str]:
        """Pick the document most likely to describe the accused product/harm
        (highest density of product-liability keywords) and return an excerpt
        centered on where those keywords actually appear, for the LLM product
        identification prompt.

        Args:
            documents: This case's documents (order doesn't matter)
            doc_texts: doc_id -> canonical text, collected during the main scan
            max_chars: Maximum excerpt length

        Returns:
            (doc_id, excerpt) - doc_id is None and excerpt is "" if no
            document scored above the minimum keyword threshold
        """
        best_doc_id, best_score, best_offset = None, 0, 0
        for doc in documents:
            text = doc_texts.get(doc.doc_id, "")
            if not text:
                continue
            low = text.lower()
            score = sum(low.count(kw) for kw in _PRODUCT_CONTEXT_KEYWORDS)
            if score > best_score:
                first_hit = min(
                    (low.find(kw) for kw in _PRODUCT_CONTEXT_KEYWORDS if kw in low),
                    default=0,
                )
                best_score, best_doc_id, best_offset = score, doc.doc_id, first_hit

        if best_doc_id is None or best_score < _PRODUCT_CONTEXT_MIN_SCORE:
            return None, ""

        text = doc_texts[best_doc_id]
        start = max(0, best_offset - 500)
        return best_doc_id, text[start:start + max_chars]

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

    def _document_sort_key(self, pdf_path: Path) -> tuple[bool, datetime, str]:
        """Sort key for ordering documents oldest-to-newest, then by
        filename - this determines the doc_id (doc_000, doc_001, ...) each
        document gets assigned in run(), so doc_id order becomes a
        chronological reading order instead of arbitrary filesystem order.

        Prefers the document's own filing date (its CM/ECF-style header
        stamp, e.g. NYSCEF's "FILED: ... 03/03/2026"); falls back to the
        PDF's own CreationDate metadata when no header filing date is
        found. Documents with neither sort last (grouped after every dated
        document), tie-broken by filename like everything else.

        Args:
            pdf_path: Path to the PDF file being sorted

        Returns:
            (is_undated, date, file_name) - sortable tuple; is_undated
            pushes undated documents to the end regardless of the
            placeholder date used for them
        """
        sort_date = None

        docling_path = get_docling_dir(pdf_path) / f"{pdf_path.stem}.docling.json"
        if docling_path.exists():
            try:
                docling_data = self.load_json(docling_path)
                # date_patterns=[] skips ExtractedDate collection (not needed
                # here) but still runs the (unconditional) CM/ECF header parse.
                docling_meta, _, _ = self._extract_from_docling(docling_data, date_patterns=[])
                if docling_meta and docling_meta.cm_ecf and docling_meta.cm_ecf.filing_date:
                    sort_date = self._parse_date_loosely(docling_meta.cm_ecf.filing_date)
            except Exception:
                pass

        if sort_date is None:
            pdf_meta = extract_pdf_metadata(pdf_path)
            sort_date = pdf_meta.get("created")

        return (sort_date is None, sort_date or datetime.max, pdf_path.name)

    @staticmethod
    def _parse_date_loosely(text: str) -> datetime | None:
        """Best-effort parse of a raw date string (e.g. "03/03/2026",
        "August 25, 2026") into a real datetime, for sorting only - the
        stored ExtractedDate.text values remain unparsed surface strings."""
        try:
            ts = pd.to_datetime(text, errors="coerce")
        except Exception:
            return None
        return None if pd.isna(ts) else ts.to_pydatetime()

    def _extract_from_docling(
        self, docling_data: dict, date_patterns: list[str]
    ) -> tuple[DoclingMetadata | None, list[ExtractedDate], str]:
        """Extract metadata and timestamps from parsed Docling JSON data.

        Docling's current export schema lists every text item in a flat
        `texts` array, each tagged with a `label` (e.g. "page_header",
        "title", "text") and a `prov` entry giving its page number - there
        is no single "header" or "first page" field, so both have to be
        assembled from the items that land on page 1.

        Args:
            docling_data: Parsed .docling.json contents
            date_patterns: Regex patterns used to find timestamps in the
                header and first page text

        Returns:
            Tuple of (Docling metadata, timestamps found in the header/first
            page, first-page text) - the text is returned separately since
            callers also use it as LLM context for title identification
            (see identify_document_title_with_llm), not just for date/
            signature extraction.
        """
        try:
            texts = docling_data.get("texts", [])

            def item_page_no(item: dict) -> int | None:
                prov = item.get("prov") or []
                return prov[0].get("page_no") if prov else None

            first_page_items = [item for item in texts if item_page_no(item) == 1]

            # Title: first "title" labeled item on the first page
            title = None
            for item in first_page_items:
                if item.get("label") == "title":
                    text = (item.get("text") or "").strip()
                    if text:
                        title = text
                        break

            # Heuristic candidates for a document's title/type, used to
            # arbitrate the final title via an LLM when Docling doesn't
            # tag one - see utils.find_title_candidates.
            title_candidates = find_title_candidates(first_page_items)

            # Header: page_header item(s) on the first page
            header_parts = [
                (item.get("text") or "").strip()
                for item in first_page_items
                if item.get("label") == "page_header" and (item.get("text") or "").strip()
            ]
            header = " ".join(header_parts) if header_parts else None

            # Try to extract CM/ECF (or NYSCEF) metadata from the header
            cm_ecf = None
            if header:
                cm_ecf_data = extract_cm_ecf_header(header)
                if cm_ecf_data:
                    cm_ecf = CMECFMetadata(**cm_ecf_data)

            first_page_text = "\n".join(
                (item.get("text") or "").strip()
                for item in first_page_items
                if item.get("label") != "page_header" and (item.get("text") or "").strip()
            )

            # This document's own filing-system stamp (e.g. "NYSCEF DOC.
            # NO. 11") - its "signature", used to resolve other documents'
            # citations of it. Not tied to any one state/system - see
            # utils.extract_document_signature.
            document_signature = extract_document_signature(first_page_text)

            # Timestamps anywhere in the header or the rest of the first page
            extracted_dates: list[ExtractedDate] = []
            if date_patterns:
                if header:
                    for date_text, start, end in extract_dates_from_text(header, date_patterns):
                        extracted_dates.append(ExtractedDate(
                            text=date_text,
                            source="docling_header",
                            type="filing_date",
                            char_start=start,
                            char_end=end,
                        ))

                if first_page_text:
                    for date_text, start, end in extract_dates_from_text(first_page_text, date_patterns):
                        extracted_dates.append(ExtractedDate(
                            text=date_text,
                            source="docling_first_page",
                            type="event_date",
                            char_start=start,
                            char_end=end,
                        ))

            return (
                DoclingMetadata(
                    title=title,
                    title_candidates=title_candidates,
                    header=header,
                    cm_ecf=cm_ecf,
                    document_signature=document_signature,
                ),
                extracted_dates,
                first_page_text,
            )

        except Exception as e:
            print(f"  Warning: Failed to extract Docling metadata: {e}")
            return None, [], ""

    def _extract_caption_actors(self, docling_data: dict) -> dict[str, list[str]]:
        """Parse plaintiff/defendant names out of a document's first-page
        case-caption block (see utils.parse_caption_block).

        Args:
            docling_data: Parsed .docling.json contents

        Returns:
            Dict with "plaintiffs" and "defendants" name lists (possibly empty)
        """
        try:
            texts = docling_data.get("texts", [])
            first_page_lines = [
                (item.get("text") or "").strip()
                for item in texts
                if (item.get("prov") or [{}])[0].get("page_no") == 1 and (item.get("text") or "").strip()
            ]
            return parse_caption_block(first_page_lines)
        except Exception as e:
            print(f"  Warning: Failed to parse caption block: {e}")
            return {"plaintiffs": [], "defendants": []}

    def _get_document_text(self, pdf_path: Path, doc_id: str) -> str:
        """Get document text for date extraction.

        Args:
            pdf_path: Path to PDF file
            doc_id: Document ID

        Returns:
            Document text
        """
        # Try parsed JSON first
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
        docling_path = get_docling_dir(pdf_path) / f"{pdf_path.stem}.docling.json"
        if docling_path.exists():
            try:
                docling_data = self.load_json(docling_path)
                if "texts" in docling_data:
                    texts = [item.get("text", "") for item in docling_data["texts"]]
                    return "\n".join(texts)
            except Exception:
                pass

        return ""

    def _generate_gliner_config(self, actors_artifact: ActorsArtifact, config: dict[str, Any]) -> GLiNERConfig:
        """Generate GLiNER configuration from the discovered actor roster.

        Args:
            actors_artifact: actors.json contents
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

        dynamic_labels = []
        seen_labels = set()
        gliner_actors = []

        for actor in actors_artifact.actors:
            role_display = actor.role.replace("_", " ")
            label = actor.gliner_label or (
                f"{role_display} ({actor.canonical_name})" if actor.is_named else role_display
            )
            gliner_actors.append(actor.model_copy(update={"gliner_label": label}))

            # Near-duplicate actors (OCR spelling variants, an LLM-cleaned
            # label matching another candidate's) can end up with the same
            # label text - keep each label only once in what's actually
            # sent to GLiNER, while every actor still keeps its own
            # gliner_label for entity-to-actor linking in Stage 2.
            if label not in seen_labels:
                seen_labels.add(label)
                dynamic_labels.append(label)

        labels = GLiNERLabels(
            static=static_labels,
            dynamic=dynamic_labels,
        )

        return GLiNERConfig(
            model=config.get("model", "urchade/gliner_multi-v2.1"),
            threshold=config.get("threshold", 0.5),
            batch_size=config.get("batch_size", 8),
            labels=labels,
            actors=gliner_actors,
        )
