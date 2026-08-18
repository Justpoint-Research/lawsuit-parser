"""Batch processing of PDF documents in the case data directory."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from tqdm import tqdm

from lawsuit_parser.parsers.pdf_parser import (
    ParsedDocument,
    parse_pdf_document,
    save_parsed_document,
)
from lawsuit_parser.postprocessors import (
    PostprocessingPipeline,
    PostprocessingStep,
    default_postprocessors,
)

logger = logging.getLogger(__name__)


def find_all_pdfs(data_dir: Path, case_id: str | None = None) -> list[Path]:
    """
    Find all PDF files in the data directory.

    Args:
        data_dir: Root data directory
        case_id: Optional case ID to filter (e.g., "104")

    Returns:
        List of PDF file paths
    """
    cases_dir = data_dir / "cases"

    if not cases_dir.exists():
        raise FileNotFoundError(f"Cases directory not found: {cases_dir}")

    # Find PDF files
    if case_id:
        pattern = f"case_{case_id}/**/*.pdf"
    else:
        pattern = "case_*/**/*.pdf"

    pdfs = sorted(cases_dir.glob(pattern))

    # Filter out .venv and other non-data directories
    pdfs = [p for p in pdfs if '.venv' not in str(p)]

    logger.info(f"Found {len(pdfs)} PDF files")
    return pdfs


def get_output_path(
    pdf_path: Path,
    output_dir: Path | None = None,
) -> Path:
    """
    Determine output path for parsed JSON.

    If output_dir is None, save alongside the PDF.
    Otherwise, preserve directory structure in output_dir.

    Args:
        pdf_path: Path to PDF file
        output_dir: Optional output directory

    Returns:
        Path to output JSON file
    """
    if output_dir is None:
        # Save alongside PDF
        return pdf_path.with_suffix('.json')

    # Preserve directory structure
    relative_path = pdf_path.relative_to(pdf_path.parents[4])  # relative to project root
    output_path = output_dir / relative_path.with_suffix('.json')
    return output_path


def load_case_metadata(case_dir: Path) -> dict[str, Any] | None:
    """
    Load case metadata from case JSON file.

    Args:
        case_dir: Case directory (e.g., data/cases/case_104)

    Returns:
        Case metadata dictionary or None if not found
    """
    case_id = case_dir.name
    case_json = case_dir / f"{case_id}.json"

    if not case_json.exists():
        return None

    with open(case_json, 'r', encoding='utf-8') as f:
        return json.load(f)


def enrich_with_metadata(
    parsed_doc: ParsedDocument,
    pdf_path: Path,
) -> ParsedDocument:
    """
    Enrich parsed document with case and document metadata.

    Args:
        parsed_doc: Parsed document
        pdf_path: Path to original PDF

    Returns:
        Enriched ParsedDocument
    """
    # Get case directory (e.g., data/cases/case_104)
    case_dir = pdf_path.parents[1]

    # Load case metadata
    case_data = load_case_metadata(case_dir)

    if case_data:
        # Add case information
        parsed_doc.metadata['case_id'] = case_data.get('case_info', {}).get('case_id')
        parsed_doc.metadata['docket_id'] = case_data.get('case_info', {}).get('docket_id')
        parsed_doc.metadata['caption'] = case_data.get('case_info', {}).get('caption')
        parsed_doc.metadata['court'] = case_data.get('case_info', {}).get('court')

        # Find matching document info
        relative_path = pdf_path.relative_to(case_dir)
        for doc in case_data.get('documents', []):
            if doc.get('local_document_path') == str(relative_path) or \
               doc.get('local_confirmation_path') == str(relative_path):
                parsed_doc.metadata['document_name'] = doc.get('document_name')
                parsed_doc.metadata['document_details'] = doc.get('document_details')
                parsed_doc.metadata['filed_by'] = doc.get('filed_by')
                parsed_doc.metadata['filed_date'] = doc.get('filed_create')
                parsed_doc.metadata['document_status'] = doc.get('document_status')
                break

    return parsed_doc


def parse_and_save_pdf(
    pdf_path: Path,
    output_path: Path,
    skip_existing: bool = False,
    use_gpu: bool = True,
    postprocessors: list[PostprocessingStep] | None = None,
) -> tuple[bool, str]:
    """
    Parse a single PDF, postprocess and enrich it with case metadata, and
    save the result.

    Args:
        pdf_path: Path to PDF file
        output_path: Path to output JSON file
        skip_existing: Skip if output already exists
        use_gpu: Use GPU acceleration
        postprocessors: Postprocessing steps to run after text extraction,
            in order (default: `default_postprocessors()`)

    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        # Check if already processed
        if skip_existing and output_path.exists():
            return True, "Skipped (already exists)"

        # Parse the PDF
        parsed = parse_pdf_document(
            pdf_path,
            use_gpu=use_gpu,
            extract_tables=True,
            extract_images=False,
        )

        # Run postprocessing steps on the extracted text
        pipeline = PostprocessingPipeline(
            postprocessors if postprocessors is not None else default_postprocessors()
        )
        parsed = pipeline.run(parsed)

        # Enrich with metadata from case JSON
        parsed = enrich_with_metadata(parsed, pdf_path)

        # Save to JSON
        save_parsed_document(parsed, output_path)

        return True, "Success"

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to parse {pdf_path}: {error_msg}")
        return False, f"Error: {error_msg[:100]}"


def parse_all_pdfs(
    data_dir: Path = Path("data"),
    output_dir: Path | None = None,
    case_id: str | None = None,
    skip_existing: bool = False,
    use_gpu: bool = True,
    progress_file: Any = None,
    postprocessors: list[PostprocessingStep] | None = None,
    max_workers: int = 8,
) -> dict[str, Any]:
    """
    Parse all PDFs in the data directory.

    Args:
        data_dir: Root data directory
        output_dir: Optional output directory (default: save alongside PDFs)
        case_id: Optional case ID to filter
        skip_existing: Skip files that have already been processed
        use_gpu: Use GPU acceleration
        progress_file: Stream the tqdm progress bar is written to
            (default: sys.stderr). Useful when stderr has been redirected
            elsewhere and the progress bar still needs to reach a console.
        postprocessors: Postprocessing steps to run after text extraction,
            in order (default: `default_postprocessors()`)
        max_workers: Number of PDFs to parse concurrently. All workers share
            a single cached Docling converter (see `_build_converter`), so
            this overlaps one file's CPU-bound work (page rasterization,
            text extraction, disk I/O) with another's GPU inference, rather
            than spinning up separate converters/CUDA contexts per worker.
            Set to 1 for sequential parsing. Defaults to 8; benchmarking on
            a single GPU showed 4 workers captures most of the throughput
            gain from overlap, with 8 adding a smaller further improvement.

    Returns:
        Dictionary with summary statistics, including a "failures" list of
        (pdf_path, error_message) tuples.
    """
    # Find all PDFs
    pdfs = find_all_pdfs(data_dir, case_id)

    if not pdfs:
        logger.warning("No PDF files found")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "failures": []}

    # Process each PDF with progress bar
    stats = {"total": len(pdfs), "success": 0, "failed": 0, "skipped": 0}
    failures = []

    def record(pdf_path: Path, success: bool, message: str) -> None:
        if "Skipped" in message:
            stats["skipped"] += 1
        elif success:
            stats["success"] += 1
        else:
            stats["failed"] += 1
            failures.append((str(pdf_path), message))

    with tqdm(total=len(pdfs), desc="Parsing PDFs", unit="file", file=progress_file) as pbar:
        if max_workers <= 1:
            for pdf_path in pdfs:
                output_path = get_output_path(pdf_path, output_dir)
                success, message = parse_and_save_pdf(
                    pdf_path,
                    output_path,
                    skip_existing=skip_existing,
                    use_gpu=use_gpu,
                    postprocessors=postprocessors,
                )
                record(pdf_path, success, message)
                pbar.set_postfix(success=stats["success"], failed=stats["failed"], skipped=stats["skipped"])
                pbar.update(1)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_pdf = {
                    executor.submit(
                        parse_and_save_pdf,
                        pdf_path,
                        get_output_path(pdf_path, output_dir),
                        skip_existing=skip_existing,
                        use_gpu=use_gpu,
                        postprocessors=postprocessors,
                    ): pdf_path
                    for pdf_path in pdfs
                }
                for future in as_completed(future_to_pdf):
                    pdf_path = future_to_pdf[future]
                    success, message = future.result()
                    record(pdf_path, success, message)
                    pbar.set_postfix(success=stats["success"], failed=stats["failed"], skipped=stats["skipped"])
                    pbar.update(1)

    # Log summary
    logger.info("\n" + "="*60)
    logger.info("PARSING SUMMARY")
    logger.info("="*60)
    logger.info(f"Total files: {stats['total']}")
    logger.info(f"Successfully parsed: {stats['success']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"Skipped: {stats['skipped']}")

    if failures:
        logger.info("\nFailed files:")
        for pdf_path, error in failures[:10]:  # Show first 10 failures
            logger.info(f"  - {pdf_path}: {error}")
        if len(failures) > 10:
            logger.info(f"  ... and {len(failures) - 10} more")

    stats["failures"] = failures
    return stats