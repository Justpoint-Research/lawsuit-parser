#!/usr/bin/env python3
"""Export Texas court case metadata to denormalized JSON files.

Texas's after_search table (courts_final.tx_cases_after_search - cases
matched to the mass-tort defendant search terms, same concept as NY's
ny_cases_after_search / FL's fl_cases_after_search) uses a job-queue scraper
(tx_jobs / tx_job_results / tx_workers). Parsed document rows live in
courts_final.tx_documents, keyed on (coa, case_number) - the same business key
tx_cases_after_search is unique on (its integer ids are independent of
tx_documents', so never join on them). Every tx_documents row is one
downloadable file (file_url is always set) under a docket event identified by
(section, row_seq), with section 'event' (docket entries) or 'brief'; one
event can carry several files (doc_seq). So:

- ``documents`` = every tx_documents row for the case, ordered by
  (section, row_seq, doc_seq), each keeping its event fields.
- ``case_history`` = one entry per distinct (section, row_seq) event, derived
  from those rows (event_date/event_type/disposition/description, plus how
  many documents the event carries). There is no separate docket-entry table.

Only ~half of tx_cases_after_search has documents; cases never reached by the
files flow are skipped by default (--no-require-documents exports them with
``documents: []``, like the other states). And tx_documents is dominated by
cases outside after_search, so the query joins in SQL rather than pulling the
whole 2.8M-row table.

courts_final.tx_cases is not used as a case_history source here: despite the
name, it isn't a per-case snapshot history table (compare NY's ny_cases,
which is exactly that) - it's a separate, larger case population that
overlaps with tx_cases_after_search only on the (coa, case_number) business
key (its own ``id`` column is an independent sequence - a same-id join
matches unrelated cases), and it carries no columns that
tx_cases_after_search doesn't already have. History comes from tx_documents
events instead (see above).

Metadata-only (no GCS downloads); tx_documents.storage_path / file_url point at
the underlying files. Scrape-queue bookkeeping (attempts, claims, priority) is
left out.

Usage:
    python scripts/export_tx_cases.py --output-dir data/cases/tx_after_search
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
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


def _clean(value):
    """NaN/NaT -> None so the JSON is valid (json.dump would emit bare NaN)."""
    if value is pd.NaT or (isinstance(value, float) and value != value):
        return None
    return value


def export_tx_cases(
    output_dir: Path,
    require_documents: bool = True,
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
               files_status, files_fetched_at, files_error, files_count,
               created_at, updated_at
        FROM {SCHEMA}.tx_cases_after_search
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )

    stats = {"total": len(cases_df), "successful": 0, "skipped": 0, "failed": 0, "no_documents": 0}
    if cases_df.empty:
        return stats

    print(f"Fetched {len(cases_df)} cases. Fetching documents...")
    docs_df = fetch_from_postgres(
        f"""
        SELECT d.id, d.coa, d.case_number, d.section, d.row_seq, d.doc_seq,
               d.event_date, d.event_type, d.disposition, d.description,
               d.remarks, d.doc_label, d.media_version_id, d.media_id,
               d.doc_type, d.file_url, d.size_label, d.size_estimate,
               d.job_id, d.download_status, d.downloaded_at,
               d.downloaded_size, d.storage_path, d.content_type,
               d.download_error, d.created_at, d.updated_at
        FROM {SCHEMA}.tx_documents d
        JOIN {SCHEMA}.tx_cases_after_search a
          ON a.coa = d.coa AND a.case_number = d.case_number
        ORDER BY d.coa, d.case_number, d.section, d.row_seq, d.doc_seq
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )
    docs_by_case = defaultdict(list)
    for row in docs_df.to_dict("records"):
        docs_by_case[(row["coa"], row["case_number"])].append(
            {k: _clean(v) for k, v in row.items()}
        )
    print(f"Fetched {len(docs_df)} documents. Building per-case JSON...")

    for case_row in tqdm(
        [{k: _clean(v) for k, v in row.items()} for row in cases_df.to_dict("records")],
        desc="Exporting TX cases",
        unit="case",
    ):
        case_id = case_row["id"]

        case_dir = output_dir / f"case_{case_id}"
        json_path = case_dir / f"case_{case_id}.json"
        if skip_if_exists and json_path.exists():
            stats["skipped"] += 1
            continue

        documents = docs_by_case.get((case_row["coa"], case_row["case_number"]), [])
        if not documents and require_documents:
            stats["no_documents"] += 1
            continue

        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            # One history entry per distinct docket event (section, row_seq);
            # documents are already ordered by (section, row_seq, doc_seq), so
            # the first document of each event supplies its event fields.
            events: dict[tuple, dict] = {}
            for doc in documents:
                key = (doc["section"], doc["row_seq"])
                if key not in events:
                    events[key] = {
                        "section": doc["section"],
                        "row_seq": doc["row_seq"],
                        "event_date": doc["event_date"],
                        "event_type": doc["event_type"],
                        "disposition": doc["disposition"],
                        "description": doc["description"],
                        "remarks": doc["remarks"],
                        "documents_count": 0,
                    }
                events[key]["documents_count"] += 1
            case_history = list(events.values())
            denormalized_case = {
                "case_info": case_row,
                "documents": documents,
                "case_history": case_history,
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
                    "total_documents": len(documents),
                    "total_history_entries": len(case_history),
                },
                "summary": {
                    "total_documents": len(documents),
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
        "--no-require-documents",
        dest="require_documents",
        action="store_false",
        help="also export cases with zero documents (skipped by default, matching the other states)",
    )
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
        require_documents=args.require_documents,
        skip_if_exists=not args.overwrite,
        force_refresh=args.overwrite,
    )

    print("\n" + "=" * 80)
    print("Export complete!")
    print(f"  Successful: {stats['successful']}/{stats['total']}")
    print(f"  Skipped: {stats['skipped']}/{stats['total']} (already exported)")
    print(f"  No documents: {stats['no_documents']}/{stats['total']}")
    print(f"  Failed: {stats['failed']}/{stats['total']}")
    print(f"  Output directory: {args.output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    main()
