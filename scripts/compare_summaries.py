#!/usr/bin/env python3
"""Compare Stage 3's LLM-generated document summaries against any summary
already present in a case's exported source metadata, and write a
side-by-side comparison to a CSV.

Metadata summaries come from whichever of these the case directory has:
- case_<id>/case_<id>.json (court_documents export) - no per-document
  summary field currently exists in that schema, so this is always empty
  for case_* directories today.
- mdl-<n>/metadata.json (MDL docket scrape) - documents[].summary, keyed
  by documents[].file.

Usage:
    python scripts/compare_summaries.py case_227
    python scripts/compare_summaries.py mdl-1358 --out mdl-1358_summaries.csv

    # Compare every case that has a summaries.json under data/extraction
    python scripts/compare_summaries.py
"""

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lawsuit_parser.event_extraction.models import SummariesArtifact

CSV_FIELDS = [
    "case_id",
    "doc_id",
    "file_name",
    "our_summary",
    "metadata_summary",
    "metadata_available",
    "model",
]


def load_our_summaries(output_root: Path, case_id: str) -> dict[str, dict]:
    """doc_id -> {file_name, summary, model} from Stage 3's summaries.json."""
    path = output_root / case_id / "events" / "summaries.json"
    if not path.exists():
        return {}

    artifact = SummariesArtifact.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return {
        doc.doc_id: {"file_name": doc.file_name, "summary": doc.summary, "model": doc.model}
        for doc in artifact.documents
    }


def load_metadata_summaries(data_root: Path, case_id: str) -> dict[str, str]:
    """file basename -> summary already present in the case's exported
    source metadata, if any (see module docstring for the two known
    shapes). Returns an empty dict if neither file exists or neither has
    any per-document summary.
    """
    case_dir = data_root / case_id

    case_json = case_dir / f"{case_id}.json"
    if case_json.exists():
        data = json.loads(case_json.read_text(encoding="utf-8"))
        summaries = {}
        for doc in data.get("documents", []):
            summary = doc.get("summary")
            file_path = doc.get("local_document_path") or doc.get("document_bucket_link")
            if summary and file_path:
                summaries[Path(file_path).name] = summary
        return summaries

    metadata_json = case_dir / "metadata.json"
    if metadata_json.exists():
        data = json.loads(metadata_json.read_text(encoding="utf-8"))
        summaries = {}
        for doc in data.get("documents", []):
            summary = doc.get("summary")
            file_path = doc.get("file")
            if summary and file_path:
                summaries[Path(file_path).name] = summary
        return summaries

    return {}


def compare_case(data_root: Path, output_root: Path, case_id: str) -> list[dict]:
    """Build one comparison row per document Stage 3 produced a summary
    entry for, whether or not it actually has a summary or a metadata
    match."""
    ours = load_our_summaries(output_root, case_id)
    metadata = load_metadata_summaries(data_root, case_id)

    rows = []
    for doc_id, info in sorted(ours.items()):
        file_name = info["file_name"]
        metadata_summary = metadata.get(file_name, "")
        rows.append({
            "case_id": case_id,
            "doc_id": doc_id,
            "file_name": file_name,
            "our_summary": info["summary"] or "",
            "metadata_summary": metadata_summary,
            "metadata_available": bool(metadata_summary),
            "model": info["model"] or "",
        })
    return rows


def discover_case_ids(output_root: Path) -> list[str]:
    if not output_root.exists():
        return []
    return sorted(
        d.name for d in output_root.iterdir()
        if d.is_dir() and (d / "events" / "summaries.json").exists()
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare our document summaries against source metadata summaries, side by side, into a CSV."
    )
    parser.add_argument(
        "case_id",
        nargs="?",
        help="Case to compare (e.g. case_227, mdl-1358). If omitted, compares every "
             "case under --output-root that has a summaries.json.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=REPO_ROOT / "data" / "cases",
        help="Source case data root (default: data/cases)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "data" / "extraction",
        help="Pipeline output root (default: data/extraction)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "data" / "summary_comparison.csv",
        help="Output CSV path (default: data/summary_comparison.csv)",
    )
    args = parser.parse_args()

    case_ids = [args.case_id] if args.case_id else discover_case_ids(args.output_root)
    if not case_ids:
        print(f"No cases with a summaries.json found under {args.output_root}")
        return 1

    all_rows = []
    for case_id in case_ids:
        rows = compare_case(args.data_root, args.output_root, case_id)
        if not rows:
            print(f"  {case_id}: no summaries.json found, skipping")
            continue
        all_rows.extend(rows)
        with_metadata = sum(1 for r in rows if r["metadata_available"])
        print(f"  {case_id}: {len(rows)} documents, {with_metadata} with a metadata summary to compare against")

    if not all_rows:
        print("Nothing to write.")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
