"""Utility functions for event extraction pipeline."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pypdfium2 as pdfium
from nltk.tokenize.punkt import PunktParameters, PunktSentenceTokenizer

logger = logging.getLogger(__name__)


def parse_date_loosely(text: str) -> datetime | None:
    """Best-effort parse of a raw date string (e.g. "03/03/2026", "August
    25, 2026") into a real datetime. Used both for Stage 1's document sort
    order and Stage 4's date parsing - the stored ExtractedDate.text values
    themselves remain unparsed surface strings regardless."""
    try:
        ts = pd.to_datetime(text, errors="coerce")
    except Exception:
        return None
    return None if pd.isna(ts) else ts.to_pydatetime()


def extract_dates_from_text(text: str, patterns: list[str]) -> list[tuple[str, int, int]]:
    """Extract dates from text using regex patterns.

    Args:
        text: Text to search for dates
        patterns: List of regex patterns for date matching

    Returns:
        List of (date_text, start_offset, end_offset) tuples
    """
    dates = []

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            dates.append((match.group(0), match.start(), match.end()))

    # Remove duplicates while preserving order
    seen = set()
    unique_dates = []
    for date_text, start, end in dates:
        if (date_text, start, end) not in seen:
            seen.add((date_text, start, end))
            unique_dates.append((date_text, start, end))

    return unique_dates


def extract_cm_ecf_header(text: str) -> dict[str, str] | None:
    """Extract filing header information from a document's header text.

    Supports two formats seen in practice:
    - Federal CM/ECF: "Case 3:24-cv-12345 Document 1 Filed 01/15/2024 Page 1 of 42"
    - NY State e-filing (NYSCEF): "FILED: NEW YORK COUNTY CLERK 02/26/2026 10:40 AM"

    Args:
        text: Document header text (typically the page_header of the first page)

    Returns:
        Dictionary with extracted fields, or None if no known header format found
    """
    pattern = r"Case\s+([^\s]+)\s+Document\s+(\d+)\s+Filed\s+([\d/]+)(?:\s+Page\s+(\d+)\s+of\s+(\d+))?"

    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        case_number, doc_number, filing_date, page_num, total_pages = match.groups()

        result = {
            "case_number": case_number,
            "document_number": doc_number,
            "filing_date": filing_date,
        }

        if page_num and total_pages:
            result["page_info"] = f"Page {page_num} of {total_pages}"

        return result

    # State e-filing systems that stamp "FILED: <COUNTY> COUNTY CLERK <date>" -
    # seen from NY's NYSCEF; add more states' header formats here as needed.
    county_clerk_efiling_pattern = r"FILED:\s*.+?COUNTY\s+CLERK\s+(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?"

    match = re.search(county_clerk_efiling_pattern, text, re.IGNORECASE)
    if match:
        filing_date, filing_time = match.groups()
        return {
            "filing_date": f"{filing_date} {filing_time}" if filing_time else filing_date,
        }

    return None


CONFIRMATION_TIMESTAMP_PATTERN = re.compile(
    r"received an electronic filing on\s+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)
CONFIRMATION_JUDGE_PATTERN = re.compile(r"Assigned Judge:\s*(.+)", re.IGNORECASE)
CONFIRMATION_FILER_PATTERN = re.compile(
    r"^([A-Z][A-Za-z.,'\-\s]*?)\s*\|\s*([\w.+-]+@[\w.-]+\.\w+)\s*\|\s*(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})"
)
# Matches both "Hon. Nancy T. Sunshine, Kings County Clerk..." and
# "Maureen O'Connell, Nassau County Clerk..." (the "Hon." title is optional).
CONFIRMATION_CLERK_PATTERN = re.compile(
    r"^(?:Hon\.\s+)?([A-Z][A-Za-z.'\-\s]+?),\s*[\w\s]*?County Clerk"
)


def extract_confirmation_details(paragraphs: list[str]) -> dict[str, str]:
    """Extract filer, judge, and clerk details from an e-filing confirmation notice.

    NYSCEF confirmation notices report, one per paragraph, the filing
    timestamp ("...received an electronic filing on <date> <time>..."), the
    assigned judge ("Assigned Judge: <name>"), the filing user as
    "<NAME> | <email> | <phone>", and the county clerk ("Hon. <name>, <County>
    County Clerk..."). Matching per-paragraph, rather than on text joined
    across paragraphs, keeps the name captures from bleeding into the
    previous or next paragraph.

    Args:
        paragraphs: Confirmation notice paragraphs, as parsed from the
            confirmation PDF's JSON (same shape as a regular document's)

    Returns:
        Dictionary with any of "notice_timestamp", "assigned_judge",
        "filer_name", "filer_email", "filer_phone", "court_clerk" that
        were found
    """
    result: dict[str, str] = {}

    for paragraph in paragraphs:
        if "notice_timestamp" not in result:
            match = CONFIRMATION_TIMESTAMP_PATTERN.search(paragraph)
            if match:
                result["notice_timestamp"] = match.group(1)

        if "assigned_judge" not in result:
            match = CONFIRMATION_JUDGE_PATTERN.search(paragraph)
            if match:
                judge = match.group(1).strip()
                if judge and judge.lower() != "none recorded":
                    result["assigned_judge"] = judge

        if "filer_name" not in result:
            match = CONFIRMATION_FILER_PATTERN.search(paragraph.strip())
            if match:
                name, email, phone = match.groups()
                result["filer_name"] = name.strip().strip(",")
                result["filer_email"] = email.strip()
                result["filer_phone"] = phone.strip()

        if "court_clerk" not in result:
            match = CONFIRMATION_CLERK_PATTERN.search(paragraph.strip())
            if match:
                result["court_clerk"] = match.group(1).strip()

    return result


# ============================================================================
# Case caption parsing (plaintiff/defendant identification from document text)
# ============================================================================

_CAPTION_BOILERPLATE_SUBSTRINGS = (
    "supreme court", "county of", "nyscef doc", "index no", "filed:",
    "received nyscef", "summons", "complaint", "basis of venue",
    "designates", "docket", "case no", "civil action", "district court",
    "united states",
    # Document-type headings NY filings commonly interleave between the
    # party list and the closing role marker (e.g. "...defendant list...
    # Index No.: X / STIPULATION / Defendants.") - not party names.
    "stipulation", "affidavit", "affirmation", "notice of motion",
    "memorandum of law", "order to show cause", "certificate of merit",
    "notice of appearance", "cross motion", "bill of particulars",
    "verified answer", "verified complaint", "judicial intervention",
    "preliminary conference", "so ordered", "notice of entry",
)
_CAPTION_DASH_LINE_PATTERN = re.compile(r"^-{3,}x?$", re.IGNORECASE)
_CAPTION_ADDRESS_PATTERN = re.compile(r"\b[A-Z]{2}\s*\d{5}\b")
_CAPTION_STREET_START_PATTERN = re.compile(r"^\d+[\s\-]")
_CAPTION_ROLE_LINE_PATTERN = re.compile(
    r"^(plaintiffs?|defendants?|petitioners?|respondents?)"
    r"(\s*\(s\))?"
    r"(\s*/\s*(plaintiffs?|defendants?|petitioners?|respondents?)(\s*\(s\))?)?"
    r"\s*[.,]?$",
    re.IGNORECASE,
)
_CAPTION_PLAINTIFF_ROLE_PATTERN = re.compile(r"plaintiff|petitioner", re.IGNORECASE)
_CAPTION_DEFENDANT_ROLE_PATTERN = re.compile(r"defendant|respondent", re.IGNORECASE)
_CAPTION_SEPARATOR_PATTERN = re.compile(r"^-?\s*(against|vs?\.)\s*-?", re.IGNORECASE)


def _is_caption_boilerplate(line: str) -> bool:
    if _CAPTION_DASH_LINE_PATTERN.match(line.strip()):
        return True
    low = line.lower()
    return any(s in low for s in _CAPTION_BOILERPLATE_SUBSTRINGS)


def _is_caption_address(line: str) -> bool:
    return bool(_CAPTION_ADDRESS_PATTERN.search(line)) or bool(_CAPTION_STREET_START_PATTERN.match(line.strip()))


def _split_caption_names(lines: list[str]) -> list[str]:
    """Split caption name lines (semicolon/'and'-joined co-parties) into
    individual party names, stripping "et al." and trailing punctuation."""
    names = []
    for line in lines:
        for part in re.split(r";", line):
            part = part.strip()
            part = re.sub(r"^and\s+", "", part, flags=re.IGNORECASE)
            part = re.sub(r"\s*,?\s*et\.?\s*al\.?$", "", part, flags=re.IGNORECASE)
            part = part.strip(" ,.")
            if part and re.search(r"[A-Za-z]{2,}", part):
                names.append(part)
    return names


def parse_caption_block(lines: list[str]) -> dict[str, list[str]]:
    """Parse plaintiff/defendant names out of a document's case-caption block.

    NY state (and similar) pleadings open with a caption: plaintiff
    name(s), a role marker line ("Plaintiff/Petitioner," or "Plaintiff(s)"),
    a separator ("-against-", "v.", "vs."), defendant name(s), and a closing
    role marker ("Defendant/Respondent." or "Defendant(s)"). This looks for
    that shape in the flattened, in-order text lines of a document's first
    page (as Docling emits them) and pulls out the names on both sides -
    tolerating the boilerplate (docket headers, venue text, addresses) that
    NYSCEF filings interleave around the caption.

    Args:
        lines: First-page text lines, in document order (e.g. from a
            Docling export's `texts`, filtered to page 1)

    Returns:
        Dict with "plaintiffs" and "defendants" name lists (either may be
        empty if no caption block was found or recognized)
    """
    p_idx = next(
        (i for i, line in enumerate(lines)
         if _CAPTION_ROLE_LINE_PATTERN.match(line.strip()) and _CAPTION_PLAINTIFF_ROLE_PATTERN.search(line)),
        None,
    )
    if p_idx is None:
        return {"plaintiffs": [], "defendants": []}

    # Anchor the plaintiff-name search to just after the last boilerplate
    # line before p_idx (typically the court/venue header). Without this,
    # a stray line above the real caption - a document-type heading like
    # "AFFIDAVIT OF SERVICE", OCR noise from elsewhere on the page - gets
    # mistaken for a party name.
    anchor_idx = next(
        (i for i in range(p_idx - 1, -1, -1) if _is_caption_boilerplate(lines[i])),
        None,
    )
    start_idx = anchor_idx + 1 if anchor_idx is not None else 0

    s_idx = next(
        (i for i in range(p_idx + 1, len(lines)) if _CAPTION_SEPARATOR_PATTERN.match(lines[i].strip())),
        None,
    )
    d_idx = next(
        (i for i in range(p_idx + 1, len(lines))
         if _CAPTION_ROLE_LINE_PATTERN.match(lines[i].strip()) and _CAPTION_DEFENDANT_ROLE_PATTERN.search(lines[i])),
        None,
    )

    plaintiff_lines = [
        line for line in lines[start_idx:p_idx]
        if line.strip() and not _is_caption_boilerplate(line) and not _is_caption_address(line)
    ]

    defendant_lines = []
    if s_idx is not None and d_idx is not None and d_idx > s_idx:
        defendant_lines = [
            line for line in lines[s_idx + 1:d_idx]
            if line.strip() and not _is_caption_boilerplate(line) and not _is_caption_address(line)
        ]

    return {
        "plaintiffs": _split_caption_names(plaintiff_lines),
        "defendants": _split_caption_names(defendant_lines),
    }


def extract_pdf_metadata(pdf_path: Path) -> dict[str, Any]:
    """Extract metadata from a PDF file.

    Args:
        pdf_path: Path to PDF file

    Returns:
        Dictionary with PDF metadata (created, modified, pages, etc.)
    """
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            result: dict[str, Any] = {"pages": len(doc)}
            metadata = doc.get_metadata_dict()

            # Extract creation/modification dates
            if metadata.get("CreationDate"):
                try:
                    result["created"] = parse_pdf_date(metadata["CreationDate"])
                except Exception:
                    pass

            if metadata.get("ModDate"):
                try:
                    result["modified"] = parse_pdf_date(metadata["ModDate"])
                except Exception:
                    pass

            # Extract text fields
            if metadata.get("Title"):
                result["title"] = metadata["Title"]

            if metadata.get("Author"):
                result["author"] = metadata["Author"]

            return result
        finally:
            doc.close()

    except Exception as e:
        logger.warning(f"Failed to extract PDF metadata from {pdf_path}: {e}")
        return {}


def parse_pdf_date(pdf_date_str: str) -> datetime | None:
    """Parse PDF date string to datetime.

    PDF dates are in format: D:YYYYMMDDHHmmSSOHH'mm'
    Example: D:20240115153000-08'00'

    Args:
        pdf_date_str: PDF date string

    Returns:
        Parsed datetime, or None if parsing fails
    """
    if not pdf_date_str:
        return None

    # Remove 'D:' prefix
    if pdf_date_str.startswith("D:"):
        pdf_date_str = pdf_date_str[2:]

    # Try to parse the base date (YYYYMMDDHHmmSS)
    try:
        # Take first 14 characters (YYYYMMDDHHmmSS)
        base_date = pdf_date_str[:14]
        return datetime.strptime(base_date, "%Y%m%d%H%M%S")
    except Exception:
        return None


def normalize_party_name(name: str) -> str:
    """Normalize party name for comparison.

    Args:
        name: Party name to normalize

    Returns:
        Normalized name
    """
    # Convert to lowercase
    name = name.lower()

    # Normalize corporate suffixes
    suffixes = {
        "incorporated": "inc",
        "corporation": "corp",
        "company": "co",
        "limited": "ltd",
        "limited liability company": "llc",
    }

    for full, abbr in suffixes.items():
        name = re.sub(rf"\b{full}\b\.?", abbr, name, flags=re.IGNORECASE)

    # Remove punctuation except hyphens
    name = re.sub(r"[^\w\s-]", "", name)

    # Collapse whitespace
    name = " ".join(name.split())

    return name.strip()


def find_party_aliases(party_name: str) -> list[str]:
    """Generate common aliases for a party name.

    Args:
        party_name: Full party name

    Returns:
        List of common aliases
    """
    aliases = []

    # Add the original name
    aliases.append(party_name)

    # Add last name for individuals (rough heuristic)
    parts = party_name.split()
    if len(parts) >= 2 and not any(suffix in party_name.lower() for suffix in ["inc", "corp", "llc", "ltd"]):
        # Likely an individual - add last name
        aliases.append(parts[-1])

    # Add short form for organizations
    if any(suffix in party_name.lower() for suffix in ["inc", "corp", "llc", "ltd", "company"]):
        # Remove corporate suffix
        short_name = re.sub(
            r"\s+(inc\.?|incorporated|corp\.?|corporation|llc|ltd\.?|limited|company|co\.?)$",
            "",
            party_name,
            flags=re.IGNORECASE
        ).strip()
        if short_name and short_name != party_name:
            aliases.append(short_name)

    # Add acronym for multi-word organizations
    words = party_name.split()
    if len(words) >= 2:
        acronym = "".join(w[0].upper() for w in words if w and w[0].isalnum())
        if len(acronym) >= 2 and acronym not in aliases:
            aliases.append(acronym)

    return aliases


def normalize_role(role: str) -> str:
    """Normalize party role to standard values.

    Args:
        role: Raw role string

    Returns:
        Normalized role
    """
    role_lower = role.lower().strip()

    role_mapping = {
        "plaintiff": "plaintiff",
        "plaintiffs": "plaintiff",
        "defendant": "defendant",
        "defendants": "defendant",
        "intervenor": "intervenor",
        "third party": "third_party",
        "third-party": "third_party",
        "amicus": "amicus",
        "amicus curiae": "amicus",
        "counsel": "counsel",
        "attorney": "counsel",
        "court": "court",
        "judge": "court",
    }

    return role_mapping.get(role_lower, "other")


# ============================================================================
# Sentence splitting (for entity context extraction)
# ============================================================================

# Seeds NLTK's Punkt tokenizer with legal/court-filing abbreviations it
# wouldn't otherwise know, so it doesn't split "Hon. Brendan T. Lantry" or
# "N.Y. C.P.L.R." into fragments at every period. Built from parameters
# alone - no pretrained NLTK corpus download required.
_SENTENCE_ABBREVIATIONS = {
    # Honorifics / titles
    "mr", "mrs", "ms", "dr", "hon", "esq", "jr", "sr", "prof",
    # Legal citation / procedural
    "no", "nos", "vs", "v", "id", "al", "et", "sec", "art", "ch", "para", "pt",
    "cir", "supp", "f.2d", "f.3d", "f.supp", "u.s.c", "c.f.r", "cplr", "c.p.l.r",
    "fed", "civ", "crim", "proc", "r", "rule", "reg",
    # Generic Latin/abbreviations
    "i.e", "e.g", "etc", "cf",
    # NYSCEF/court boilerplate
    "nyscef", "doc", "dept", "div", "co", "corp", "inc", "ltd", "llc", "llp", "lp",
    "assoc", "bros",
    # Geography
    "u.s", "n.y", "st", "ave", "blvd", "ct",
    # Months
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    # Misc
    "vol", "pg", "pp",
}

_sentence_tokenizer: PunktSentenceTokenizer | None = None


def _get_sentence_tokenizer() -> PunktSentenceTokenizer:
    global _sentence_tokenizer
    if _sentence_tokenizer is None:
        params = PunktParameters()
        params.abbrev_types = set(_SENTENCE_ABBREVIATIONS)
        _sentence_tokenizer = PunktSentenceTokenizer(params)
    return _sentence_tokenizer


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Split text into sentences, returning their (char_start, char_end) spans.

    Args:
        text: Document text

    Returns:
        List of (start, end) offsets, one per sentence, in document order.
        Empty if text is blank.
    """
    if not text.strip():
        return []
    return list(_get_sentence_tokenizer().span_tokenize(text))


_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def split_paragraphs(text: str) -> list[tuple[int, int]]:
    """Split text into paragraphs, returning their (char_start, char_end)
    spans - same span-based contract as split_sentences. A paragraph is a
    run of text between blank-line separators; each span is trimmed to its
    actual content (surrounding whitespace excluded), and a run that's
    blank after trimming (consecutive separators) is skipped.

    Used to group same-paragraph dates/entities (see Stage4Dates) - a
    coarser grain than split_sentences' per-sentence context window.

    Args:
        text: Document text

    Returns:
        List of (start, end) offsets, one per paragraph, in document order.
        Empty if text is blank.
    """
    if not text.strip():
        return []

    spans = []
    pos = 0
    for m in _PARAGRAPH_SPLIT_RE.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))

    trimmed = []
    for start, end in spans:
        segment = text[start:end]
        stripped = segment.strip()
        if not stripped:
            continue
        lstrip_len = len(segment) - len(segment.lstrip())
        trimmed.append((start + lstrip_len, start + lstrip_len + len(stripped)))
    return trimmed


# ============================================================================
# Cross-document references (linking a document's own filing number to
# citations of it in other documents' text)
#
# Not tied to any one court/state's e-filing system: different systems
# label a document's self-identifying stamp differently (NY's NYSCEF prints
# "NYSCEF DOC. NO. <N>"; others may say "DOCUMENT NO.", "FILING ID:", etc.).
# _DOCUMENT_SIGNATURE_LABEL_PATTERNS is an ordered, extensible list - add a
# pattern per system as new ones are seen rather than hardcoding one.
# ============================================================================

_DOCUMENT_SIGNATURE_LABEL_PATTERNS = [
    # NY state e-filing (NYSCEF)
    re.compile(r"NYSCEF\s+DOC(?:UMENT)?\.?\s*NO\.?\s*(\d+)", re.IGNORECASE),
    # Generic fallback: any system that just labels its own document number
    # "DOC. NO. <N>" / "DOCUMENT NO. <N>" without a named system prefix. A
    # citation to another document is rare this early in a filing, so the
    # first bare label found on the first page is reliably this document's
    # own number.
    re.compile(r"\bDOC(?:UMENT)?\.?\s*NO\.?\s*(\d+)", re.IGNORECASE),
]


_TITLE_BOILERPLATE_MARKERS = (
    "court", "county of", "state of", "index no", "doc. no", "docket no",
    "case no", "filed:", "received", "-against-", " vs ", " v. ",
    "attorneys for", "telephone", "esq",
)

# Caption labels excluded only as a whole-line match (e.g. "Defendants,")
# - unlike _TITLE_BOILERPLATE_MARKERS, a substring match here would also
# reject genuine titles that happen to name a party's role, e.g.
# "STIPULATION OF DISCONTINUANCE AS AGAINST DEFENDANT KKR & CO."
_TITLE_CAPTION_LABELS = {"plaintiff", "plaintiffs", "defendant", "defendants"}


def find_title_candidates(first_page_items: list[dict], max_candidates: int = 5) -> list[str]:
    """Heuristic candidates for a document's title/type: short, mostly-
    uppercase text lines on page 1 - the convention pleadings/motions use
    for a document's formal name (e.g. "SUMMONS", "NOTICE OF MOTION",
    "STIPULATION OF DISCONTINUANCE...") - that aren't obvious court/
    caption/header boilerplate. Not tied to any one court/e-filing system's
    layout.

    Not authoritative on their own: an LLM makes the final call using
    these as a hint alongside the actual page text - see
    llm_validation.identify_document_title_with_llm.

    Args:
        first_page_items: Docling text items on page 1 (dicts with "text"/"label")
        max_candidates: Maximum number of candidates to return

    Returns:
        Candidate strings, in page order
    """
    candidates = []
    for item in first_page_items:
        if item.get("label") in ("page_header", "page_footer"):
            continue
        text = (item.get("text") or "").strip()
        if not (4 <= len(text) <= 150):
            continue
        letters = [c for c in text if c.isalpha()]
        if not letters:
            continue
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio < 0.8:
            continue
        low = text.lower()
        if low.strip(" ,.:;") in _TITLE_CAPTION_LABELS:
            continue
        if any(marker in low for marker in _TITLE_BOILERPLATE_MARKERS):
            continue
        candidates.append(text)
        if len(candidates) >= max_candidates:
            break
    return candidates


def extract_document_signature(text: str) -> str | None:
    """Extract a document's own filing/document number - its "signature"
    within the case.

    e.g. "NYSCEF DOC. NO. 11" -> "11". This is the number other filings
    cite when referencing this document (see find_document_references).

    Args:
        text: Document text to search (typically its first-page text)

    Returns:
        The document number as a string, or None if no known stamp format matched
    """
    for pattern in _DOCUMENT_SIGNATURE_LABEL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


# Citations elsewhere in a filing's body are looser than the self-
# identifying stamp above: real-world phrasing varies by system and by
# author ("Doc. No. 7", "Document No. 7", "NYSCEF Doc. No. 7", ...). Known
# system-name prefixes are optional alternatives here, not requirements -
# add more as new systems are seen.
DOCUMENT_REFERENCE_PATTERN = re.compile(
    r"(?:(?:NYSCEF|ECF)\s+)?Doc(?:ument)?\.?\s*No\.?\s*(\d+)", re.IGNORECASE
)


def find_document_references(text: str) -> list[tuple[str, int, int]]:
    """Find every "Doc. No. <N>" / "Document No. <N>" style citation in
    text - how one filing refers to another by its document number.

    Args:
        text: Document text to search (typically the full body)

    Returns:
        List of (doc_number, char_start, char_end) tuples, in document order
    """
    return [
        (match.group(1), match.start(), match.end())
        for match in DOCUMENT_REFERENCE_PATTERN.finditer(text)
    ]


# ============================================================================
# Litigation-caption product signals (regex pre-scan feeding LLM product
# identification - see llm_validation.identify_products_with_llm)
# ============================================================================

# Coordinated/MDL proceedings routinely name the accused product right in
# the case caption: "In Re Depo-Provera Litigation", "In re Depo-Provera
# (Depot Medroxyprogesterone Acetate) Products Liability Litigation". This
# is a strong, court/state-agnostic signal when present - not every
# product-liability case has one (a single-plaintiff filing naming a
# category of products, e.g. "the Cosmetic Products", won't), so this is
# only ever one input alongside LLM-based identification, never the sole
# source.
LITIGATION_CAPTION_PATTERN = re.compile(
    r"In\s+Re:?\s+(.+?)\s+(?:\([^)]*\)\s+)?(?:Products?\s+Liability\s+)?Litigation\b",
    re.IGNORECASE,
)


def find_litigation_captions(text: str) -> list[str]:
    """Find "In Re <product/subject> [Products Liability] Litigation" style
    captions and return the named subject.

    Args:
        text: Document text to search

    Returns:
        Deduplicated (case-insensitive) list of captured subject names, in
        order of first appearance
    """
    names: list[str] = []
    seen: set[str] = set()
    for match in LITIGATION_CAPTION_PATTERN.finditer(text):
        name = match.group(1).strip(" ,:")
        key = name.upper()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names
