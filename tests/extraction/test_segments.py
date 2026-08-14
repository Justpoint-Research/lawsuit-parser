"""Tests for Stage 0: Segmentation."""

import pytest
from lawsuit_parser.extraction.segments import (
    normalize_whitespace,
    extract_cmecf_header,
    detect_paragraph_label,
)


def test_normalize_whitespace_idempotent():
    """Test that whitespace normalization is idempotent."""
    text = "Line 1\r\nLine 2\n\n\nLine 3  \nLine 4"
    normalized = normalize_whitespace(text)
    normalized_twice = normalize_whitespace(normalized)
    assert normalized == normalized_twice


def test_normalize_whitespace_crlf():
    """Test that \\r\\n is normalized to \\n."""
    text = "Line 1\r\nLine 2\r\n"
    normalized = normalize_whitespace(text)
    assert "\r" not in normalized
    assert normalized == "Line 1\nLine 2"


def test_normalize_whitespace_trailing():
    """Test that trailing whitespace is stripped."""
    text = "Line 1   \nLine 2  "
    normalized = normalize_whitespace(text)
    assert normalized == "Line 1\nLine 2"


def test_normalize_whitespace_collapse_blank_lines():
    """Test that 3+ blank lines are collapsed to 2."""
    text = "Line 1\n\n\n\nLine 2"
    normalized = normalize_whitespace(text)
    assert normalized == "Line 1\n\nLine 2"


def test_extract_cmecf_header_success():
    """Test CM/ECF header extraction."""
    text = "Case 1:19-cv-01234-ABC Document 1 Filed 01/15/2019 Page 1 of 25"
    result = extract_cmecf_header(text)
    assert result is not None
    assert result["case_number"] == "1:19-cv-01234-ABC"
    assert result["document_number"] == "1"
    assert result["filing_date"] == "01/15/2019"


def test_extract_cmecf_header_not_found():
    """Test CM/ECF header extraction when not present."""
    text = "This is a regular document without CM/ECF header"
    result = extract_cmecf_header(text)
    assert result is None


def test_detect_paragraph_label_numbered():
    """Test detection of numbered paragraphs."""
    assert detect_paragraph_label("42. This is a paragraph.") == "42."
    assert detect_paragraph_label("1. First paragraph.") == "1."


def test_detect_paragraph_label_pilcrow():
    """Test detection of pilcrow paragraphs."""
    assert detect_paragraph_label("¶ 42. This is a paragraph.") == "¶ 42."
    assert detect_paragraph_label("¶42. This is a paragraph.") == "¶42."


def test_detect_paragraph_label_roman():
    """Test detection of Roman numeral labels."""
    assert detect_paragraph_label("II.B. This is a section.") == "II.B."
    assert detect_paragraph_label("IV.A. This is a section.") == "IV.A."


def test_detect_paragraph_label_parenthetical():
    """Test detection of parenthetical labels."""
    assert detect_paragraph_label("(a) This is a subsection.") == "(a)"
    assert detect_paragraph_label("(1) This is a subsection.") == "(1)"


def test_detect_paragraph_label_not_found():
    """Test when no label is found."""
    assert detect_paragraph_label("This is just regular text.") is None
    assert detect_paragraph_label("No label here") is None
