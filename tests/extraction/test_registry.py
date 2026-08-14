"""Tests for Stage 2: Party Registry."""

import pytest
from lawsuit_parser.extraction.registry import (
    normalize_name,
    infer_party_type,
)


def test_normalize_name_case_fold():
    """Test that names are case-folded."""
    assert normalize_name("ACME Corp") == normalize_name("acme corp")


def test_normalize_name_punctuation():
    """Test that punctuation is stripped."""
    assert normalize_name("ACME, Inc.") == "acme inc"
    assert normalize_name("John Smith (aka J.S.)") == "john smith aka j s"


def test_normalize_name_corporate_suffix():
    """Test corporate suffix normalization."""
    assert normalize_name("ACME Inc.") == "acme inc"
    assert normalize_name("ACME Incorporated") == "acme inc"
    assert normalize_name("ACME Corp.") == "acme corp"
    assert normalize_name("ACME Corporation") == "acme corp"
    assert normalize_name("ACME LLC") == "acme llc"
    assert normalize_name("ACME L.L.C.") == "acme llc"


def test_infer_party_type_court():
    """Test court party type inference."""
    assert infer_party_type("United States District Court", "court") == "court"


def test_infer_party_type_government():
    """Test government party type inference."""
    assert infer_party_type("United States of America", "plaintiff") == "government"
    assert infer_party_type("State of California", "defendant") == "government"
    assert infer_party_type("County of Los Angeles", "plaintiff") == "government"
    assert (
        infer_party_type("Department of Justice", "plaintiff") == "government"
    )


def test_infer_party_type_organization():
    """Test organization party type inference."""
    assert infer_party_type("ACME Corporation", "defendant") == "organization"
    assert infer_party_type("XYZ LLC", "plaintiff") == "organization"
    assert infer_party_type("Tech Company Inc", "defendant") == "organization"


def test_infer_party_type_individual():
    """Test individual party type inference."""
    assert infer_party_type("John Smith", "plaintiff") == "individual"
    assert infer_party_type("Jane Doe", "defendant") == "individual"
