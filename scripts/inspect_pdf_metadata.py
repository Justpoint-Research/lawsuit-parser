#!/usr/bin/env python3
"""Inspect PDF metadata from exported case files.

This script examines the PDF metadata (author, creation date, producer, etc.)
to determine if it contains useful information not already in the database.
"""

import argparse
import json
import subprocess
from pathlib import Path


def extract_pdf_metadata(pdf_path: Path) -> dict:
    """Extract metadata from a PDF file using pdfinfo command.

    Args:
        pdf_path: Path to PDF file.

    Returns:
        Dictionary with PDF metadata.
    """
    try:
        # Try using pdfinfo command-line tool
        result = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            metadata = {}
            for line in result.stdout.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    metadata[key.strip()] = value.strip()
            return metadata
        else:
            # Fall back to manual approach
            return extract_pdf_metadata_manual(pdf_path)

    except FileNotFoundError:
        # pdfinfo not available, use manual approach
        return extract_pdf_metadata_manual(pdf_path)
    except Exception as e:
        return {"error": str(e)}


def extract_pdf_metadata_manual(pdf_path: Path) -> dict:
    """Extract basic PDF metadata manually by reading PDF file.

    Args:
        pdf_path: Path to PDF file.

    Returns:
        Dictionary with basic PDF metadata.
    """
    try:
        # Read first 8KB of PDF to look for metadata
        with open(pdf_path, "rb") as f:
            header = f.read(8192)

        # Try to find basic metadata in PDF
        result = {}

        # Get file size
        result["File size"] = f"{pdf_path.stat().st_size} bytes"

        # Try to find PDF version
        if header.startswith(b"%PDF-"):
            version = header[5:8].decode('latin-1', errors='ignore')
            result["PDF version"] = version

        # Search for common metadata fields in the header
        header_str = header.decode('latin-1', errors='ignore')

        for field in ["/Author", "/Creator", "/Producer", "/Title", "/Subject", "/CreationDate", "/ModDate"]:
            if field in header_str:
                # Try to extract the value
                idx = header_str.index(field)
                # Look for value in parentheses or angle brackets
                chunk = header_str[idx:idx+200]
                if '(' in chunk and ')' in chunk:
                    start = chunk.index('(') + 1
                    end = chunk.index(')', start)
                    value = chunk[start:end]
                    result[field.lstrip('/')] = value
                elif '<' in chunk and '>' in chunk:
                    start = chunk.index('<') + 1
                    end = chunk.index('>', start)
                    value = chunk[start:end]
                    result[field.lstrip('/')] = value

        if not result or len(result) <= 2:
            result["note"] = "Limited metadata extraction - pdfinfo tool not available"

        return result

    except Exception as e:
        return {"error": str(e)}


def inspect_case_pdfs(case_dir: Path):
    """Inspect all PDFs in a case directory."""

    print("=" * 80)
    print(f"PDF METADATA INSPECTION - {case_dir.name}")
    print("=" * 80)

    # Find JSON file
    json_files = list(case_dir.glob("*.json"))
    if not json_files:
        print("No JSON file found")
        return

    json_path = json_files[0]
    with open(json_path) as f:
        case_data = json.load(f)

    print(f"\nCase: {case_data['summary']['case_id']}")
    print(f"Caption: {case_data['summary']['caption']}")
    print(f"Total Documents: {case_data['summary']['total_documents']}\n")

    # Inspect each document
    for doc in case_data["documents"]:
        doc_id = doc["id"]
        doc_name = doc["document_name"]

        print("=" * 80)
        print(f"Document {doc_id}: {doc_name}")
        print("=" * 80)

        # Check main document
        if "local_document_path" in doc:
            doc_pdf_path = case_dir / doc["local_document_path"]
            if doc_pdf_path.exists():
                print(f"\n📄 Main Document: {doc_pdf_path.name}")
                print(f"   Database metadata:")
                print(f"      Filed by: {doc.get('filed_by')}")
                print(f"      Filed create: {doc.get('filed_create')}")
                print(f"      Filed received: {doc.get('filed_received')}")
                print(f"      Document status: {doc.get('document_status')}")

                print(f"\n   PDF File Metadata:")
                metadata = extract_pdf_metadata(doc_pdf_path)
                for key, value in sorted(metadata.items()):
                    if value:
                        print(f"      {key}: {value}")
            else:
                print(f"\n   Main document not found: {doc_pdf_path}")

        # Check confirmation document
        if "local_confirmation_path" in doc:
            confirm_pdf_path = case_dir / doc["local_confirmation_path"]
            if confirm_pdf_path.exists():
                print(f"\n📝 Confirmation Document: {confirm_pdf_path.name}")

                print(f"\n   PDF File Metadata:")
                metadata = extract_pdf_metadata(confirm_pdf_path)
                for key, value in sorted(metadata.items()):
                    if value:
                        print(f"      {key}: {value}")
            else:
                print(f"\n   Confirmation document not found: {confirm_pdf_path}")

        print()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Inspect PDF metadata from exported case."
    )
    parser.add_argument(
        "case_id",
        type=int,
        help="Case ID",
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=Path("data/cases"),
        help="Directory containing exported cases (default: data/cases)",
    )

    args = parser.parse_args()

    case_dir = args.cases_dir / f"case_{args.case_id}"
    if not case_dir.exists():
        print(f"Case directory not found: {case_dir}")
        return

    inspect_case_pdfs(case_dir)


if __name__ == "__main__":
    main()
