#!/usr/bin/env python3
"""Parse all PDFs in a case directory for extraction pipeline.

This script parses all PDFs in a case directory using Docling and saves
the .docling.json files needed by the extraction pipeline.
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.parsers.pdf_parser import parse_pdf_document


def parse_case_pdfs(case_id: str, data_root: Path):
    """Parse all PDFs in a case directory.

    Args:
        case_id: Case identifier (e.g., "case_67")
        data_root: Root data directory
    """
    case_dir = data_root / case_id
    if not case_dir.exists():
        print(f"Error: Case directory not found: {case_dir}")
        sys.exit(1)

    # Find all PDFs in documents subdirectory
    docs_dir = case_dir / "documents"
    if not docs_dir.exists():
        print(f"Error: Documents directory not found: {docs_dir}")
        sys.exit(1)

    pdf_files = sorted(docs_dir.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDF files found in {docs_dir}")
        sys.exit(1)

    print(f"Found {len(pdf_files)} PDF files in {case_id}")
    print(f"Parsing with Docling (this may take a few minutes)...\n")

    # Move parsed documents to case root directory
    # The extraction pipeline expects .docling.json files at case_dir/<filename>.docling.json
    for i, pdf_file in enumerate(pdf_files, 1):
        print(f"[{i}/{len(pdf_files)}] Parsing {pdf_file.name}...")

        try:
            # Parse the PDF
            # This will create pdf_file.docling.json in the same directory
            parsed = parse_pdf_document(
                pdf_file,
                save_docling_document=True,
                save_markdown=True,
                extract_images=False,
            )

            # Move the .docling.json file to case root with doc_NNN naming
            docling_file = pdf_file.with_suffix(".docling.json")
            if docling_file.exists():
                # Rename to doc_000.docling.json, doc_001.docling.json, etc.
                new_name = case_dir / f"doc_{i-1:03d}.docling.json"
                docling_file.rename(new_name)
                print(f"  Saved: {new_name.name}")

            # Also move markdown
            md_file = pdf_file.with_suffix(".md")
            if md_file.exists():
                new_md = case_dir / f"doc_{i-1:03d}.md"
                md_file.rename(new_md)

        except Exception as e:
            print(f"  Error parsing {pdf_file.name}: {e}")
            continue

    print(f"\n✓ Parsed {len(pdf_files)} documents")
    print(f"✓ Ready for extraction pipeline")


def main():
    parser = argparse.ArgumentParser(
        description="Parse case PDFs for extraction pipeline"
    )
    parser.add_argument("case_id", help="Case identifier (e.g., case_67)")
    parser.add_argument(
        "--data-root",
        default="data/cases",
        help="Root data directory (default: data/cases)",
    )

    args = parser.parse_args()

    data_root = Path(args.data_root)
    parse_case_pdfs(args.case_id, data_root)


if __name__ == "__main__":
    main()
