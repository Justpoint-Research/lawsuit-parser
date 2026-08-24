#!/usr/bin/env python3
"""Browse event_extraction pipeline output for a case.

Reads the Stage 1/2/3 artifacts under data/extraction/<case_id>/events/ (see
`make extract-events` / scripts/run_event_extraction.py). A real event/
timeline concept (Stage 4+) isn't built yet, so this browses at document
granularity: one row per scanned document, showing the dates Stage 1 found
in it, the actors Stage 2 linked to it, and the short summary Stage 3
generated for it - the closest thing to "browsing events" the pipeline
currently produces. Pass --detail for the full per-document entity
breakdown (label, span text, confidence score, context) instead of the table.

For interactive filtering (e.g. by actor), see apps/event_browser.py instead.

Usage:
    uv run scripts/browse_events.py --case-id case_95
    uv run scripts/browse_events.py --case-id case_95 --detail
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.event_extraction.browse import (
    EVENT_TABLE_COLUMNS,
    CaseArtifacts,
    DocumentRow,
    build_document_rows,
    compute_case_summary,
    document_rows_to_table_dicts,
    load_case_artifacts,
    render_ascii_table,
)


def print_case_summary(artifacts: CaseArtifacts) -> None:
    summary = compute_case_summary(artifacts)

    print("=" * 78)
    print(f"CASE SUMMARY: {summary.case_id}")
    print("=" * 78)
    if summary.caption:
        print(f"Caption:           {summary.caption}")
    if summary.court:
        print(f"Court:             {summary.court}")
    print(f"Documents:         {summary.num_documents}")

    if summary.plaintiffs:
        print(f"{'Plaintiff:':<19}{', '.join(summary.plaintiffs)}")
    if summary.defendants:
        print(f"{'Defendant:':<19}{', '.join(summary.defendants)}")

    if summary.other_roles:
        other_str = "; ".join(f"{role}: {', '.join(names)}" for role, names in summary.other_roles.items())
        print(f"Other parties:     {other_str}")

    if summary.products:
        print(f"Accused products:  {', '.join(summary.products)}")

    print(f"Parties/roles:     {summary.num_parties} total")

    if summary.temporal_count:
        if summary.year_range and summary.year_range[0] != summary.year_range[1]:
            span_str = f"{summary.year_range[0]}-{summary.year_range[1]}"
        elif summary.year_range:
            span_str = str(summary.year_range[0])
        else:
            span_str = "no years detected"
        print(f"Time span:         {span_str} ({summary.temporal_count} dates found)")

    label_str = ", ".join(
        f"{label}: {count}" for label, count in sorted(summary.label_counts.items(), key=lambda kv: -kv[1])
    )
    print(f"Entities:          {summary.num_entities} total ({summary.num_linked} linked to a known actor)")
    if label_str:
        print(f"  by label:        {label_str}")

    if summary.gliner_model:
        print(f"GLiNER model:      {summary.gliner_model} (threshold={summary.gliner_threshold})")
    print("=" * 78)
    print()


def print_detail(rows: list[DocumentRow]) -> None:
    """Full per-document breakdown: dates found, plus every entity with its
    label, confidence score, and surrounding context."""
    for row in rows:
        print("=" * 78)
        print(f"[{row.doc_id}] {row.file_name}")
        if row.document_title:
            print(row.document_title)
        print("-" * 78)

        if row.filing_date:
            print(f"Filed: {row.filing_date}" + (f" (by {row.filed_by})" if row.filed_by else ""))

        if row.summary:
            print(f"\nSummary: {row.summary}")

        if row.dates:
            print("\nDates found:")
            for dtype, text in row.dates:
                print(f"  [{dtype:14s}] {text}")

        if row.entities:
            print("\nEntities:")
            for label, span_text, score in row.entities:
                print(f"  [{label:35s}] {span_text!r}  (score={score:.2f})")

        if row.actors:
            print(f"\nActors: {', '.join(row.actors)}")

        print()


def main():
    parser = argparse.ArgumentParser(description="Browse event_extraction pipeline output for a case")
    parser.add_argument("--case-id", required=True, help="Case identifier (e.g., case_95)")
    parser.add_argument("--data-root", default="data/cases", help="Root directory for source case data (caption lookup)")
    parser.add_argument("--output-root", default="data/extraction", help="Root directory for extraction artifacts")
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print the full per-document entity breakdown instead of the summary table",
    )
    args = parser.parse_args()

    artifacts = load_case_artifacts(args.case_id, Path(args.data_root), Path(args.output_root))

    print_case_summary(artifacts)

    rows = build_document_rows(artifacts)
    if not rows:
        print("No documents found for this case.")
        return

    if args.detail:
        print_detail(rows)
        return

    print(f"DOCUMENTS ({len(rows)})")
    print(render_ascii_table(EVENT_TABLE_COLUMNS, document_rows_to_table_dicts(rows)))


if __name__ == "__main__":
    main()
