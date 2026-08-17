"""PDF document parser using Docling for structured extraction."""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
from docling_core.types.doc import DocItemLabel
from docling_core.types.doc.base import ImageRefMode


@dataclass
class ParsedDocument:
    """Structured representation of a parsed PDF document."""

    title: str | None = None
    header: str | None = None
    paragraphs: list[str] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    page_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding the raw text (not part of the JSON output)."""
        data = asdict(self)
        data.pop("raw_text", None)
        return data

    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def parse_pdf_document(
    pdf_path: str | Path,
    use_gpu: bool = True,
    extract_tables: bool = True,
    extract_images: bool = False,
    save_docling_document: bool = True,
    save_markdown: bool = True,
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

    The full Docling document (layout, bounding boxes, all item types) is
    saved as `<pdf_path>.docling.json` alongside the PDF, so anything not
    surfaced on `ParsedDocument` (footers, footnotes, section headers, etc.)
    remains available for further processing. A `<pdf_path>.md` rendering
    is also saved alongside the PDF for easy human preview.

    Args:
        pdf_path: Path to the PDF file
        use_gpu: Whether to use GPU acceleration (default: True)
        extract_tables: Whether to extract tables (default: True)
        extract_images: Whether to embed images in the saved Docling
            document and Markdown preview (default: False)
        save_docling_document: Whether to save the full Docling document
            next to the PDF (default: True)
        save_markdown: Whether to save a Markdown rendering next to the
            PDF, for easy preview (default: True)

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

    # Initialize converter with default options
    # The default 'auto' device setting will automatically use GPU if available
    # Note: Custom format_options cause version incompatibility issues with Docling 2.x
    converter = DocumentConverter()

    # Debug: log GPU usage setting
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Parsing {pdf_path.name} with use_gpu={use_gpu} (using default 'auto' device detection)")

    try:
        # Convert the document
        result = converter.convert(str(pdf_path))

        # Extract structured content
        doc = result.document

        image_mode = ImageRefMode.EMBEDDED if extract_images else ImageRefMode.PLACEHOLDER

        # Save the full Docling document (layout, bounding boxes, every
        # item type) next to the PDF for further processing.
        if save_docling_document:
            doc.save_as_json(pdf_path.with_suffix(".docling.json"), image_mode=image_mode)

        # Save a Markdown rendering next to the PDF for easy preview.
        if save_markdown:
            doc.save_as_markdown(pdf_path.with_suffix(".md"), image_mode=image_mode)

        # Initialize parsed document
        parsed = ParsedDocument(
            page_count=len(doc.pages) if hasattr(doc, 'pages') else 0,
            metadata={
                "file_name": pdf_path.name,
                "file_size": pdf_path.stat().st_size,
            }
        )

        # Extract title (usually the first heading), falling back to the
        # document name if no title item is found
        if hasattr(doc, 'name') and doc.name:
            parsed.title = doc.name

        # Extract all text content
        first_title = None
        first_header = None
        paragraphs = []
        tables = []
        all_text = []

        # Iterate through document items
        for item, level in doc.iterate_items():
            item_type = type(item).__name__
            label = getattr(item, 'label', None)

            # Extract text from different item types
            if hasattr(item, 'text') and item.text:
                text = item.text.strip()
                if text:
                    all_text.append(text)

                    if label == DocItemLabel.TITLE:
                        if first_title is None:
                            first_title = text
                    elif label == DocItemLabel.PAGE_HEADER:
                        if first_header is None:
                            first_header = text
                    elif label == DocItemLabel.TEXT:
                        # Regular page body paragraph
                        paragraphs.append(text)

            # Extract tables if enabled
            if extract_tables and 'table' in item_type.lower():
                if hasattr(item, 'to_dict'):
                    tables.append(item.to_dict())

        if first_title:
            parsed.title = first_title
        parsed.header = first_header
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
