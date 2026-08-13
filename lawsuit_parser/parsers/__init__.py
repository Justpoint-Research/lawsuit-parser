"""PDF parsing functionality for legal documents."""

from .batch import (
    enrich_with_metadata,
    find_all_pdfs,
    get_output_path,
    load_case_metadata,
    parse_all_pdfs,
    parse_and_save_pdf,
)
from .pdf_parser import ParsedDocument, parse_pdf_document, save_parsed_document

__all__ = [
    "ParsedDocument",
    "enrich_with_metadata",
    "find_all_pdfs",
    "get_output_path",
    "load_case_metadata",
    "parse_all_pdfs",
    "parse_and_save_pdf",
    "parse_pdf_document",
    "save_parsed_document",
]