"""Utility functions for event extraction pipeline."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from PyPDF2 import PdfReader
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False
    PdfReader = None


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
    """Extract CM/ECF header information from document text.

    Looks for patterns like:
    "Case 3:24-cv-12345 Document 1 Filed 01/15/2024 Page 1 of 42"

    Args:
        text: Document text (typically first 500 characters)

    Returns:
        Dictionary with extracted fields, or None if no header found
    """
    # Pattern for CM/ECF header
    pattern = r"Case\s+([^\s]+)\s+Document\s+(\d+)\s+Filed\s+([\d/]+)(?:\s+Page\s+(\d+)\s+of\s+(\d+))?"

    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None

    case_number, doc_number, filing_date, page_num, total_pages = match.groups()

    result = {
        "case_number": case_number,
        "document_number": doc_number,
        "filing_date": filing_date,
    }

    if page_num and total_pages:
        result["page_info"] = f"Page {page_num} of {total_pages}"

    return result


def extract_pdf_metadata(pdf_path: Path) -> dict[str, Any]:
    """Extract metadata from a PDF file.

    Args:
        pdf_path: Path to PDF file

    Returns:
        Dictionary with PDF metadata (created, modified, pages, etc.)
    """
    if not HAS_PYPDF2:
        print(f"  Warning: PyPDF2 not installed, skipping PDF metadata extraction")
        return {}

    try:
        reader = PdfReader(pdf_path)
        metadata = reader.metadata or {}

        result = {
            "pages": len(reader.pages),
        }

        # Extract creation/modification dates
        if "/CreationDate" in metadata:
            try:
                result["created"] = parse_pdf_date(metadata["/CreationDate"])
            except Exception:
                pass

        if "/ModDate" in metadata:
            try:
                result["modified"] = parse_pdf_date(metadata["/ModDate"])
            except Exception:
                pass

        # Extract text fields
        if "/Title" in metadata:
            result["title"] = str(metadata["/Title"])

        if "/Author" in metadata:
            result["author"] = str(metadata["/Author"])

        return result

    except Exception as e:
        print(f"Warning: Failed to extract PDF metadata from {pdf_path}: {e}")
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
