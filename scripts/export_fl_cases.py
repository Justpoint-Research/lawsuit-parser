#!/usr/bin/env python3
"""Export Florida court case metadata to denormalized JSON files.

Florida's after_search table (courts_final.fl_cases_after_search - cases
matched to the mass-tort defendant search terms, same concept as NY's
ny_cases_after_search) has a different column set than NY and joins to its
documents table on case_instance_uuid rather than a docket_id, so it can't go
through scripts/export_cases.py / CaseExporter. This mirrors that script's
shape (bulk-fetch the tables once, group in memory, write one denormalized
case_<id>.json per case) but targets fl_cases_after_search / fl_docket_documents
instead.

Also pulls in two tables the original version of this script never touched:
- fl_cases: a fuller per-case row (case_caption, location, group/panel flags)
  than fl_cases_after_search carries - merged into case_info.
- fl_docket_entries: the actual docket/timeline entries for each case -
  fills case_history (previously always []), mirroring how NY's case_history
  comes from ny_cases.

case_instance_uuid is the only key shared consistently across all of FL's
tables. fl_cases_after_search.id and fl_docket_entries.case_id / fl_cases.id
are independent id sequences (confirmed by joining a sample: matching ids
pointed at unrelated case_numbers) - NEVER join FL tables on the bare
integer id/case_id across tables. Joining fl_docket_documents by case_id
(the previous behavior) undercounted documents by more than half compared to
joining by case_instance_uuid (1,243 vs 2,827 matched documents in a
same-day check) - that join has been fixed here.

Metadata-only by design (no GCS downloads) - fl_docket_documents.storage_path /
document_url point at the underlying files if a download pass is added later.

Usage:
    python scripts/export_fl_cases.py --output-dir data/cases/fl_after_search
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
    filename="fl_export.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SCRAPPING_DB_PORT = 5433
SCHEMA = "courts_final"

# Substantive extra fields fl_cases carries beyond fl_cases_after_search
# (excludes fl_cases' own scrape-bookkeeping columns: details_status,
# details_attempts, details_claimed_at, details_claimed_by, details_fetched_at,
# details_error - and the columns already present on fl_cases_after_search).
FL_CASES_EXTRA_FIELDS = [
    "case_caption",
    "case_class_group_type",
    "case_class_group_type_id",
    "location",
    "location_id",
    "case_group_flag",
    "panel_flag",
]


def _pg_text_array(values: list[str]) -> str:
    """Render strings as a Postgres ``ARRAY[...]::text[]`` literal.

    Mirrors lawsuit_parser.utils.case_exporter._pg_text_array - embedding the
    id list in the query text (rather than a bound parameter) is what lets
    fetch_from_postgres's query-hash cache reuse a prior run's result.
    """
    return "ARRAY[" + ",".join("'" + str(v).replace("'", "''") + "'" for v in values) + "]::text[]"


def export_fl_cases(
    output_dir: Path,
    require_documents: bool = True,
    skip_if_exists: bool = True,
    force_refresh: bool = False,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching fl_cases_after_search rows...")
    cases_df = fetch_from_postgres(
        f"""
        SELECT id, case_instance_uuid, case_number, case_title, closed_flag,
               case_classification, case_classification_id, court_id,
               filed_date, originating_court_cases, query, query_kind,
               created_at, updated_at
        FROM {SCHEMA}.fl_cases_after_search
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )

    stats = {"total": len(cases_df), "successful": 0, "skipped": 0, "failed": 0, "no_documents": 0}
    if cases_df.empty:
        return stats

    case_uuids = cases_df["case_instance_uuid"].dropna().unique().tolist()

    print(f"Fetched {len(cases_df)} cases. Fetching fuller fl_cases rows for {len(case_uuids)} cases...")
    full_cases_df = fetch_from_postgres(
        f"""
        SELECT case_instance_uuid, {", ".join(FL_CASES_EXTRA_FIELDS)}
        FROM {SCHEMA}.fl_cases
        WHERE case_instance_uuid = ANY({_pg_text_array(case_uuids)})
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )
    full_case_by_uuid = full_cases_df.set_index("case_instance_uuid").to_dict("index")

    print(f"Fetched {len(full_cases_df)} fl_cases rows. Fetching documents for {len(case_uuids)} cases...")
    docs_df = fetch_from_postgres(
        f"""
        SELECT id, case_id, case_instance_uuid, docket_entry_uuid,
               document_link_uuid, document_name, document_type, content_type,
               file_extension, page_count, file_size, user_document_state,
               document_url, download_status, downloaded_at, downloaded_size,
               storage_path, created_at, updated_at
        FROM {SCHEMA}.fl_docket_documents
        WHERE case_instance_uuid = ANY({_pg_text_array(case_uuids)})
        ORDER BY id
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )
    print(f"Fetched {len(docs_df)} documents. Fetching docket entries for {len(case_uuids)} cases...")
    entries_df = fetch_from_postgres(
        f"""
        SELECT id, case_id, case_instance_uuid, docket_entry_uuid, filed_date,
               submitted_date, docket_entry_type, docket_entry_type_id,
               docket_entry_sub_type, docket_entry_sub_type_id,
               docket_entry_name, docket_entry_status, docket_entry_status_id,
               docket_entry_description, official, document_count,
               secured_document, security1, security2, security3, security4,
               security5, composite_security, submitted_by, created_at,
               updated_at
        FROM {SCHEMA}.fl_docket_entries
        WHERE case_instance_uuid = ANY({_pg_text_array(case_uuids)})
        ORDER BY case_instance_uuid, filed_date
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )
    print(f"Fetched {len(entries_df)} docket entries. Building per-case JSON...")

    docs_by_case = {k: v for k, v in docs_df.groupby("case_instance_uuid")}
    entries_by_case = {k: v for k, v in entries_df.groupby("case_instance_uuid")}

    for case_row in tqdm(cases_df.to_dict("records"), desc="Exporting FL cases", unit="case"):
        case_id = case_row["id"]
        case_uuid = case_row["case_instance_uuid"]

        case_dir = output_dir / f"case_{case_id}"
        json_path = case_dir / f"case_{case_id}.json"
        if skip_if_exists and json_path.exists():
            stats["skipped"] += 1
            continue

        doc_group = docs_by_case.get(case_uuid)
        documents = doc_group.to_dict("records") if doc_group is not None else []
        if not documents and require_documents:
            stats["no_documents"] += 1
            continue

        entry_group = entries_by_case.get(case_uuid)
        case_history = entry_group.to_dict("records") if entry_group is not None else []

        case_info = dict(case_row)
        case_info.update(full_case_by_uuid.get(case_uuid, {}))

        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            denormalized_case = {
                "case_info": case_info,
                "documents": documents,
                "case_history": case_history,
                # Same field names/shape as CaseExporter._create_denormalized_json's
                # "normalized" block (lawsuit_parser/utils/case_exporter.py) and
                # export_il_cases.py/export_tx_cases.py's own - a consistent
                # cross-state view alongside the untouched, state-specific
                # case_info above. FL has no single case_status string; a
                # case is "closed" once closed_flag is set, "open" otherwise.
                "normalized": {
                    "state": "fl",
                    "internal_id": case_id,
                    "case_number": case_info.get("case_number"),
                    "unique_key": case_uuid,
                    "caption": case_info.get("case_caption") or case_info.get("case_title"),
                    "court": case_info.get("court_id"),
                    "case_status": "closed" if case_info.get("closed_flag") else "open",
                    "case_type": case_info.get("case_classification"),
                    "filed_date": case_info.get("filed_date"),
                    "total_documents": len(documents),
                    "total_history_entries": len(case_history),
                },
                "summary": {
                    "total_documents": len(documents),
                    "case_number": case_info.get("case_number"),
                    "case_title": case_info.get("case_title"),
                    "case_classification": case_info.get("case_classification"),
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
            logger.warning(f"Failed to export FL case {case_id}: {e}")
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=Path("data/cases/fl_after_search"))
    parser.add_argument(
        "--no-require-documents",
        dest="require_documents",
        action="store_false",
        help="also export cases with zero documents (skipped by default, matching the NY export)",
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
    print("Florida Case Exporter")
    print("=" * 80)

    stats = export_fl_cases(
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
