#!/usr/bin/env python3
"""Export MN / OK / CA court case metadata to denormalized JSON files.

Same idea as export_fl_cases.py / export_tx_cases.py (bulk-fetch the tables
once, group in memory, write one case_<id>/case_<id>.json per case, same
case_info / documents / case_history / normalized / summary shape), but for
the three states whose crawl tables arrived after those scripts were written.
Each has its own schema, so this keeps one small config per state behind a
shared writer instead of three copies of it:

  mn  base:     mn_cases_after_search (keyed by case_number)
      detail:   mn_cases (judge, hearings/dispositions/financials jsonb,
                counts) -> merged into case_info
      parties:  mn_parties (role, name, address, attorneys jsonb) -> "parties"
      history:  mn_case_events -> case_history
      docs:     mn_documents

  ok  base:     ok_cases_after_search (keyed by court_db + case_number)
      detail:   ok_cases (case_type, closed_date, judge, disposition, counts)
                -> merged into case_info. Only a minority of after_search
                cases have been detail-fetched; the rest export case_info
                from the after_search row alone.
      history:  ok_docket_entries -> case_history
      docs:     ok_documents

  ca  base:     ca_cases (keyed by district + case_number). There is no
                separate after_search table: ca_cases itself carries the
                matching query (query / query_kind / term_id).
      docs:     ca_opinions (the downloaded opinion files)
      history:  always [] - no docket-entry table exists for ca.

Child tables are joined through their integer case_id -> parent.id; for all
three states that was checked against the live DB to agree with the business
key (0 mismatches, 0 orphans), unlike FL where bare integer ids are
independent sequences.

Cases with no documents are skipped by default, matching the other state
exports (--no-require-documents to keep them). That is a large cut for some
states: OK has documents for ~16% of its cases and CA for ~3%.

Metadata-only (no GCS downloads); documents' storage_path / file_url point at
the underlying files if a download pass is added later. Scrape-queue
bookkeeping (attempt counters, claim timestamps) is intentionally left out.

Usage:
    python scripts/export_state_cases.py --state mn --output-dir data/cases/mn_after_search
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils.db import fetch_from_postgres

logging.basicConfig(
    filename="state_export.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SCRAPPING_DB_PORT = 5433
SCHEMA = "courts_final"


@dataclass
class StateConfig:
    # SELECT for the base (after_search-style) table; must include id.
    cases_sql: str
    # Business-key columns on the base table, used for unique_key and to link
    # to the detail table.
    key_cols: list[str]
    normalize: Callable[[dict[str, Any]], dict[str, Any]]
    # Optional detail table: (sql, columns to merge into case_info). Linked to
    # the base row on key_cols.
    detail_sql: str | None = None
    detail_fields: list[str] = field(default_factory=list)
    # Child tables, each (sql, parent) with parent in {"detail", "base"}: the
    # integer case_id column in the SQL result refers to detail.id (or
    # base.id when there's no detail table).
    documents_sql: str | None = None
    history_sql: str | None = None
    parties_sql: str | None = None
    docs_parent: str = "detail"


def _normalized_mn(ci: dict[str, Any]) -> dict[str, Any]:
    return {
        "unique_key": ci.get("case_number"),
        "caption": ci.get("case_title"),
        "court": ci.get("case_location") or ci.get("county"),
        "case_status": ci.get("case_status"),
        "case_type": ci.get("case_type"),
        "filed_date": ci.get("filed_date"),
    }


def _normalized_ok(ci: dict[str, Any]) -> dict[str, Any]:
    # closed_date only exists once the detail row was fetched; with no detail
    # the status is unknown rather than "open".
    if ci.get("fetched_at") is None:
        status = None
    else:
        status = "closed" if ci.get("closed_date") else "open"
    return {
        "unique_key": f"{ci.get('court_db')}:{ci.get('case_number')}",
        "caption": ci.get("style"),
        "court": ci.get("court_name"),
        "case_status": status,
        "case_type": ci.get("case_type"),
        "filed_date": ci.get("filed_date"),
    }


def _normalized_ca(ci: dict[str, Any]) -> dict[str, Any]:
    return {
        "unique_key": f"{ci.get('district')}:{ci.get('case_number')}",
        "caption": ci.get("caption"),
        "court": ci.get("court"),
        "case_status": ci.get("case_status"),
        "case_type": ci.get("case_type"),
        "filed_date": ci.get("filed_date"),
    }


STATES: dict[str, StateConfig] = {
    "mn": StateConfig(
        cases_sql=f"""
            SELECT id, case_number, filed_date, case_title, case_type,
                   case_location, case_status, county, query, query_kind,
                   details_status, details_fetched_at, details_error,
                   created_at, updated_at
            FROM {SCHEMA}.mn_cases_after_search
        """,
        key_cols=["case_number"],
        normalize=_normalized_mn,
        detail_sql=f"""
            SELECT id AS detail_id, case_number, judge, hearings, dispositions,
                   financials, parties_count, events_count, documents_count,
                   fetched_at, documents_status, documents_error
            FROM {SCHEMA}.mn_cases
        """,
        detail_fields=[
            "judge", "hearings", "dispositions", "financials", "parties_count",
            "events_count", "documents_count", "fetched_at",
            "documents_status", "documents_error",
        ],
        parties_sql=f"""
            SELECT id, case_id, case_number, seq, role, name, address,
                   attorneys, created_at, updated_at
            FROM {SCHEMA}.mn_parties
            ORDER BY case_id, seq
        """,
        history_sql=f"""
            SELECT id, case_id, case_number, seq, event_date, description,
                   index_no, notes, documents_count, created_at, updated_at
            FROM {SCHEMA}.mn_case_events
            ORDER BY case_id, seq
        """,
        documents_sql=f"""
            SELECT id, case_id, event_id, case_number, event_seq, doc_seq,
                   index_no, description, page_count, download_status,
                   downloaded_at, storage_path, downloaded_size, content_type,
                   download_error, sha256, created_at, updated_at
            FROM {SCHEMA}.mn_documents
            ORDER BY case_id, event_seq, doc_seq
        """,
    ),
    "ok": StateConfig(
        cases_sql=f"""
            SELECT id, court_db, court_name, case_number, filed_date, style,
                   found_party, cmid, case_url, query, query_kind,
                   details_status, details_fetched_at, details_error,
                   created_at, updated_at
            FROM {SCHEMA}.ok_cases_after_search
        """,
        key_cols=["court_db", "case_number"],
        normalize=_normalized_ok,
        detail_sql=f"""
            SELECT id AS detail_id, court_db, case_number, case_type,
                   closed_date, judge, disposition, docket_entries_count,
                   documents_count, parties_count, fetched_at
            FROM {SCHEMA}.ok_cases
        """,
        detail_fields=[
            "case_type", "closed_date", "judge", "disposition",
            "docket_entries_count", "documents_count", "parties_count",
            "fetched_at",
        ],
        history_sql=f"""
            SELECT id, case_id, court_db, case_number, seq, entry_date, code,
                   description, count_no, party, amount, created_at, updated_at
            FROM {SCHEMA}.ok_docket_entries
            ORDER BY case_id, seq
        """,
        documents_sql=f"""
            SELECT id, case_id, docket_entry_id, court_db, case_number, bc,
                   fmt, file_url, description, download_status, storage_path,
                   downloaded_size, content_type, downloaded_at,
                   download_error, created_at, updated_at
            FROM {SCHEMA}.ok_documents
            ORDER BY case_id, id
        """,
    ),
    "ca": StateConfig(
        cases_sql=f"""
            SELECT id, district, court, case_number, case_url, doc_id, doc_no,
                   caption, coa_case_number, supreme_case_number,
                   trial_case_number, case_category, case_type, division,
                   coa_district, filed_date, completion_date, case_status,
                   issues, citation, oral_argument_at, opinion_pdf_url,
                   opinion_doc_url, query, query_kind, term_id,
                   summary_status, summary_fetched_at, summary_error,
                   created_at, updated_at
            FROM {SCHEMA}.ca_cases
        """,
        key_cols=["district", "case_number"],
        normalize=_normalized_ca,
        documents_sql=f"""
            SELECT id, case_id, district, case_number, kind, file_url,
                   file_extension, download_status, storage_path,
                   downloaded_size, content_type, downloaded_at,
                   download_error, created_at, updated_at
            FROM {SCHEMA}.ca_opinions
            ORDER BY case_id, id
        """,
        docs_parent="base",
    ),
}


def _clean(value: Any) -> Any:
    """NaN/NaT -> None so the JSON is valid (json.dump would emit bare NaN)."""
    if value is pd.NaT or (isinstance(value, float) and value != value):
        return None
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def _group_by_case_id(sql: str | None, force_refresh: bool) -> dict[int, list[dict[str, Any]]]:
    if not sql:
        return {}
    df = fetch_from_postgres(sql, port=SCRAPPING_DB_PORT, force_refresh=force_refresh)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in _records(df):
        grouped[int(row["case_id"])].append(row)
    return grouped


def export_state_cases(
    state: str,
    output_dir: Path,
    require_documents: bool = True,
    skip_if_exists: bool = True,
    force_refresh: bool = False,
) -> dict[str, int]:
    cfg = STATES[state]
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {state} case rows...")
    cases_df = fetch_from_postgres(cfg.cases_sql, port=SCRAPPING_DB_PORT, force_refresh=force_refresh)
    stats = {"total": len(cases_df), "successful": 0, "skipped": 0, "failed": 0, "no_documents": 0}
    if cases_df.empty:
        return stats

    detail_by_key: dict[tuple, dict[str, Any]] = {}
    if cfg.detail_sql:
        print("Fetching detail rows...")
        detail_df = fetch_from_postgres(cfg.detail_sql, port=SCRAPPING_DB_PORT, force_refresh=force_refresh)
        for row in _records(detail_df):
            detail_by_key[tuple(row[k] for k in cfg.key_cols)] = row

    print("Fetching child tables...")
    docs_by_case = _group_by_case_id(cfg.documents_sql, force_refresh)
    history_by_case = _group_by_case_id(cfg.history_sql, force_refresh)
    parties_by_case = _group_by_case_id(cfg.parties_sql, force_refresh)
    print(f"Fetched {stats['total']} cases. Building per-case JSON...")

    for case_row in tqdm(_records(cases_df), desc=f"Exporting {state.upper()} cases", unit="case"):
        case_id = case_row["id"]

        case_dir = output_dir / f"case_{case_id}"
        json_path = case_dir / f"case_{case_id}.json"
        if skip_if_exists and json_path.exists():
            stats["skipped"] += 1
            continue

        case_info = dict(case_row)
        detail = detail_by_key.get(tuple(case_row[k] for k in cfg.key_cols))
        if detail:
            case_info.update({f: detail[f] for f in cfg.detail_fields})

        # Child tables point at the detail row's id when there is a detail
        # table, otherwise at the base row's id.
        child_id = detail["detail_id"] if (cfg.detail_sql and detail) else (None if cfg.detail_sql else case_id)
        documents = docs_by_case.get(child_id, []) if child_id is not None else []
        case_history = history_by_case.get(child_id, []) if child_id is not None else []
        parties = parties_by_case.get(child_id, []) if child_id is not None else []

        if not documents and require_documents:
            stats["no_documents"] += 1
            continue

        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            norm = cfg.normalize(case_info)
            denormalized_case = {
                "case_info": case_info,
                "documents": documents,
                "case_history": case_history,
                # Same field names/shape as the other exporters' "normalized"
                # block - a consistent cross-state view alongside the
                # untouched, state-specific case_info above.
                "normalized": {
                    "state": state,
                    "internal_id": case_id,
                    "case_number": case_info.get("case_number"),
                    **norm,
                    "total_documents": len(documents),
                    "total_history_entries": len(case_history),
                },
                "summary": {
                    "total_documents": len(documents),
                    "case_number": case_info.get("case_number"),
                    "caption": norm["caption"],
                    "case_type": norm["case_type"],
                    "files_downloaded": False,
                    "text_extraction_enabled": False,
                    "exported_at": datetime.now().isoformat(),
                },
            }
            if cfg.parties_sql:
                denormalized_case["parties"] = parties
            # DVC checks out cached files read-only (0444); truncate-writing
            # into one raises PermissionError even for the owner.
            if json_path.exists():
                json_path.unlink()
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(denormalized_case, f, indent=2, default=str)
            stats["successful"] += 1
        except Exception as e:
            logger.warning(f"Failed to export {state.upper()} case {case_id}: {e}")
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", required=True, choices=sorted(STATES))
    parser.add_argument("--output-dir", type=Path, help="default: data/cases/<state>_after_search")
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
    output_dir = args.output_dir or Path(f"data/cases/{args.state}_after_search")

    print("=" * 80)
    print(f"{args.state.upper()} Case Exporter")
    print("=" * 80)

    stats = export_state_cases(
        args.state,
        output_dir,
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
    print(f"  Output directory: {output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    main()
