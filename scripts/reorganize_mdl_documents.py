#!/usr/bin/env python3
"""Reorganize MDL document PDFs into per-MDL case folders.

Reads the MDL summary CSV in medical-research-data/, groups its rows by
MDL number (the CSV only states the MDL # / Product on the first row of
each group; later rows are blank and inherit the previous value), copies
each row's PDF from the content-addressed medical-research-data/ store
into data/cases/mdl-<number>/documents/, and writes a metadata.json per
MDL with product, document #, title, link and summary for every document.

Usage:
    python scripts/reorganize_mdl_documents.py
    python scripts/reorganize_mdl_documents.py --dry-run
"""

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

GCS_PREFIX = "gs://courts_crawl/mdl/documents/"


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [row for row in reader if any(v.strip() for v in row.values() if v)]

    current_mdl = None
    current_product = None
    for row in rows:
        if row["MDL #"].strip():
            current_mdl = row["MDL #"].strip()
            current_product = row["Product"].strip()
        row["MDL #"] = current_mdl
        row["Product"] = current_product
    return rows


def group_by_mdl(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["MDL #"], []).append(row)
    return groups


def resolve_source_path(source_dir: Path, gcs: str) -> Path | None:
    gcs = gcs.strip()
    if not gcs.startswith(GCS_PREFIX):
        return None
    return source_dir / gcs[len(GCS_PREFIX) :]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reorganize MDL document PDFs into per-MDL case folders."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("medical-research-data/MDL documents - MDLs summary.csv"),
        help="Path to the MDL summary CSV.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("medical-research-data"),
        help="Directory containing the content-addressed PDF store.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cases"),
        help="Directory to write mdl-<number> folders into (default: data/cases).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without copying files or writing JSON.",
    )
    args = parser.parse_args()

    rows = load_rows(args.csv)
    groups = group_by_mdl(rows)

    total_copied = 0
    total_missing = 0
    total_no_file = 0

    for mdl_number, mdl_rows in sorted(groups.items(), key=lambda kv: int(kv[0])):
        product = mdl_rows[0]["Product"]
        mdl_dir = args.output_dir / f"mdl-{mdl_number}"
        docs_dir = mdl_dir / "documents"
        if not args.dry_run:
            docs_dir.mkdir(parents=True, exist_ok=True)

        documents = []
        for row in mdl_rows:
            gcs = row["GCS"].strip()
            src = resolve_source_path(args.source_dir, gcs) if gcs else None

            file_name = None
            if src is None:
                total_no_file += 1
            elif not src.exists():
                print(f"  [missing] mdl-{mdl_number}: {src}")
                total_missing += 1
            else:
                file_name = src.name
                if not args.dry_run:
                    shutil.copy2(src, docs_dir / file_name)
                total_copied += 1

            documents.append(
                {
                    "document_number": row["Document #"].strip() or None,
                    "title": row["Document Title"].strip() or None,
                    "link": row["Document Link"].strip() or None,
                    "date": row["Date"].strip() or None,
                    "summary": row["Summary"].strip() or None,
                    "file": f"documents/{file_name}" if file_name else None,
                }
            )

        metadata = {
            "mdl_number": mdl_number,
            "product": product,
            "document_count": len(documents),
            "documents": documents,
        }

        print(f"mdl-{mdl_number} ({product}): {len(documents)} documents")
        if not args.dry_run:
            with open(mdl_dir / "metadata.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)

    print()
    print(f"MDLs processed: {len(groups)}")
    print(f"Files copied:   {total_copied}")
    print(f"Files missing:  {total_missing} (referenced in CSV but not found on disk)")
    print(f"No file in CSV: {total_no_file} (row had no GCS link)")
    if args.dry_run:
        print("\n(dry run — no files or JSON written)")


if __name__ == "__main__":
    sys.exit(main())
