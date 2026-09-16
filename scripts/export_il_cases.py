#!/usr/bin/env python3
"""Export Illinois court case metadata to denormalized JSON files.

Illinois uses a different scraper/schema than NY (no search-term "after_search"
split, no docket_id - cases join to documents via case_key, and documents join
to their files via document_id), so it can't go through
scripts/export_cases.py / CaseExporter. This mirrors that script's shape
(bulk-fetch the tables once, group in memory, write one denormalized
case_<id>.json per case) but targets courts_final.il_cases / il_documents /
il_document_files instead.

Metadata-only by design (no GCS downloads) - il_document_files.storage_path /
file_url point at the underlying files if a download pass is added later.

Usage:
    python scripts/export_il_cases.py --output-dir data/cases/il_after_search
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
    filename="il_export.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SCRAPPING_DB_PORT = 5433
SCHEMA = "courts_final"


def export_il_cases(
    output_dir: Path,
    require_documents: bool = True,
    skip_if_exists: bool = True,
    force_refresh: bool = False,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching il_cases rows...")
    cases_df = fetch_from_postgres(
        f"""
        SELECT id, case_key, case_number, case_url, title, nature, filed_date,
               case_type, authority_types, case_status, entities,
               first_seen_year, created_at, updated_at
        FROM {SCHEMA}.il_cases
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )

    stats = {"total": len(cases_df), "successful": 0, "skipped": 0, "failed": 0, "no_documents": 0}
    if cases_df.empty:
        return stats

    case_keys = cases_df["case_key"].dropna().unique().tolist()

    print(f"Fetched {len(cases_df)} cases. Fetching documents for {len(case_keys)} case_keys...")
    docs_df = fetch_from_postgres(
        f"""
        SELECT id, document_id, case_key, case_number, document_type,
               description, filed_by, party, posted_date, document_url,
               search_year, filed_for, means_received, date_filed,
               file_count, storage_paths, created_at, updated_at
        FROM {SCHEMA}.il_documents
        ORDER BY id
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )
    docs_df = docs_df[docs_df["case_key"].isin(case_keys)]

    document_ids = docs_df["document_id"].dropna().unique().tolist()
    print(f"Fetched {len(docs_df)} documents. Fetching files for {len(document_ids)} documents...")
    files_df = fetch_from_postgres(
        f"""
        SELECT id, file_id, document_id, case_key, title, file_url,
               file_extension, file_size, downloaded_size, storage_path,
               download_status, downloaded_at, created_at, updated_at
        FROM {SCHEMA}.il_document_files
        ORDER BY id
        """,
        port=SCRAPPING_DB_PORT,
        force_refresh=force_refresh,
    )
    files_df = files_df[files_df["document_id"].isin(document_ids)]
    print(f"Fetched {len(files_df)} files. Building per-case JSON...")

    docs_by_case = {k: v for k, v in docs_df.groupby("case_key")}
    files_by_doc = {k: v for k, v in files_df.groupby("document_id")}

    for case_row in tqdm(cases_df.to_dict("records"), desc="Exporting IL cases", unit="case"):
        case_id = case_row["id"]
        case_key = case_row["case_key"]

        case_dir = output_dir / f"case_{case_id}"
        json_path = case_dir / f"case_{case_id}.json"
        if skip_if_exists and json_path.exists():
            stats["skipped"] += 1
            continue

        doc_group = docs_by_case.get(case_key)
        documents = doc_group.to_dict("records") if doc_group is not None else []
        if not documents and require_documents:
            stats["no_documents"] += 1
            continue

        for doc in documents:
            file_group = files_by_doc.get(doc["document_id"])
            doc["files"] = file_group.to_dict("records") if file_group is not None else []

        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            denormalized_case = {
                "case_info": case_row,
                "documents": documents,
                "case_history": [],
                "summary": {
                    "total_documents": len(documents),
                    "case_key": case_key,
                    "title": case_row.get("title"),
                    "case_status": case_row.get("case_status"),
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
            logger.warning(f"Failed to export IL case {case_id}: {e}")
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=Path("data/cases/il_after_search"))
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
    print("Illinois Case Exporter")
    print("=" * 80)

    stats = export_il_cases(
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
