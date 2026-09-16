#!/usr/bin/env python3
"""Validate document counts for exported cases against GCS.

For every case_<id>.json under an export directory (default:
data/cases/ny_after_search), tracks each case through a 4-stage funnel:

0. found_by_crawl - ny_cases_after_search.documents_listed_count: the number
                 of documents the crawler actually saw listed on the court
                 website when it visited the case's docket page. This is the
                 real "how many documents exist for this lawsuit" number -
                 NOT the same as the docket_documents row count below, even
                 though the two agree for ~99.9% of cases (30,912/30,937 in
                 courts_final.ny_cases_after_search as of 2026-09-16). Where
                 they disagree, the scraper genuinely failed to capture (or
                 over-captured) documents the crawl itself found.
1. scraped     - the docket_documents row count (i.e. how many of the
                 found_by_crawl documents actually made it into the DB).
2. has_link    - document_bucket_link (or ...confirmation_bucket_link) is a
                 non-null/non-empty value on that row.
3. on_gcs      - that link resolves to a blob that actually exists in the
                 bucket. Resolution mirrors the exporter's own path handling
                 (CaseExporter._with_state_prefix in
                 lawsuit_parser/utils/case_exporter.py): ~22% of rows already
                 have the state code baked into the stored path (e.g.
                 "ny/document_link/..."), and naively prepending "ny/" on
                 those produces a doubled "ny/ny/..." blob name that 404s.
                 Getting this resolution wrong makes every one of those rows
                 look like a false mismatch.

A case is flagged as a mismatch whenever found_by_crawl > scraped, or
on_gcs < scraped for either documents or confirmations.

Requires the Cloud SQL proxy running on the scrapping DB (make run-proxy,
port 5433) to pull documents_listed_count - pass --skip-db-check to fall
back to using the scraped row count as stage 0 instead (no DB needed).

Usage:
    uv run python scripts/validate_document_mismatches.py
    uv run python scripts/validate_document_mismatches.py --limit 500
    uv run python scripts/validate_document_mismatches.py --skip-gcs-check
    uv run python scripts/validate_document_mismatches.py --skip-db-check
    uv run python scripts/validate_document_mismatches.py --output report.csv
"""

import argparse
import csv
import html
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils.gcs import extract_blob_name, get_storage_client
from lawsuit_parser.utils.db import load_db_config


def is_missing(value) -> bool:
    """True for None, NaN, or empty/whitespace-only strings."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def with_state_prefix(blob_name: str, state_code: str) -> str:
    """Mirror CaseExporter._with_state_prefix: only add the prefix if absent."""
    if not state_code or blob_name.startswith(f"{state_code}/"):
        return blob_name
    return f"{state_code}/{blob_name}"


def resolve_blob_name(raw_path, state_code: str) -> str | None:
    if is_missing(raw_path):
        return None
    blob_name = extract_blob_name(raw_path)
    if not blob_name:
        return None
    return with_state_prefix(blob_name, state_code)


def list_gcs_blob_names(
    bucket_name: str, prefix: str, cache_file: Path | None = None
) -> set[str]:
    if cache_file is not None and cache_file.exists():
        print(f"Loading cached blob list from {cache_file} ...", file=sys.stderr)
        with open(cache_file) as f:
            names = {line.rstrip("\n") for line in f if line.strip()}
        print(f"Loaded {len(names):,} cached blob names", file=sys.stderr)
        return names

    print(f"Listing gs://{bucket_name}/{prefix} ...", file=sys.stderr)
    client = get_storage_client()
    names = set()
    count = 0
    for blob in client.list_blobs(
        bucket_name, prefix=prefix, fields="items(name),nextPageToken"
    ):
        names.add(blob.name)
        count += 1
        if count % 100000 == 0:
            print(f"  ... {count:,} blobs listed so far", file=sys.stderr)
    print(f"Done listing: {count:,} blobs under {prefix!r}", file=sys.stderr)

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            f.writelines(f"{name}\n" for name in names)
        print(f"Cached blob list to {cache_file}", file=sys.stderr)

    return names


def fetch_documents_listed_counts(table_prefix: str) -> dict[str, int]:
    """docket_id -> documents_listed_count, straight from cases_after_search.

    This is the crawler's own tally of how many documents it saw listed on
    the case's docket page - the authoritative "found" number, independent
    of whether the scraper actually captured all of them into
    docket_documents.
    """
    from sqlalchemy import create_engine, text

    print("Fetching documents_listed_count from the DB ...", file=sys.stderr)
    config = load_db_config()
    config["port"] = 5433  # scrapping DB
    engine = create_engine(
        f"postgresql+psycopg://{config['user']}:{config['password']}"
        f"@{config['host']}:{config['port']}/{config['database']}"
    )
    query = text(
        f"SELECT docket_id, documents_listed_count "
        f"FROM courts_final.{table_prefix}cases_after_search"
    )
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
    result = {row[0]: row[1] for row in rows}
    print(f"Fetched documents_listed_count for {len(result):,} cases", file=sys.stderr)
    return result


def docket_documents_sql(table_prefix: str, docket_id: str) -> str:
    """A ready-to-paste query to pull this case's live rows from the DB.

    Lets you check the current DB state directly, bypassing the (possibly
    stale) exported JSON - e.g. to see if a document row's bucket_link has
    since been backfilled, or the docket has new rows since the export ran.
    """
    escaped_docket_id = (docket_id or "").replace("'", "''")
    return (
        f"SELECT id, document_name, document_status, filed_create, "
        f"document_link, document_bucket_link, "
        f"document_confirmation_link, document_confirmation_bucket_link "
        f"FROM courts_final.{table_prefix}docket_documents "
        f"WHERE docket_id = '{escaped_docket_id}' ORDER BY id;"
    )


def find_case_files(cases_dir: Path):
    for case_dir in sorted(
        cases_dir.glob("case_*"), key=lambda p: int(p.name.removeprefix("case_"))
    ):
        json_path = case_dir / f"{case_dir.name}.json"
        if json_path.exists():
            yield case_dir.name, json_path


def main():
    parser = argparse.ArgumentParser(
        description="Find cases whose exported document counts don't match GCS."
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=Path("data/cases/ny_after_search"),
        help="Directory of case_<id>/case_<id>.json exports (default: data/cases/ny_after_search)",
    )
    parser.add_argument(
        "--state-code",
        default="ny",
        help="State prefix used in GCS paths, e.g. 'ny' (default: ny)",
    )
    parser.add_argument(
        "--bucket",
        default="courts_crawl",
        help="GCS bucket name (default: courts_crawl)",
    )
    parser.add_argument(
        "--table-prefix",
        default="ny_",
        help="Per-state table prefix for the DB query (default: ny_)",
    )
    parser.add_argument(
        "--skip-gcs-check",
        action="store_true",
        help="Only run the cheap local check (missing bucket_link fields); skip listing GCS.",
    )
    parser.add_argument(
        "--skip-db-check",
        action="store_true",
        help="Don't query documents_listed_count from the DB; use the scraped "
        "docket_documents row count as stage 0 instead (no Cloud SQL proxy needed).",
    )
    parser.add_argument(
        "--blob-cache-file",
        type=Path,
        default=Path("data/cases/ny_after_search_gcs_blobs.txt"),
        help="Local cache of the bucket's blob names, one per line - listing "
        "the bucket can take minutes, so reuse this across runs. Delete it "
        "to force a fresh listing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N cases (for a quick test run).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the per-case mismatch report as CSV to this path.",
    )
    parser.add_argument(
        "--examples-output",
        type=Path,
        default=None,
        help="Write a document-level CSV of rows where a website link "
        "(document_link) exists - i.e. the document was found/expected to "
        "scrape - but document_bucket_link (the GCS link) is missing. "
        "Includes the document_link URL so you can open it and check by hand.",
    )
    parser.add_argument(
        "--examples-count",
        type=int,
        default=200,
        help="Max rows to write to --examples-output; randomly sampled across "
        "all matching rows for case/court diversity (default: 200; use 0 for all).",
    )
    parser.add_argument(
        "--examples-seed",
        type=int,
        default=42,
        help="Random seed for --examples-output sampling (default: 42, for reproducibility).",
    )
    args = parser.parse_args()

    if not args.cases_dir.exists():
        parser.error(f"cases dir not found: {args.cases_dir}")

    gcs_blob_names = None
    if not args.skip_gcs_check:
        gcs_blob_names = list_gcs_blob_names(
            args.bucket, f"{args.state_code}/", cache_file=args.blob_cache_file
        )

    listed_counts = None
    if not args.skip_db_check:
        listed_counts = fetch_documents_listed_counts(args.table_prefix)

    rows = []
    example_rows = []
    total_cases = 0
    total_found_by_crawl = 0
    total_scraped = 0
    total_doc_link = 0
    total_doc_gcs = 0
    total_conf_link = 0
    total_conf_gcs = 0
    cases_with_mismatch = 0
    cases_crawl_scrape_mismatch = 0

    case_iter = find_case_files(args.cases_dir)
    for i, (case_name, json_path) in enumerate(case_iter):
        if args.limit is not None and i >= args.limit:
            break
        total_cases += 1

        with open(json_path) as f:
            data = json.load(f)
        documents = data.get("documents", [])
        scraped = len(documents)
        docket_id = data.get("case_info", {}).get("docket_id")

        if listed_counts is not None:
            found_by_crawl = listed_counts.get(docket_id)
            if found_by_crawl is None:
                found_by_crawl = scraped  # docket_id not found in DB (stale export)
        else:
            found_by_crawl = scraped

        total_found_by_crawl += found_by_crawl
        total_scraped += scraped
        if found_by_crawl != scraped:
            cases_crawl_scrape_mismatch += 1

        doc_link = doc_gcs = conf_link = conf_gcs = 0
        not_on_gcs_examples = []
        case_example_candidates = []

        for doc in documents:
            doc_blob = resolve_blob_name(doc.get("document_bucket_link"), args.state_code)
            conf_blob = resolve_blob_name(
                doc.get("document_confirmation_bucket_link"), args.state_code
            )

            if doc_blob is not None:
                doc_link += 1
                if gcs_blob_names is None or doc_blob in gcs_blob_names:
                    doc_gcs += 1
                else:
                    not_on_gcs_examples.append(doc_blob)
            elif args.examples_output and not is_missing(doc.get("document_link")):
                # Has a website link (crawler found/expected this document),
                # but no GCS bucket link was ever recorded for it. doc_gcs
                # isn't final yet (still accumulating below), so stash the
                # doc and fill in case-level counts once the loop is done.
                case_example_candidates.append(doc)

            if conf_blob is not None:
                conf_link += 1
                if gcs_blob_names is None or conf_blob in gcs_blob_names:
                    conf_gcs += 1
                else:
                    not_on_gcs_examples.append(conf_blob)

        total_doc_link += doc_link
        total_doc_gcs += doc_gcs
        total_conf_link += conf_link
        total_conf_gcs += conf_gcs

        for doc in case_example_candidates:
            example_rows.append(
                {
                    "case": case_name,
                    "case_id": data.get("case_info", {}).get("case_id", ""),
                    "docket_id": docket_id,
                    "case_docket_page_link": html.unescape(
                        data.get("case_info", {}).get("case_link") or ""
                    ),
                    "case_found_by_crawl": found_by_crawl,
                    "case_scraped_rows": scraped,
                    "case_docs_on_gcs": doc_gcs,
                    "document_id": doc.get("id", ""),
                    "document_name": doc.get("document_name", ""),
                    "document_status": doc.get("document_status", ""),
                    "filed_create": doc.get("filed_create", ""),
                    "filed_received": doc.get("filed_received", ""),
                    "document_link": doc.get("document_link", ""),
                    "document_confirmation_link": doc.get(
                        "document_confirmation_link", ""
                    ),
                    "sql_query": docket_documents_sql(args.table_prefix, docket_id),
                }
            )

        has_mismatch = (
            found_by_crawl != scraped or doc_gcs < scraped or conf_gcs < scraped
        )
        if has_mismatch:
            cases_with_mismatch += 1
            rows.append(
                {
                    "case": case_name,
                    "case_id": data.get("case_info", {}).get("case_id", ""),
                    "found_by_crawl": found_by_crawl,
                    "scraped_rows": scraped,
                    "docs_with_link": doc_link,
                    "docs_on_gcs": doc_gcs,
                    "confirmations_with_link": conf_link,
                    "confirmations_on_gcs": conf_gcs,
                    "not_on_gcs_examples": "; ".join(not_on_gcs_examples[:3]),
                }
            )

        if total_cases % 2000 == 0:
            print(f"  processed {total_cases:,} cases...", file=sys.stderr)

    def pct(n, d):
        return f"{n / d:.1%}" if d else "n/a"

    print()
    print("=" * 80)
    print(f"Cases scanned:                        {total_cases:,}")
    print()
    print(f"0. Found by crawl (documents_listed_count): {total_found_by_crawl:,}")
    print(
        f"1. Scraped into docket_documents:            {total_scraped:,}"
        f"  ({pct(total_scraped, total_found_by_crawl)})"
    )
    print(
        f"   Cases where crawl count != scraped rows:  {cases_crawl_scrape_mismatch:,}"
    )
    print()
    print("MAIN DOCUMENTS (out of scraped rows)")
    print(f"  2. Have a GCS link recorded:     {total_doc_link:,}  ({pct(total_doc_link, total_scraped)})")
    print(f"  3. Actually present on GCS:      {total_doc_gcs:,}  ({pct(total_doc_gcs, total_scraped)})")
    print()
    print("CONFIRMATIONS (out of scraped rows)")
    print(f"  2. Have a GCS link recorded:     {total_conf_link:,}  ({pct(total_conf_link, total_scraped)})")
    print(f"  3. Actually present on GCS:      {total_conf_gcs:,}  ({pct(total_conf_gcs, total_scraped)})")
    print()
    print(f"Cases with a mismatch:             {cases_with_mismatch:,}")
    if not args.skip_gcs_check:
        print(f"(GCS check enabled against gs://{args.bucket}/{args.state_code}/)")
    else:
        print("(GCS check skipped - stage 3 numbers above just mirror stage 2)")
    if args.skip_db_check:
        print("(DB check skipped - stage 0 above just mirrors stage 1)")
    if args.examples_output:
        print(f"Doc rows with a website link but no GCS link: {len(example_rows):,}")
    print("=" * 80)

    if args.output:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "case",
                    "case_id",
                    "found_by_crawl",
                    "scraped_rows",
                    "docs_with_link",
                    "docs_on_gcs",
                    "confirmations_with_link",
                    "confirmations_on_gcs",
                    "not_on_gcs_examples",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"Report written to {args.output} ({len(rows):,} mismatched cases)")
    elif rows:
        print("\nFirst 20 mismatched cases:")
        for row in rows[:20]:
            print(
                f"  {row['case']:>16}  crawl={row['found_by_crawl']:<4} "
                f"scraped={row['scraped_rows']:<4} "
                f"with_link={row['docs_with_link']:<4} "
                f"on_gcs={row['docs_on_gcs']:<4}"
            )

    if args.examples_output:
        sample = example_rows
        if args.examples_count and len(example_rows) > args.examples_count:
            random.seed(args.examples_seed)
            sample = random.sample(example_rows, args.examples_count)
            sample.sort(key=lambda r: r["case"])
        with open(args.examples_output, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "case",
                    "case_id",
                    "docket_id",
                    "case_docket_page_link",
                    "case_found_by_crawl",
                    "case_scraped_rows",
                    "case_docs_on_gcs",
                    "document_id",
                    "document_name",
                    "document_status",
                    "filed_create",
                    "filed_received",
                    "document_link",
                    "document_confirmation_link",
                    "sql_query",
                ],
            )
            writer.writeheader()
            writer.writerows(sample)
        print(
            f"Examples written to {args.examples_output} "
            f"({len(sample):,} of {len(example_rows):,} matching rows)"
        )


if __name__ == "__main__":
    main()
