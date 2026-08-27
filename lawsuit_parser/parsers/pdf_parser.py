"""PDF document parser using Docling for structured extraction."""

import json
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    LayoutObjectDetectionOptions,
)
from docling.datamodel.stage_model_specs import ObjectDetectionModelSpec, EngineModelConfig
from docling.datamodel.object_detection_engine_options import (
    OnnxRuntimeObjectDetectionEngineOptions,
)
from docling.models.inference_engines.object_detection.base import ObjectDetectionEngineType
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


@lru_cache(maxsize=2)
def _build_converter(use_gpu: bool) -> DocumentConverter:
    """
    Build (and cache) the Docling converter for a given GPU setting.

    Building a `DocumentConverter` re-initializes the ONNX layout and OCR
    models (session creation, HuggingFace revision lookup, CUDA context
    setup), which costs seconds. Reusing one converter across a whole batch
    run instead of rebuilding it per file avoids paying that cost per
    document and avoids the GPU memory growth that comes from repeatedly
    creating and discarding ONNX Runtime CUDA sessions.
    """
    # Configure layout model to use ONNX runtime instead of transformers
    # This bypasses the torch import bug in transformers 5.x
    # ONNX runtime can still use GPU via CUDA execution provider
    pipeline_options = StandardPdfPipeline.get_default_options()

    # Create HERON model spec with ONNX runtime engine override
    onnx_model_spec = ObjectDetectionModelSpec(
        name="layout_heron",
        repo_id="docling-project/docling-layout-heron-onnx",
        revision="main",
        engine_overrides={
            ObjectDetectionEngineType.ONNXRUNTIME: EngineModelConfig(
                repo_id="docling-project/docling-layout-heron-onnx",
                extra_config={"model_filename": "model.onnx"},
            )
        },
    )

    # Override layout options to use ONNX model, running on the ONNX
    # Runtime engine (the default engine is Transformers, which cannot
    # load this ONNX-only model repository)
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if use_gpu else ["CPUExecutionProvider"]
    pipeline_options.layout_options = LayoutObjectDetectionOptions(
        model_spec=onnx_model_spec,
        engine_options=OnnxRuntimeObjectDetectionEngineOptions(providers=providers),
    )

    # Let RapidOCR (and any other torch/onnxruntime-backed stage) pick up
    # the GPU too - by default it only activates CUDA when the resolved
    # device string contains "cuda", so "auto" isn't enough to guarantee it.
    pipeline_options.accelerator_options.device = "cuda" if use_gpu else "cpu"

    # Initialize converter with ONNX-based layout detection
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )


def parse_pdf_document(
    pdf_path: str | Path,
    use_gpu: bool = True,
    extract_tables: bool = True,
    extract_images: bool = False,
    save_docling_document: bool = True,
    save_markdown: bool = True,
    docling_dir: str | Path | None = None,
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
    saved as `<pdf_stem>.docling.json`, so anything not surfaced on
    `ParsedDocument` (footers, footnotes, section headers, etc.) remains
    available for further processing. A `<pdf_stem>.md` rendering is also
    saved for easy human preview. Both go in `docling_dir` if given,
    otherwise alongside the PDF.

    Args:
        pdf_path: Path to the PDF file
        use_gpu: Whether to use GPU acceleration (default: True)
        extract_tables: Whether to extract tables (default: True)
        extract_images: Whether to embed images in the saved Docling
            document and Markdown preview (default: False)
        save_docling_document: Whether to save the full Docling document
            (default: True)
        save_markdown: Whether to save a Markdown rendering (default: True)
        docling_dir: Directory to save the Docling document and Markdown
            rendering into. Defaults to the PDF's own directory.

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

    if docling_dir is not None:
        docling_dir = Path(docling_dir)
        docling_dir.mkdir(parents=True, exist_ok=True)
        docling_json_path = docling_dir / f"{pdf_path.stem}.docling.json"
        markdown_path = docling_dir / f"{pdf_path.stem}.md"
    else:
        docling_json_path = pdf_path.with_suffix(".docling.json")
        markdown_path = pdf_path.with_suffix(".md")

    converter = _build_converter(use_gpu)

    # Debug: log GPU usage setting
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Parsing {pdf_path.name} with ONNX runtime layout model (GPU-enabled), use_gpu={use_gpu}")

    try:
        # Convert the document
        result = converter.convert(str(pdf_path))

        # Extract structured content
        doc = result.document

        image_mode = ImageRefMode.EMBEDDED if extract_images else ImageRefMode.PLACEHOLDER

        # Save the full Docling document (layout, bounding boxes, every
        # item type) for further processing.
        if save_docling_document:
            doc.save_as_json(docling_json_path, image_mode=image_mode)

        # Save a Markdown rendering for easy preview.
        if save_markdown:
            doc.save_as_markdown(markdown_path, image_mode=image_mode)

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
                if hasattr(item, 'model_dump'):
                    tables.append(item.model_dump(mode="json"))

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
