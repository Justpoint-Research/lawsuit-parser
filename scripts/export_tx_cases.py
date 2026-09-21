#!/usr/bin/env python3
"""Export Texas court case metadata to denormalized JSON files.

Texas's after_search table (courts_final.tx_cases_after_search - cases
matched to the mass-tort defendant search terms, same concept as NY's
ny_cases_after_search / FL's fl_cases_after_search) uses a job-queue scraper
(tx_jobs / tx_job_results / tx_workers) instead of per-document child tables.
Document-level data isn't parsed into rows anywhere yet - tx_job_results
stores raw unparsed HTML in its ``captures`` jsonb column (the "tx_files"
flow) rather than a structured tx_documents table like FL's
fl_docket_documents or IL's il_documents/il_document_files. So, unlike those
two, this export is case-metadata-only: ``documents`` is always ``[]`` until
someone builds an HTML-parsing pass over tx_job_results.captures.

courts_final.tx_cases is not used as a case_history source here: despite the
name, it isn't a per-case snapshot history table (compare NY's ny_cases,
which is exactly that) - it's a separate, larger case population that
overlaps with tx_cases_after_search only on the (coa, case_number) business
key (its own ``id`` column is an independent sequence - a same-id join
matches unrelated cases), and it carries no columns that
tx_cases_after_search doesn't already have. So there is nothing here for it
to usefully contribute; case_history is always [].

Usage:
    python scripts/export_tx_cases.py --output-dir data/cases/tx_after_search
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils.db import fetch_from_postgres

logging.basicConfig(
    filename="tx_export.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SCRAPPING_DB_PORT = 5433
SCHEMA = "courts_final"


def export_tx_cases(
    output_dir: Path,
    skip_if_exists: bool = True,
    force_refresh: bool = False,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching tx_cases_after_search rows...")
    cases_df = fetch_from_postgres(
        f"""
        SELECT id, coa, case_number, case_url, date_filed, style, style_v,
               case_type, coa_case_number, coa_case_url,
               trial_court_case_number, trial_court_county, trial_court,
               appellate_court, query, query_kind, term_id, job_id,
               files_count, created_at, updated_at
        FROM {SCHEMA}.tx_cases_after_search
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )

    stats = {"total": len(cases_df), "successful": 0, "skipped": 0, "failed": 0, "no_documents": 0}
    if cases_df.empty:
        return stats

    print(f"Fetched {len(cases_df)} cases. Building per-case JSON...")

    for case_row in tqdm(cases_df.to_dict("records"), desc="Exporting TX cases", unit="case"):
        case_id = case_row["id"]

        case_dir = output_dir / f"case_{case_id}"
        json_path = case_dir / f"case_{case_id}.json"
        if skip_if_exists and json_path.exists():
            stats["skipped"] += 1
            continue

        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            denormalized_case = {
                "case_info": case_row,
                # Always empty - see module docstring: TX has no parsed
                # per-document table yet, only raw HTML captures.
                "documents": [],
                "case_history": [],
                # Same field names/shape as CaseExporter's "normalized" block
                # (lawsuit_parser/utils/case_exporter.py) and export_fl_cases.py/
                # export_il_cases.py's own - a consistent cross-state view
                # alongside the untouched, state-specific case_info above.
                # unique_key includes coa: case_number alone repeats across
                # different courts of appeals.
                "normalized": {
                    "state": "tx",
                    "internal_id": case_id,
                    "case_number": case_row.get("case_number"),
                    "unique_key": f"{case_row.get('coa')}:{case_row.get('case_number')}",
                    "caption": case_row.get("style"),
                    "court": case_row.get("trial_court") or case_row.get("appellate_court"),
                    "case_status": None,
                    "case_type": case_row.get("case_type"),
                    "filed_date": case_row.get("date_filed"),
                    "total_documents": 0,
                    "total_history_entries": 0,
                },
                "summary": {
                    "total_documents": 0,
                    "case_number": case_row.get("case_number"),
                    "style": case_row.get("style"),
                    "case_type": case_row.get("case_type"),
                    "files_downloaded": False,
                    "text_extraction_enabled": False,
                    "exported_at": datetime.now().isoformat(),
                },
            }
            # DVC checks out cached files read-only (0444); truncate-writing
            # into one raises PermissionError even for the owner.
            if json_path.exists():
                json_path.unlink()
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(denormalized_case, f, indent=2, default=str)
            stats["successful"] += 1
        except Exception as e:
            logger.warning(f"Failed to export TX case {case_id}: {e}")
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=Path("data/cases/tx_after_search"))
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-fetch and overwrite cases whose JSON already exists instead "
        "of skipping them, and bypass fetch_from_postgres's query cache "
        "(default: skip + reuse cache). Use this to refresh a stale export.",
    )
    args = parser.parse_args()

    print("=" * 80)
    print("Texas Case Exporter")
    print("=" * 80)

    stats = export_tx_cases(
        args.output_dir,
        skip_if_exists=not args.overwrite,
        force_refresh=args.overwrite,
    )

    print("\n" + "=" * 80)
    print("Export complete!")
    print(f"  Successful: {stats['successful']}/{stats['total']}")
    print(f"  Skipped: {stats['skipped']}/{stats['total']} (already exported)")
    print(f"  Failed: {stats['failed']}/{stats['total']}")
    print(f"  Output directory: {args.output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    main()
