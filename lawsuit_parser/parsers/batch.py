"""Batch processing of PDF documents in the case data directory."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from tqdm import tqdm

from lawsuit_parser.parsers.pdf_parser import parse_pdf_document, save_parsed_document

logger = logging.getLogger(__name__)


def find_all_pdfs(data_dir: Path, case_id: str | None = None) -> list[Path]:
    """
    Find all PDF files in the data directory.

    Args:
        data_dir: Root data directory
        case_id: Optional case directory to filter to - either a full
            directory name (e.g. "case_104", "mdl-1358") or a bare case
            number (e.g. "104", kept for backward compatibility - implies
            "case_104")

    Returns:
        List of PDF file paths
    """
    cases_dir = data_dir / "cases"

    if not cases_dir.exists():
        raise FileNotFoundError(f"Cases directory not found: {cases_dir}")

    # Find PDF files. No case_id filter processes every case directory
    # (case_* NYSCEF exports and mdl-* MDL docket scrapes alike), not just
    # case_*.
    if case_id:
        dir_name = case_id if (cases_dir / case_id).is_dir() else f"case_{case_id}"
        pattern = f"{dir_name}/**/*.pdf"
    else:
        pattern = "*/**/*.pdf"

    pdfs = sorted(cases_dir.glob(pattern))

    # Filter out .venv and other non-data directories
    pdfs = [p for p in pdfs if '.venv' not in str(p)]

    logger.info(f"Found {len(pdfs)} PDF files")
    return pdfs


def get_docling_dir(pdf_path: Path) -> Path:
    """
    Determine the directory to save a PDF's Docling outputs
    (.docling.json, .md) into.

    A case directory holds PDFs of the same name under multiple source
    subdirectories (e.g. `documents/` and `confirmations/` can each contain
    a `document_<id>.pdf` that are different files). Saving Docling output
    next to the PDF, as `parse_pdf_document` does by default, spreads it
    across those source subdirectories and risks collisions if they're ever
    flattened. Mirroring the source subdirectory under a single case-level
    `docling/` directory keeps generated artifacts out of the source
    directories while still avoiding name collisions.

    Args:
        pdf_path: Path to PDF file (e.g. data/cases/case_104/documents/foo.pdf)

    Returns:
        Directory to save Docling outputs into
        (e.g. data/cases/case_104/docling/documents)
    """
    case_dir = pdf_path.parents[1]
    return case_dir / "docling" / pdf_path.parent.name


def parse_and_save_pdf(
    pdf_path: Path,
    skip_existing: bool = False,
    use_gpu: bool = True,
) -> tuple[bool, str]:
    """
    Parse a single PDF and save Docling's output (.docling.json, .md).

    A confirmations/ PDF (an e-filing confirmation notice) also gets a
    parsed-JSON sidecar saved next to it - Stage 1's confirmation-metadata
    extraction (extract_confirmation_details) still reads that sidecar's
    "paragraphs". A documents/ PDF (a case's main filings) does NOT get
    one: the event extraction pipeline reads those via Docling only now
    (see BaseStage.load_document_text's docstring) - the sidecar's
    paragraph reconstruction (walking Docling's hierarchical reading-order
    tree) could silently drop entire pages that Docling's own flat text
    export still captures, confirmed on a dense deposition transcript
    where it lost 88% of the document.

    Args:
        pdf_path: Path to PDF file
        skip_existing: Skip if Docling output already exists
        use_gpu: Use GPU acceleration

    Returns:
        Tuple of (success: bool, message: str)
    """
    docling_path = get_docling_dir(pdf_path) / f"{pdf_path.stem}.docling.json"

    try:
        # Check if already processed
        if skip_existing and docling_path.exists():
            return True, "Skipped (already exists)"

        # Parse the PDF (saves .docling.json/.md as a side effect)
        parsed = parse_pdf_document(
            pdf_path,
            use_gpu=use_gpu,
            extract_tables=True,
            extract_images=False,
            docling_dir=get_docling_dir(pdf_path),
        )

        if pdf_path.parent.name != "documents":
            save_parsed_document(parsed, pdf_path.with_suffix(".json"))

        return True, "Success"

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to parse {pdf_path}: {error_msg}")
        return False, f"Error: {error_msg[:100]}"


def parse_all_pdfs(
    data_dir: Path = Path("data"),
    case_id: str | None = None,
    skip_existing: bool = False,
    use_gpu: bool = True,
    progress_file: Any = None,
    max_workers: int = 8,
) -> dict[str, Any]:
    """
    Parse all PDFs in the data directory.

    Args:
        data_dir: Root data directory
        case_id: Optional case ID to filter
        skip_existing: Skip files that have already been processed
        use_gpu: Use GPU acceleration
        progress_file: Stream the tqdm progress bar is written to
            (default: sys.stderr). Useful when stderr has been redirected
            elsewhere and the progress bar still needs to reach a console.
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
                success, message = parse_and_save_pdf(
                    pdf_path,
                    skip_existing=skip_existing,
                    use_gpu=use_gpu,
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
                        skip_existing=skip_existing,
                        use_gpu=use_gpu,
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