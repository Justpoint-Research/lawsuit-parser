"""Stage 0: Canonical text + segmentation.

Whitespace policy (applied once, irreversible):
- Normalize \r\n → \n
- Collapse runs of 3+ blank lines to exactly 2
- Strip trailing whitespace per line
- Preserve everything else
- Never re-tokenize or re-normalize after this point

Every offset in the entire system indexes into the canonical text produced here.
"""

import logging
import re
from pathlib import Path
from typing import Any

from docling_core.types.doc import DocItemLabel

from .schemas import Segment, SegmentsArtifact, DocumentRef, Span
from .store import ArtifactStore

logger = logging.getLogger(__name__)


def normalize_whitespace(text: str) -> str:
    """Apply the canonical whitespace normalization policy.

    This is applied exactly once to produce canonical text.
    The policy is:
    - Normalize \\r\\n → \\n
    - Collapse runs of 3+ blank lines to exactly 2 blank lines
    - Strip trailing whitespace per line
    - Strip trailing newline at end of document
    - Preserve everything else

    Args:
        text: Raw text

    Returns:
        Normalized text

    This function is idempotent: normalize(normalize(x)) == normalize(x)
    """
    # Step 1: Normalize line endings
    text = text.replace("\r\n", "\n")

    # Step 2: Strip trailing whitespace per line
    lines = text.split("\n")
    lines = [line.rstrip() for line in lines]

    # Step 3: Collapse runs of 3+ blank lines to exactly 2
    # Rejoin and use regex to collapse
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Step 4: Strip final trailing newline
    text = text.rstrip("\n")

    return text


def extract_cmecf_header(text: str) -> dict[str, str] | None:
    """Extract CM/ECF page header information if present.

    CM/ECF headers follow the pattern:
    Case <case_no>  Document <n>  Filed <date>  Page <p> of <total>

    Args:
        text: Document text (typically from first page)

    Returns:
        Dict with case_number, document_number, filing_date, or None if not found

    The filing date is a surface string, not parsed.
    """
    # Pattern for CM/ECF header
    # Example: "Case 1:19-cv-01234-ABC Document 1 Filed 01/15/2019 Page 1 of 25"
    pattern = r"Case\s+([^\s]+)\s+Document\s+(\d+)\s+Filed\s+([^\s]+(?:\s+[^\s]+)?)\s+Page"

    match = re.search(pattern, text[:1000])  # Check first 1000 chars
    if match:
        return {
            "case_number": match.group(1),
            "document_number": match.group(2),
            "filing_date": match.group(3),
        }

    return None


def detect_paragraph_label(text: str) -> str | None:
    """Detect numbered paragraph label at start of text.

    Legal documents use patterns like:
    - "42. Text..."
    - "¶ 42. Text..."
    - "II.B. Text..."
    - "(a) Text..."

    Args:
        text: Paragraph text

    Returns:
        Label string if detected, None otherwise
    """
    text = text.strip()

    # Pattern for various numbering styles
    patterns = [
        r"^(¶\s*\d+\.?)\s",  # ¶ 42.
        r"^(\d+\.)\s",  # 42.
        r"^([IVX]+\.[A-Z]\.)\s",  # II.B.
        r"^(\([a-z0-9]+\))\s",  # (a) or (1)
    ]

    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group(1)

    return None


def build_segments(
    case_id: str,
    docling_docs: list[Any],
    store: ArtifactStore,
) -> SegmentsArtifact:
    """Build canonical text and segments from Docling documents.

    This is Stage 0: the foundation of the entire pipeline.

    Args:
        case_id: Case identifier
        docling_docs: List of Docling Document objects
        store: Artifact store

    Returns:
        SegmentsArtifact with all segments and document references

    The canonical text is written to disk and never modified.
    All subsequent stages read from disk.
    """
    counters = {
        "documents": 0,
        "segments": 0,
        "section_types": {},
        "cmecf_hit_rate": 0,
        "numbered_paragraphs": 0,
    }

    all_segments = []
    document_refs = []

    for doc_idx, docling_doc in enumerate(docling_docs):
        # Generate doc_id
        doc_id = f"doc_{doc_idx:03d}"
        if hasattr(docling_doc, "name") and docling_doc.name:
            file_name = docling_doc.name
        else:
            file_name = f"document_{doc_idx}.pdf"

        document_refs.append(DocumentRef(doc_id=doc_id, file_name=file_name))
        counters["documents"] += 1

        # Extract all text to build canonical text
        text_parts = []
        for item, level in docling_doc.iterate_items():
            if hasattr(item, "text") and item.text:
                text = item.text.strip()
                if text:
                    text_parts.append(text)

        # Build and normalize canonical text
        raw_text = "\n\n".join(text_parts)
        canonical_text = normalize_whitespace(raw_text)

        # Write canonical text to disk (single source of truth)
        store.write_canonical_text(doc_id, canonical_text)

        # Check for CM/ECF header
        cmecf_info = extract_cmecf_header(canonical_text)
        if cmecf_info:
            counters["cmecf_hit_rate"] += 1
            logger.info(f"{doc_id}: Found CM/ECF header: {cmecf_info}")

        # Build segments
        seg_counter = 0
        char_offset = 0
        in_caption = True  # First sections are caption until we hit numbered paras

        for item, level in docling_doc.iterate_items():
            if not hasattr(item, "text") or not item.text:
                continue

            item_text = item.text.strip()
            if not item_text:
                continue

            # Find this text in canonical text
            # Search from current offset
            found_start = canonical_text.find(item_text, char_offset)
            if found_start == -1:
                # Text not found - might be due to normalization
                logger.warning(
                    f"{doc_id}: Could not locate segment text in canonical text"
                )
                continue

            found_end = found_start + len(item_text)

            # Determine page number if available
            page_num = None
            if hasattr(item, "prov") and item.prov:
                for prov in item.prov:
                    if hasattr(prov, "page_no"):
                        page_num = prov.page_no
                        break

            # Detect section type and paragraph label
            label = getattr(item, "label", None)
            para_label = detect_paragraph_label(item_text)

            # Determine section type
            if label == DocItemLabel.TITLE:
                section_type = "caption"
            elif label == DocItemLabel.PAGE_HEADER or label == DocItemLabel.PAGE_FOOTER:
                section_type = "other"
            elif "signature" in item_text.lower()[:50]:
                section_type = "signature"
                in_caption = False
            elif "certificate of service" in item_text.lower():
                section_type = "certificate_of_service"
                in_caption = False
            elif "exhibit" in item_text.lower()[:50]:
                section_type = "exhibit"
                in_caption = False
            elif para_label:
                section_type = "body"
                in_caption = False
                counters["numbered_paragraphs"] += 1
            elif label == DocItemLabel.SECTION_HEADER:
                section_type = "heading"
                in_caption = False
            elif in_caption:
                section_type = "caption"
            else:
                section_type = "body"

            # Track section types
            counters["section_types"][section_type] = (
                counters["section_types"].get(section_type, 0) + 1
            )

            # Create segment
            seg_id = f"{doc_id}_seg_{seg_counter:04d}"
            segment = Segment(
                seg_id=seg_id,
                doc_id=doc_id,
                page=page_num,
                para_label=para_label,
                section_type=section_type,
                char_start=found_start,
                char_end=found_end,
            )

            all_segments.append(segment)
            seg_counter += 1
            counters["segments"] += 1

            # Update char offset for next search
            char_offset = found_end

    # Validate segments
    for segment in all_segments:
        canonical_text = store.read_canonical_text(segment.doc_id)
        extracted = canonical_text[segment.char_start : segment.char_end]
        if not extracted:
            logger.error(
                f"Empty segment {segment.seg_id} "
                f"at [{segment.char_start}:{segment.char_end}]"
            )

    logger.info(f"Stage 0 counters: {counters}")

    artifact = SegmentsArtifact(
        case_id=case_id,
        segments=all_segments,
        documents=document_refs,
    )

    return artifact
