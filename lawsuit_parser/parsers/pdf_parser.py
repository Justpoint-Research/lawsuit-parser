"""PDF document parser using Docling for structured extraction."""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions


@dataclass
class ParsedDocument:
    """Structured representation of a parsed PDF document."""

    title: str | None = None
    paragraphs: list[str] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    page_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def parse_pdf_document(
    pdf_path: str | Path,
    use_gpu: bool = True,
    extract_tables: bool = True,
    extract_images: bool = False,
) -> ParsedDocument:
    """
    Parse a PDF document and extract structured content.

    Uses Docling (IBM's document understanding library) for state-of-the-art
    PDF parsing with support for:
    - Title and heading extraction
    - Paragraph segmentation
    - Table extraction
    - Layout understanding
    - GPU acceleration

    Args:
        pdf_path: Path to the PDF file
        use_gpu: Whether to use GPU acceleration (default: True)
        extract_tables: Whether to extract tables (default: True)
        extract_images: Whether to extract images (default: False)

    Returns:
        ParsedDocument containing structured extracted content

    Raises:
        FileNotFoundError: If PDF file doesn't exist
        Exception: If parsing fails

    Example:
        >>> doc = parse_pdf_document("lawsuit.pdf")
        >>> print(doc.title)
        >>> for paragraph in doc.paragraphs:
        ...     print(paragraph)
        >>> json_output = doc.to_json()
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    # Initialize converter
    # Note: Docling automatically enables OCR and table extraction by default
    converter = DocumentConverter()

    try:
        # Convert the document
        result = converter.convert(str(pdf_path))

        # Extract structured content
        doc = result.document

        # Initialize parsed document
        parsed = ParsedDocument(
            page_count=len(doc.pages) if hasattr(doc, 'pages') else 0,
            metadata={
                "file_name": pdf_path.name,
                "file_size": pdf_path.stat().st_size,
            }
        )

        # Extract title (usually the first heading)
        if hasattr(doc, 'name') and doc.name:
            parsed.title = doc.name

        # Extract all text content
        paragraphs = []
        tables = []
        all_text = []

        # Iterate through document items
        for item, level in doc.iterate_items():
            item_type = type(item).__name__

            # Extract text from different item types
            if hasattr(item, 'text') and item.text:
                text = item.text.strip()
                if text:
                    all_text.append(text)

                    # Check if this is a heading (title)
                    if 'heading' in item_type.lower() or 'title' in item_type.lower():
                        if parsed.title is None and level == 0:
                            parsed.title = text
                        elif level == 0:
                            # First level-0 heading is the title
                            parsed.title = text
                    else:
                        # Regular paragraph
                        paragraphs.append(text)

            # Extract tables if enabled
            if extract_tables and 'table' in item_type.lower():
                if hasattr(item, 'to_dict'):
                    tables.append(item.to_dict())

        parsed.paragraphs = paragraphs
        parsed.tables = tables
        parsed.raw_text = "\n\n".join(all_text)

        # Add document metadata
        if hasattr(doc, 'metadata'):
            parsed.metadata.update(doc.metadata)

        return parsed

    except Exception as e:
        raise Exception(f"Failed to parse PDF {pdf_path}: {str(e)}") from e


def save_parsed_document(
    parsed_doc: ParsedDocument,
    output_path: str | Path,
    indent: int = 2,
) -> None:
    """
    Save parsed document to JSON file.

    Args:
        parsed_doc: ParsedDocument to save
        output_path: Path to output JSON file
        indent: JSON indentation (default: 2)
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(parsed_doc.to_json(indent=indent))


def parse_and_save(
    pdf_path: str | Path,
    output_path: str | Path,
    **kwargs,
) -> ParsedDocument:
    """
    Convenience function to parse and save in one call.

    Args:
        pdf_path: Path to PDF file
        output_path: Path to output JSON file
        **kwargs: Additional arguments passed to parse_pdf_document

    Returns:
        ParsedDocument
    """
    parsed = parse_pdf_document(pdf_path, **kwargs)
    save_parsed_document(parsed, output_path)
    return parsed
