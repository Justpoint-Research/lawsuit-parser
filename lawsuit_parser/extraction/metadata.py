"""Stage 1: Document metadata extraction using NuExtract3.

Extracts metadata from caption and signature blocks only (not full document).
All date fields are surface strings - no parsing.
"""

import logging
from typing import Literal

from .models import NuExtractClient, ExtractionError
from .schemas import (
    DocumentMetadata,
    MetadataArtifact,
    PartySeed,
    SegmentsArtifact,
    Span,
)
from .store import ArtifactStore
from .segments import extract_cmecf_header

logger = logging.getLogger(__name__)


# Mapping from model output to PartySeed.role enum
ROLE_MAPPING = {
    "plaintiff": "plaintiff",
    "plaintiffs": "plaintiff",
    "defendant": "defendant",
    "defendants": "defendant",
    "intervenor": "intervenor",
    "intervenors": "intervenor",
    "third party": "third_party",
    "third-party": "third_party",
    "amicus": "amicus",
    "amicus curiae": "amicus",
    "counsel": "counsel",
    "attorney": "counsel",
    "court": "court",
    "petitioner": "plaintiff",  # Map to plaintiff for now
    "respondent": "defendant",  # Map to defendant for now
    "appellant": "plaintiff",
    "appellee": "defendant",
    "movant": "plaintiff",
}


def _get_str(d: dict, key: str) -> str:
    """Read a string field from a NuExtract result, tolerating explicit nulls.

    NuExtract returns JSON `null` (not an empty string) for fields it couldn't
    find in the text, so a plain `d.get(key, "")` still yields None.
    """
    value = d.get(key)
    return value.strip() if isinstance(value, str) else ""


def normalize_role(role_str: str | None) -> Literal[
    "plaintiff", "defendant", "intervenor", "third_party",
    "amicus", "counsel", "court", "other"
]:
    """Normalize a role string to the enum.

    Args:
        role_str: Raw role string from model

    Returns:
        Normalized role
    """
    if not role_str:
        return "other"

    role_lower = role_str.lower().strip()
    normalized = ROLE_MAPPING.get(role_lower, "other")

    if normalized == "other":
        logger.warning(f"Unmapped role: {role_str}")

    return normalized


def find_span_in_text(
    text: str,
    target: str,
    doc_id: str,
    base_offset: int = 0,
) -> Span | None:
    """Find a target string in text and return a Span.

    Args:
        text: Text to search in
        target: Target string to find
        doc_id: Document ID for the span
        base_offset: Base character offset to add (for segment offsets)

    Returns:
        Span if found uniquely, None if not found or ambiguous
    """
    if not target:
        return None

    # Try exact match first
    start = text.find(target)
    if start == -1:
        return None

    # Check for uniqueness
    second = text.find(target, start + 1)
    if second != -1:
        logger.warning(f"Ambiguous match for '{target}' - multiple occurrences found")
        return None

    end = start + len(target)

    return Span(
        doc_id=doc_id,
        char_start=base_offset + start,
        char_end=base_offset + end,
        text=target,
    )


def extract_metadata(
    case_id: str,
    segments: SegmentsArtifact,
    client: NuExtractClient,
    store: ArtifactStore,
) -> MetadataArtifact:
    """Extract document metadata using NuExtract3.

    This is Stage 1. It processes only the caption and signature blocks
    to extract metadata, not the full document.

    Args:
        case_id: Case identifier
        segments: Segmentation artifact from stage 0
        client: NuExtract3 client
        store: Artifact store

    Returns:
        MetadataArtifact with extracted metadata
    """
    counters = {
        "documents_processed": 0,
        "null_fields": {},
        "span_mapping_failures": 0,
        "header_model_disagreements": 0,
    }

    documents = []

    # Group segments by document
    docs_segments = {}
    for seg in segments.segments:
        if seg.doc_id not in docs_segments:
            docs_segments[seg.doc_id] = []
        docs_segments[seg.doc_id].append(seg)

    # Process each document
    for doc_id in docs_segments:
        canonical_text = store.read_canonical_text(doc_id)

        # Get caption and signature blocks
        caption_segs = [
            s for s in docs_segments[doc_id] if s.section_type == "caption"
        ]
        signature_segs = [
            s for s in docs_segments[doc_id] if s.section_type == "signature"
        ]

        # Build input text from caption + signature
        input_parts = []
        for seg in caption_segs:
            seg_text = canonical_text[seg.char_start : seg.char_end]
            input_parts.append(seg_text)

        for seg in signature_segs:
            seg_text = canonical_text[seg.char_start : seg.char_end]
            input_parts.append(seg_text)

        input_text = "\n\n".join(input_parts)

        # NuExtract template
        template = {
            "court": "",
            "case_number": "",
            "document_title": "",
            "document_number": "",
            "filing_date": "",
            "signature_date": "",
            "filed_by": "",
            "parties": [
                {
                    "name": "",
                    "role": "",
                    "short_name": "",
                }
            ],
        }

        # Call NuExtract
        try:
            result = client.extract(input_text, template)
        except ExtractionError as e:
            logger.error(f"Extraction failed for {doc_id}: {e}")
            store.log_error("01_metadata", "extraction_error", {
                "doc_id": doc_id,
                "error": str(e),
            })
            counters["documents_processed"] += 1
            # Create empty metadata
            documents.append(DocumentMetadata(
                doc_id=doc_id,
                parties=[],
                source_spans={},
            ))
            continue

        # Map fields to spans
        source_spans = {}

        # Court
        court_str = _get_str(result, "court")
        court_span = None
        if court_str:
            court_span = find_span_in_text(input_text, court_str, doc_id, 0)
            if court_span:
                source_spans["court"] = court_span
            else:
                counters["span_mapping_failures"] += 1
                court_str = None

        # Case number
        case_number_str = _get_str(result, "case_number")
        case_number_span = None
        if case_number_str:
            case_number_span = find_span_in_text(input_text, case_number_str, doc_id, 0)
            if case_number_span:
                source_spans["case_number"] = case_number_span
            else:
                counters["span_mapping_failures"] += 1
                case_number_str = None

        # Document title
        doc_title_str = _get_str(result, "document_title")
        doc_title_span = None
        if doc_title_str:
            doc_title_span = find_span_in_text(input_text, doc_title_str, doc_id, 0)
            if doc_title_span:
                source_spans["document_title"] = doc_title_span
            else:
                counters["span_mapping_failures"] += 1
                doc_title_str = None

        # Document number
        doc_number_str = _get_str(result, "document_number")
        doc_number_span = None
        if doc_number_str:
            doc_number_span = find_span_in_text(input_text, doc_number_str, doc_id, 0)
            if doc_number_span:
                source_spans["document_number"] = doc_number_span
            else:
                counters["span_mapping_failures"] += 1
                doc_number_str = None

        # Filing date
        filing_date_str = _get_str(result, "filing_date")
        filing_date_span = None
        if filing_date_str:
            filing_date_span = find_span_in_text(input_text, filing_date_str, doc_id, 0)
            if filing_date_span:
                source_spans["filing_date"] = filing_date_span
            else:
                counters["span_mapping_failures"] += 1
                filing_date_str = None

        # Signature date
        signature_date_str = _get_str(result, "signature_date")
        signature_date_span = None
        if signature_date_str:
            signature_date_span = find_span_in_text(
                input_text, signature_date_str, doc_id, 0
            )
            if signature_date_span:
                source_spans["signature_date"] = signature_date_span
            else:
                counters["span_mapping_failures"] += 1
                signature_date_str = None

        # Filed by
        filed_by_str = _get_str(result, "filed_by")
        filed_by_span = None
        if filed_by_str:
            filed_by_span = find_span_in_text(input_text, filed_by_str, doc_id, 0)
            if filed_by_span:
                source_spans["filed_by"] = filed_by_span
            else:
                counters["span_mapping_failures"] += 1
                filed_by_str = None

        # Parties
        party_seeds = []
        parties_list = result.get("parties", [])
        if isinstance(parties_list, list):
            for party_dict in parties_list:
                name = _get_str(party_dict, "name")
                role = _get_str(party_dict, "role")
                short_name = _get_str(party_dict, "short_name")

                if name:
                    normalized_role = normalize_role(role)
                    party_seeds.append(PartySeed(
                        name=name,
                        role=normalized_role,
                        short_name=short_name if short_name else None,
                    ))

        # Check CM/ECF header and prefer it over model
        cmecf_info = extract_cmecf_header(canonical_text)
        if cmecf_info:
            # Prefer CM/ECF header dates
            cmecf_filing_date = cmecf_info.get("filing_date")
            if cmecf_filing_date and filing_date_str:
                if cmecf_filing_date != filing_date_str:
                    logger.warning(
                        f"{doc_id}: CM/ECF date '{cmecf_filing_date}' != "
                        f"model date '{filing_date_str}'"
                    )
                    counters["header_model_disagreements"] += 1

            # Use CM/ECF filing date if we don't have one from model
            if cmecf_filing_date and not filing_date_str:
                filing_date_str = cmecf_filing_date

            # Use CM/ECF case/doc numbers if not from model
            if cmecf_info.get("case_number") and not case_number_str:
                case_number_str = cmecf_info["case_number"]
            if cmecf_info.get("document_number") and not doc_number_str:
                doc_number_str = cmecf_info["document_number"]

        # Determine DCT (document creation time) - prefer filing date
        dct_raw = None
        if filing_date_str:
            dct_raw = filing_date_str
        elif signature_date_str:
            dct_raw = signature_date_str

        # Track null fields
        for field in [
            "court",
            "case_number",
            "document_title",
            "document_number",
            "filing_date",
            "signature_date",
            "filed_by",
        ]:
            value = locals().get(f"{field}_str")
            if not value:
                counters["null_fields"][field] = (
                    counters["null_fields"].get(field, 0) + 1
                )

        # Create metadata
        doc_metadata = DocumentMetadata(
            doc_id=doc_id,
            court=court_str if court_str else None,
            case_number=case_number_str if case_number_str else None,
            document_title=doc_title_str if doc_title_str else None,
            document_number=doc_number_str if doc_number_str else None,
            filing_date_raw=filing_date_str if filing_date_str else None,
            signature_date_raw=signature_date_str if signature_date_str else None,
            dct_raw=dct_raw,
            filed_by=filed_by_str if filed_by_str else None,
            parties=party_seeds,
            source_spans=source_spans,
        )

        documents.append(doc_metadata)
        counters["documents_processed"] += 1

    logger.info(f"Stage 1 counters: {counters}")

    return MetadataArtifact(
        case_id=case_id,
        documents=documents,
    )
