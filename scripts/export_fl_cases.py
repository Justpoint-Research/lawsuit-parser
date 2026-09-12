#!/usr/bin/env python3
"""Export Florida court case metadata to denormalized JSON files.

Florida's after_search table (courts_final.fl_cases_after_search - cases
matched to the mass-tort defendant search terms, same concept as NY's
ny_cases_after_search) has a different column set than NY and joins to its
documents table on an integer case_id rather than a docket_id, so it can't go
through scripts/export_cases.py / CaseExporter. This mirrors that script's
shape (bulk-fetch the tables once, group in memory, write one denormalized
case_<id>.json per case) but targets fl_cases_after_search / fl_docket_documents
instead.

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


def export_fl_cases(output_dir: Path, require_documents: bool = True, skip_if_exists: bool = True) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching fl_cases_after_search rows (cached)...")
    cases_df = fetch_from_postgres(
        f"""
        SELECT id, case_instance_uuid, case_number, case_title, closed_flag,
               case_classification, case_classification_id, court_id,
               filed_date, originating_court_cases, query, query_kind,
               created_at, updated_at
        FROM {SCHEMA}.fl_cases_after_search
        """,
        port=SCRAPPING_DB_PORT,
    )

    stats = {"total": len(cases_df), "successful": 0, "skipped": 0, "failed": 0, "no_documents": 0}
    if cases_df.empty:
        return stats

    case_ids = cases_df["id"].tolist()

    print(f"Fetched {len(cases_df)} cases. Fetching documents for {len(case_ids)} cases (cached)...")
    docs_df = fetch_from_postgres(
        f"""
        SELECT id, case_id, case_instance_uuid, docket_entry_uuid,
               document_link_uuid, document_name, document_type, content_type,
               file_extension, page_count, file_size, user_document_state,
               document_url, download_status, downloaded_at, downloaded_size,
               storage_path, created_at, updated_at
        FROM {SCHEMA}.fl_docket_documents
        ORDER BY id
        """,
        port=SCRAPPING_DB_PORT,
    )
    docs_df = docs_df[docs_df["case_id"].isin(case_ids)]
    print(f"Fetched {len(docs_df)} documents. Building per-case JSON...")

    docs_by_case = {k: v for k, v in docs_df.groupby("case_id")}

    for case_row in tqdm(cases_df.to_dict("records"), desc="Exporting FL cases", unit="case"):
        case_id = case_row["id"]

        case_dir = output_dir / f"case_{case_id}"
        json_path = case_dir / f"case_{case_id}.json"
        if skip_if_exists and json_path.exists():
            stats["skipped"] += 1
            continue

        doc_group = docs_by_case.get(case_id)
        documents = doc_group.to_dict("records") if doc_group is not None else []
        if not documents and require_documents:
            stats["no_documents"] += 1
            continue

        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            denormalized_case = {
                "case_info": case_row,
                "documents": documents,
                "case_history": [],
                "summary": {
                    "total_documents": len(documents),
                    "case_number": case_row.get("case_number"),
                    "case_title": case_row.get("case_title"),
                    "case_classification": case_row.get("case_classification"),
                    "files_downloaded": False,
                    "text_extraction_enabled": False,
                    "exported_at": datetime.now().isoformat(),
                },
            }
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
    args = parser.parse_args()

    print("=" * 80)
    print("Florida Case Exporter")
    print("=" * 80)

    stats = export_fl_cases(args.output_dir, require_documents=args.require_documents)

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
