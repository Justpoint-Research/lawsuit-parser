#!/usr/bin/env python3
"""Browse extracted event-candidate segments for a case.

Full structured proto-events (predicate + typed actor/date/location edges)
require GLiNER-Relex, which isn't implemented yet (relex.enabled = false in
config/extraction.toml runs a no-op stub, so 04_protoevents.json's
proto_events list is always empty). What IS available after stages 0-4 is
priority_segments: paragraphs flagged as event-bearing because they contain
a temporal expression, or a legal-action span plus a party mention.

This prints a case-level summary followed by those priority segments as a
table (timestamp / actors / one-line summary / source text / document /
page) - the closest thing to "browsing events" the pipeline currently
produces. Pass --detail for the full per-segment entity breakdown (label,
span text, confidence score) instead of the table.

For interactive filtering (e.g. by actor), see apps/event_browser.py instead.

Usage:
    uv run scripts/browse_events.py --case-id case_2132
    uv run scripts/browse_events.py --case-id case_2132 --detail
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.extraction.browse import (
    EVENT_TABLE_COLUMNS,
    CaseArtifacts,
    EventRow,
    build_event_rows,
    compute_case_summary,
    event_rows_to_table_dicts,
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

    print(
        f"Registry parties:  {summary.num_registry_parties} total "
        f"({summary.num_mentions} mentions, {summary.num_unresolved} unresolved)"
    )

    if summary.temporal_count:
        if summary.year_range and summary.year_range[0] != summary.year_range[1]:
            span_str = f"{summary.year_range[0]}-{summary.year_range[1]}"
        elif summary.year_range:
            span_str = str(summary.year_range[0])
        else:
            span_str = "no years detected"
        print(f"Time span:         {span_str} ({summary.temporal_count} temporal expressions)")

    label_str = ", ".join(
        f"{label}: {count}" for label, count in sorted(summary.label_counts.items(), key=lambda kv: -kv[1])
    )
    print(f"Entity spans:      {sum(summary.label_counts.values())} total ({label_str})")

    print(f"Priority segments (event candidates): {summary.num_priority_segments}")
    print(f"Proto-events (requires relex, currently disabled): {summary.num_proto_events}")
    print("=" * 78)
    print()


def print_detail(rows: list[EventRow]) -> None:
    """The original verbose view: full segment text plus every entity span
    with its label and confidence score."""
    for row in rows:
        print("=" * 78)
        print(f"[{row.seg_id}] {row.doc_id}  {row.section_type}  (page {row.page})")
        print("-" * 78)
        print(row.text.strip())

        if row.entities:
            print("\nEntities:")
            for label, span_text, score in row.entities:
                print(f"  [{label:22s}] {span_text!r}  (score={score:.2f})")

        if row.actors:
            print(f"\nActors: {', '.join(row.actors)}")

        print()


def main():
    parser = argparse.ArgumentParser(description="Browse extracted events for a case")
    parser.add_argument("--case-id", required=True, help="Case identifier (e.g., case_2132)")
    parser.add_argument("--data-root", default="data/cases", help="Root data directory")
    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print the full per-segment entity breakdown instead of the summary table",
    )
    args = parser.parse_args()

    artifacts = load_case_artifacts(args.case_id, Path(args.data_root))

    print_case_summary(artifacts)

    rows = build_event_rows(artifacts)
    if not rows:
        print("No priority segments found for this case.")
        return

    if args.detail:
        print_detail(rows)
        return

    print(f"EVENT CANDIDATES ({len(rows)})")
    print(render_ascii_table(EVENT_TABLE_COLUMNS, event_rows_to_table_dicts(rows)))


if __name__ == "__main__":
    main()
