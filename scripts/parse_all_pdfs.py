#!/usr/bin/env python
"""
Batch process all PDF documents in the data directory.

This script:
1. Finds all PDF files in data/cases/
2. Parses each PDF using Docling for structured extraction
3. Saves results to JSON files alongside the PDFs
4. Shows progress with tqdm
5. Handles errors gracefully and logs failures

Usage:
    python scripts/parse_all_pdfs.py
    python scripts/parse_all_pdfs.py --case-id 104
    python scripts/parse_all_pdfs.py --output-dir data/parsed
    python scripts/parse_all_pdfs.py --skip-existing
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

import click
from tqdm import tqdm

# Add parent directory to path to import lawsuit_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.parsers import parse_pdf_document, ParsedDocument


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('pdf_parsing.log'),
        logging.StreamHandler()
    ]
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


def parse_single_pdf(
    pdf_path: Path,
    output_path: Path,
    skip_existing: bool = False,
    use_gpu: bool = True,
) -> tuple[bool, str]:
    """
    Parse a single PDF and save results.

    Args:
        pdf_path: Path to PDF file
        output_path: Path to output JSON file
        skip_existing: Skip if output already exists
        use_gpu: Use GPU acceleration

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

        # Enrich with metadata from case JSON
        parsed = enrich_with_metadata(parsed, pdf_path)

        # Save to JSON
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(parsed.to_json(indent=2))

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
) -> dict[str, Any]:
    """
    Parse all PDFs in the data directory.

    Args:
        data_dir: Root data directory
        output_dir: Optional output directory (default: save alongside PDFs)
        case_id: Optional case ID to filter
        skip_existing: Skip files that have already been processed
        use_gpu: Use GPU acceleration

    Returns:
        Dictionary with summary statistics
    """
    # Find all PDFs
    pdfs = find_all_pdfs(data_dir, case_id)

    if not pdfs:
        logger.warning("No PDF files found")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    # Process each PDF with progress bar
    stats = {"total": len(pdfs), "success": 0, "failed": 0, "skipped": 0}
    failures = []

    with tqdm(total=len(pdfs), desc="Parsing PDFs", unit="file") as pbar:
        for pdf_path in pdfs:
            # Determine output path
            output_path = get_output_path(pdf_path, output_dir)

            # Parse the PDF
            success, message = parse_single_pdf(
                pdf_path,
                output_path,
                skip_existing=skip_existing,
                use_gpu=use_gpu,
            )

            # Update statistics
            if "Skipped" in message:
                stats["skipped"] += 1
            elif success:
                stats["success"] += 1
            else:
                stats["failed"] += 1
                failures.append((str(pdf_path), message))

            # Update progress bar
            pbar.set_postfix({
                "success": stats["success"],
                "failed": stats["failed"],
                "skipped": stats["skipped"]
            })
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

    return stats


@click.command()
@click.option(
    '--data-dir',
    type=str,
    default='data',
    help='Root data directory (default: data)'
)
@click.option(
    '--output-dir',
    type=str,
    default=None,
    help='Output directory for parsed JSONs (default: save alongside PDFs)'
)
@click.option(
    '--case-id',
    type=str,
    default=None,
    help='Process only specific case ID (e.g., 104)'
)
@click.option(
    '--skip-existing',
    is_flag=True,
    help='Skip files that have already been processed'
)
@click.option(
    '--no-gpu',
    is_flag=True,
    help='Disable GPU acceleration'
)
def main(data_dir: str, output_dir: str | None, case_id: str | None, skip_existing: bool, no_gpu: bool):
    """Parse all PDF documents and extract structured content.

    Examples:

      # Parse all PDFs in data directory

      python scripts/parse_all_pdfs.py

      # Parse only case 104

      python scripts/parse_all_pdfs.py --case-id 104

      # Save to separate output directory

      python scripts/parse_all_pdfs.py --output-dir data/parsed

      # Skip already processed files

      python scripts/parse_all_pdfs.py --skip-existing

      # Disable GPU acceleration

      python scripts/parse_all_pdfs.py --no-gpu
    """
    # Convert to Path objects
    data_dir_path = Path(data_dir)
    output_dir_path = Path(output_dir) if output_dir else None

    logger.info("Starting PDF parsing")
    logger.info(f"Data directory: {data_dir_path}")
    logger.info(f"Output directory: {output_dir_path or 'Same as PDF location'}")
    logger.info(f"Case ID filter: {case_id or 'All cases'}")
    logger.info(f"Skip existing: {skip_existing}")
    logger.info(f"GPU acceleration: {not no_gpu}")

    # Run the parser
    stats = parse_all_pdfs(
        data_dir=data_dir_path,
        output_dir=output_dir_path,
        case_id=case_id,
        skip_existing=skip_existing,
        use_gpu=not no_gpu,
    )

    # Exit with error code if any failures
    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
