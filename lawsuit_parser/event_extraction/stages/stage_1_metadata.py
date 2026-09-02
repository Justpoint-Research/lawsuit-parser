"""Stage 1: Metadata Extraction.

Extracts metadata from PDF files, Docling parsed documents, and e-filing
confirmation notices. Produces actors.json, products.json, files_scan.json,
and gliner_config.json artifacts.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from tqdm import tqdm

from ...parsers.batch import get_docling_dir
from ..base import BaseStage
from ..llm_validation import (
    extract_actors_from_document_with_llm,
    extract_actors_from_document_with_nuextract,
    identify_document_title_with_llm,
    identify_document_title_with_nuextract,
    identify_products_with_llm,
    identify_products_with_nuextract,
    unload_ollama_model,
    validate_actors_with_llm,
    validate_actors_with_nuextract,
)
from ..models import (
    Actor,
    ActorsArtifact,
    CMECFMetadata,
    ConfirmationMetadata,
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
    extract_appearances_block,
    extract_cm_ecf_header,
    extract_confirmation_details,
    extract_court_venue,
    extract_dates_from_text,
    extract_document_signature,
    extract_pdf_metadata,
    find_document_references,
    find_litigation_captions,
    find_party_aliases,
    find_title_candidates,
    names_match,
    parse_caption_block,
    parse_date_loosely,
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

logger = logging.getLogger(__name__)


class Stage1Metadata(BaseStage):
    """Stage 1: Extract metadata from all available sources.

    This stage scans:
    1. PDF files for file metadata (creation/modification timestamps -
       also a last-resort filing-date fallback, see step 3 below)
    2. Docling parsed files for headers, document structure, each document's
       case-caption block (plaintiff/defendant names), and its own filing
       number/"signature" (a CM/ECF document number, or an e-filing
       system's own document-number stamp - not tied to any one state)
    3. Matching e-filing confirmation notices (confirmations/) for filer,
       assigned judge, court clerk, and filing timestamp - metadata only;
       entity detection in Stage 2 still runs on documents/ alone
    4. Every document's own text for citations of other documents by
       filing number (e.g. "Doc. No. 7"), resolved against every
       document's signature from step 2 to link doc_id <-> doc_id
       cross-references in both directions (see referenced_documents /
       referenced_by on DocumentMetadata)
    5. All sources for dates using regex patterns
    6. Case context (caption, court, case type) and litigation-caption hints
       (e.g. "In Re Depo-Provera Litigation") for an LLM identification of
       the accused medical substance/drug/medical device/cosmetic product
       and the defendant(s) it's attributed to - a reading-comprehension
       task with no fixed textual format, so this runs LLM-first rather
       than validating a regex-built candidate list (see
       llm_validation.identify_products_with_llm)

    A per-case database lookup (the scraping DB's court_cases table) used
    to seed plaintiff/defendant/court here too, but was removed - it's a
    dead end for most cases (only populated for case_<numeric> ids scraped
    from that DB; an MDL docket like mdl-1954 never has one, and even when
    present the table has no plaintiff/defendant columns at all). Party/
    court discovery now relies entirely on per-document signals: caption
    parsing (step 2), confirmation notices (step 3), and the comprehensive
    LLM extraction pass below.

    The actor roster assembled from 2 and 3 is optionally sanity-checked
    by a local Ollama model (see llm_validation.validate_actors_with_llm)
    before being written out and turned into GLiNER labels; the product
    roster from step 6 joins it there.

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
        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 1: Metadata Extraction - {case_id}")
        logger.info(f"{'='*60}\n")

        # Extract configuration
        extract_from_pdfs = config.get("extract_from_pdfs", True)
        extract_from_docling = config.get("extract_from_docling", True)
        extract_from_confirmations = config.get("extract_from_confirmations", True)
        date_patterns = config.get("date_patterns", [])
        identify_document_titles = config.get("identify_document_titles", True)
        # Shared by every LLM-assisted step below (title/actor/product
        # identification): which backend, model, and endpoint to use.
        backend = config.get("llm_backend", "ollama")
        llm_model = config["llm_model"]
        llm_base_url = config["llm_base_url"]
        identify_title = (
            identify_document_title_with_nuextract if backend == "nuextract" else identify_document_title_with_llm
        )

        # Initialize results
        documents = []
        actors: list[Actor] = []
        all_dates = []
        doc_texts: dict[str, str] = {}  # doc_id -> canonical text, reused for product identification below
        litigation_caption_hits: dict[str, list[str]] = {}  # subject name -> doc_ids it was found in
        # (venue name, doc_id) pairs from utils.extract_court_venue - held
        # back from `actors` until after LLM roster validation below (see
        # where this is merged in) rather than added inline like the
        # plaintiff/defendant caption actors are, because a venue caption
        # ("SUPREME COURT OF THE STATE OF NEW YORK COUNTY OF NASSAU")
        # doesn't read like a person/party name and got silently dropped
        # by that validation pass when added the same way they are.
        court_venues: list[tuple[str, str]] = []

        # 1. Find all case documents, oldest filing first (see
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

        logger.info(f"  Found {len(pdf_files)} PDF files")
        logger.info(f"  Found {len(docling_files)} Docling files")
        logger.info(f"  Found {len(parsed_files)} parsed JSON files")
        logger.info(f"  Found {len(confirmation_files)} confirmation files")

        # 2. Extract metadata for each document
        doc_id_counter = 0
        pbar = tqdm(pdf_files, desc="Stage 1: metadata", unit="doc", file=sys.__stderr__)
        for pdf_path in pbar:
            doc_id = f"doc_{doc_id_counter:03d}"
            doc_id_counter += 1
            pbar.set_postfix_str(pdf_path.name)

            logger.info(f"\n→ Processing {pdf_path.name} (doc_id={doc_id})...")

            doc_metadata = DocumentMetadata(
                doc_id=doc_id,
                file_name=pdf_path.name,
            )

            # Extract PDF metadata (if enabled)
            if extract_from_pdfs:
                logger.info(f"  Extracting PDF metadata...")
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
                        logger.warning(f"  Warning: Failed to load Docling JSON: {e}")

                if docling_data is not None:
                    logger.info(f"  Extracting Docling metadata...")
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
                            logger.info(f"  Identifying document title (backend={backend})...")
                            title = identify_title(
                                text_excerpt=first_page_text[:3000],
                                candidates=docling_meta.title_candidates,
                                docling_title=docling_meta.title,
                                model=llm_model,
                                base_url=llm_base_url,
                            )
                            if title:
                                doc_metadata.document_title = title
                                logger.info(f"  Title: {title}")

                        # Extract CM/ECF (or NYSCEF) metadata if available
                        if docling_meta.cm_ecf:
                            doc_metadata.document_number = docling_meta.cm_ecf.document_number
                            doc_metadata.filing_date = docling_meta.cm_ecf.filing_date

                        # Some state e-filing systems (e.g. NY's NYSCEF)
                        # don't carry their document number in the CM/ECF-
                        # style header - fall back to the system's own stamp.
                        if not doc_metadata.document_number and docling_meta.document_signature:
                            doc_metadata.document_number = docling_meta.document_signature

                        # Court venue (e.g. "SUPREME COURT OF THE STATE OF
                        # NEW YORK COUNTY OF NASSAU") - a per-document
                        # signal used instead of the removed database
                        # lookup (see this stage's class docstring). Held
                        # in court_venues, merged into the roster after
                        # validation below - see that comment.
                        court_venue = extract_court_venue(
                            "\n".join(filter(None, [docling_meta.header, first_page_text]))
                        )
                        if court_venue:
                            court_venues.append((court_venue, doc_id))

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
                        logger.warning(f"  Warning: Failed to load confirmation metadata: {e}")
                        details = {}

                    if details:
                        logger.info(f"  Extracting confirmation metadata...")
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

            # Last-resort fallback: the PDF's own embedded creation
            # timestamp. Least authoritative of all the filing_date signals
            # here (it's when the PDF file was generated/scanned, not
            # necessarily when it was filed with the court) but it's a
            # per-document signal that's virtually always present, unlike
            # a case-level database lookup (removed - see this stage's
            # class docstring: a dead end for most cases). Appended last so
            # the fallback below only reaches it once every other
            # filing_date-typed source (CM/ECF, header, last page,
            # confirmation notice) has already had a chance.
            if doc_metadata.pdf_metadata and doc_metadata.pdf_metadata.created:
                extracted_date = ExtractedDate(
                    text=doc_metadata.pdf_metadata.created.strftime("%B %d, %Y"),
                    source="pdf_metadata",
                    type="filing_date",
                    doc_id=doc_id,
                )
                doc_metadata.extracted_dates.append(extracted_date)
                all_dates.append(extracted_date)

            # Fall back to whichever date got typed "filing_date" above when
            # there's no CM/ECF stamp to set doc_metadata.filing_date
            # directly (line ~254) - e.g. a court-reporter transcript or an
            # e-filing confirmation notice, neither of which extract_cm_ecf_
            # header recognizes. Without this, a document can carry a
            # filing_date-typed entry in extracted_dates (so it shows up
            # under "Dates found") while doc_metadata.filing_date - what the
            # browsers' "Filed"/"filing date" fields actually read - stays
            # None, reporting "no filing date" for the same document.
            # Confirmed on mdl-1954 doc_003: a settlement-statement
            # transcript headed "February 7, 2014" with no CM/ECF block.
            # Priority follows append order above: docling_header,
            # docling_last_page, confirmation, then pdf_metadata last.
            if not doc_metadata.filing_date:
                fallback_filing_date = next(
                    (d.text for d in doc_metadata.extracted_dates if d.type == "filing_date"),
                    None,
                )
                if fallback_filing_date:
                    doc_metadata.filing_date = fallback_filing_date

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

                # Deterministic backstop for a transcript's "APPEARANCES"
                # block (see utils.extract_appearances_block) - the LLM
                # comprehensive extraction pass below tends to under-
                # enumerate this dense, repeating list.
                for entry in extract_appearances_block(text):
                    self._add_appearances_actor(actors, entry, doc_id)

            # Extract dates from document
            if date_patterns and text:
                logger.info(f"  Extracting dates from text...")
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
                    logger.info(f"  Cites document number(s): {', '.join(cited)}")

            # Litigation-caption hint for product identification below (see
            # utils.find_litigation_captions): coordinated/MDL proceedings
            # often name the accused product right in the caption.
            if text:
                for subject in find_litigation_captions(text):
                    litigation_caption_hits.setdefault(subject, []).append(doc_id)

            documents.append(doc_metadata)

        logger.info(f"\n→ Extracted metadata for {len(documents)} documents")
        logger.info(f"→ Discovered {len(actors)} actors")
        logger.info(f"→ Found {len(all_dates)} dates")

        # 2.5. Resolve cross-document references now that every document's
        # own document_number (its "signature") is known, and build the
        # reverse index (referenced_by) so a document can be looked up by
        # what cites it, not just what it cites.
        logger.info("\n→ Resolving cross-document references...")
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
        logger.info(f"  Resolved {resolved_count} references to documents in this case")

        caption, court, case_type = self._load_case_context(case_id)

        # 2.75. Comprehensive LLM extraction (mandatory pass)
        # This extracts all parties, counsel, judges, products, etc. from
        # the first document's text with full contact information. This is
        # particularly important for MDL/coordinated cases where caption
        # parsing may not find individual parties.
        if config.get("comprehensive_llm_extraction", True) and documents:
            logger.info(f"\n→ Running comprehensive LLM actor extraction (backend={backend})...")

            # Determine which document(s) to extract from. -1 means "all
            # documents in the case" rather than capping to the first N -
            # each document gets its own LLM call below (not concatenated),
            # so cost scales with the docs actually present.
            doc_count = config.get("llm_extraction_doc_count", 1)
            docs_to_extract = documents if doc_count < 0 else documents[:doc_count]

            max_pages = config.get("llm_extraction_page_count", 2)
            max_chars = max_pages * 3000

            extractor = (
                extract_actors_from_document_with_nuextract
                if backend == "nuextract"
                else extract_actors_from_document_with_llm
            )

            total_llm_actors = 0
            for doc in docs_to_extract:
                doc_text = doc_texts.get(doc.doc_id)
                if not doc_text:
                    # Try to load from file
                    text_path = self.get_documents_dir(case_id) / f"{doc.doc_id}.txt"
                    if text_path.exists():
                        doc_text = self.load_text(text_path)
                    else:
                        doc_text = self._get_document_text(
                            self.get_documents_dir(case_id) / doc.file_name,
                            doc.doc_id
                        )

                if not doc_text:
                    continue

                # One call per document (not concatenated), so each actor
                # found can be attributed to the document it actually came
                # from instead of always the first document.
                llm_actors = extractor(
                    doc_text[:max_chars],
                    model=llm_model,
                    base_url=llm_base_url,
                    caption=caption,
                    court=court,
                    case_type=case_type,
                    page_count=max_pages,
                )
                total_llm_actors += len(llm_actors)

                # Merge LLM-extracted actors with existing roster
                for llm_actor in llm_actors:
                    # Map counsel roles back to standard roles
                    if llm_actor.role == "counsel":
                        # Try to infer if plaintiff or defendant counsel
                        # For now, just use "counsel"
                        role = "counsel"
                    else:
                        role = llm_actor.role

                    # Check if already exists
                    existing_actor = None
                    for actor in actors:
                        if actor.role == role and names_match(actor.canonical_name, llm_actor.canonical_name):
                            existing_actor = actor
                            break

                    if existing_actor:
                        # Merge: add this document's doc_id, enrich with LLM data
                        if doc.doc_id not in existing_actor.doc_ids:
                            existing_actor.doc_ids.append(doc.doc_id)
                        # Enrich with LLM-extracted details
                        if llm_actor.email and not existing_actor.email:
                            existing_actor.email = llm_actor.email
                        if llm_actor.phone and not existing_actor.phone:
                            existing_actor.phone = llm_actor.phone
                        if llm_actor.address and not existing_actor.address:
                            existing_actor.address = llm_actor.address
                        if llm_actor.title and not existing_actor.title:
                            existing_actor.title = llm_actor.title
                        if llm_actor.organization and not existing_actor.organization:
                            existing_actor.organization = llm_actor.organization
                        if llm_actor.location and not existing_actor.location:
                            existing_actor.location = llm_actor.location
                        if llm_actor.case_number and not existing_actor.case_number:
                            existing_actor.case_number = llm_actor.case_number
                    else:
                        # New actor: add to roster, attributed to this document
                        llm_actor.doc_ids = [doc.doc_id]
                        actors.append(llm_actor)

            logger.info(f"  LLM extracted {total_llm_actors} actor mention(s) across {len(docs_to_extract)} document(s)")
            logger.info(f"  Total roster after LLM extraction: {len(actors)} actors")

        # 3. Validate the discovered actor roster with an LLM (optional)
        if config.get("validate_actors_with_llm", True) and actors:
            logger.info(f"\n→ Validating actor roster with LLM (backend={backend})...")
            validator = validate_actors_with_nuextract if backend == "nuextract" else validate_actors_with_llm
            actors = validator(
                actors,
                caption=caption,
                court=court,
                model=llm_model,
                base_url=llm_base_url,
            )
            logger.info(f"  Roster after validation: {len(actors)} actors")

        # Merge in court venues discovered above (see court_venues) now
        # that validation has run - added after, not before, so the
        # validator's actor-vs-noise judgment (tuned for person/party
        # names) doesn't drop a venue caption for not reading like one.
        for court_venue, doc_id in court_venues:
            self._add_actor(actors, court_venue, "court_clerk", "caption", doc_id)

        # 3.5. Identify the accused product(s) - the medical substance/drug/
        # medical device/cosmetic product the plaintiff blames for harm,
        # attributed to a defendant, if determinable. No fixed textual
        # format to regex an arbitrary product name from (unlike a caption
        # block or a citation), so this is LLM-first: caption/court/case
        # type, any "In Re ... Litigation" hits from step 3, and an excerpt
        # of whichever document reads most like it describes the product/
        # harm (see _select_product_context_document).
        products: list[Actor] = []
        if config.get("extract_products", True):
            logger.info("\n→ Identifying accused product(s)...")
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
                logger.info(f"  {result['product_type']}: {result['name']}{attribution}")

            if not products:
                logger.info("  No accused product identified")

        products_artifact = ActorsArtifact(case_id=case_id, actors=products)
        self.save_artifact(case_id, "products.json", products_artifact)

        # 4. Fill in generic role placeholders for roles with no named
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

        # 5. Create files_scan artifact
        files_scan = FilesScan(
            case_id=case_id,
            scan_timestamp=datetime.now(),
            documents=documents,
            all_dates=all_dates,
        )

        self.save_artifact(case_id, "files_scan.json", files_scan)

        # 6. Generate GLiNER config from the combined actor + product roster
        logger.info("\n→ Generating GLiNER configuration...")
        combined_roster = ActorsArtifact(case_id=case_id, actors=actors + products)
        gliner_config = self._generate_gliner_config(combined_roster, config)
        self.save_artifact(case_id, "gliner_config.json", gliner_config)

        # Ollama keeps llm_model resident in GPU memory for a few minutes
        # after our last call above - release it now rather than leaving it
        # to compete with Stage 2/GLiNER's own model for VRAM right after.
        if backend == "ollama":
            unload_ollama_model(llm_model, llm_base_url)

        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 1 Complete!")
        logger.info(f"{'='*60}\n")

    def validate_inputs(self, case_id: str) -> bool:
        """Check if case directory exists.

        Args:
            case_id: Case identifier

        Returns:
            True if case directory exists
        """
        case_dir = self.get_case_dir(case_id)
        if not case_dir.exists():
            logger.error(f"Error: Case directory not found: {case_dir}")
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
            source: Where this was discovered ('caption', 'confirmation', 'llm', ...)
            doc_id: Document this instance was found in, if any
        """
        name = name.strip()
        if not name:
            return

        for actor in actors:
            if actor.role == role and names_match(actor.canonical_name, name):
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

    def _add_appearances_actor(self, actors: list[Actor], entry: dict, doc_id: str) -> None:
        """Merge one attorney parsed from a transcript's APPEARANCES block
        (see utils.extract_appearances_block) into the roster, enriching an
        existing same-person entry (e.g. one the LLM pass already found)
        rather than duplicating it.

        Args:
            actors: Roster to add to (mutated in place)
            entry: One dict from extract_appearances_block
            doc_id: Document this attorney was found in
        """
        name = (entry.get("canonical_name") or "").strip()
        if not name:
            return

        existing_actor = None
        for actor in actors:
            if actor.role == "counsel" and names_match(actor.canonical_name, name):
                existing_actor = actor
                break

        if existing_actor:
            if doc_id not in existing_actor.doc_ids:
                existing_actor.doc_ids.append(doc_id)
            if entry.get("email") and not existing_actor.email:
                existing_actor.email = entry["email"]
            if entry.get("phone") and not existing_actor.phone:
                existing_actor.phone = entry["phone"]
            if entry.get("address") and not existing_actor.address:
                existing_actor.address = entry["address"]
            if entry.get("organization") and not existing_actor.organization:
                existing_actor.organization = entry["organization"]
            if entry.get("title") and not existing_actor.title:
                existing_actor.title = entry["title"]
        else:
            actors.append(Actor(
                canonical_name=name,
                role="counsel",
                is_named=True,
                source="appearances_block",
                aliases=find_party_aliases(name),
                doc_ids=[doc_id],
                organization=entry.get("organization"),
                title=entry.get("title"),
                email=entry.get("email"),
                phone=entry.get("phone"),
                address=entry.get("address"),
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
                    sort_date = parse_date_loosely(docling_meta.cm_ecf.filing_date)
            except Exception:
                pass

        if sort_date is None:
            pdf_meta = extract_pdf_metadata(pdf_path)
            sort_date = pdf_meta.get("created")

        return (sort_date is None, sort_date or datetime.max, pdf_path.name)

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
                header, first page, and last page text

        Returns:
            Tuple of (Docling metadata, timestamps found in the header/
            first page/last page, first-page text) - the text is returned
            separately since callers also use it as LLM context for title
            identification (see identify_document_title_with_llm), not
            just for date/signature extraction.
        """
        try:
            texts = docling_data.get("texts", [])

            def item_page_no(item: dict) -> int | None:
                prov = item.get("prov") or []
                return prov[0].get("page_no") if prov else None

            first_page_items = [item for item in texts if item_page_no(item) == 1]

            # A filing/signing/certificate-of-service date can land on the
            # last page instead of (or in addition to) the header - e.g. a
            # closing signature block. Skip re-scanning page 1 as "last
            # page" too on a single-page document.
            page_numbers = [n for item in texts if (n := item_page_no(item)) is not None]
            last_page_no = max(page_numbers) if page_numbers else None
            last_page_items = (
                [item for item in texts if item_page_no(item) == last_page_no]
                if last_page_no is not None and last_page_no != 1
                else []
            )

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

            last_page_text = "\n".join(
                (item.get("text") or "").strip()
                for item in last_page_items
                if (item.get("text") or "").strip()
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

                # Lower priority than the header (appended above) in the
                # filing_date fallback below - only used when the header
                # itself carried no dated stamp.
                if last_page_text:
                    for date_text, start, end in extract_dates_from_text(last_page_text, date_patterns):
                        extracted_dates.append(ExtractedDate(
                            text=date_text,
                            source="docling_last_page",
                            type="filing_date",
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
            logger.warning(f"  Warning: Failed to extract Docling metadata: {e}")
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
            logger.warning(f"  Warning: Failed to parse caption block: {e}")
            return {"plaintiffs": [], "defendants": []}

    def _get_document_text(self, pdf_path: Path, doc_id: str) -> str:
        """Get document text for date extraction.

        Docling-only, not the legacy parsed JSON sidecar (see
        BaseStage.load_document_text's docstring for why: it can silently
        drop entire pages from a multi-page PDF).

        Args:
            pdf_path: Path to PDF file
            doc_id: Document ID

        Returns:
            Document text
        """
        docling_path = get_docling_dir(pdf_path) / f"{pdf_path.stem}.docling.json"
        if docling_path.exists():
            try:
                docling_data = self.load_json(docling_path)
                if "texts" in docling_data:
                    return "\n".join(item.get("text", "") for item in docling_data["texts"])
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
