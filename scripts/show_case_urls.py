#!/usr/bin/env python3
"""Show all URLs associated with a case."""

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils import load_db_config


def create_scrapping_engine():
    """Create SQLAlchemy engine for the scrapping database (port 5433)."""
    config = load_db_config()
    config["port"] = 5433

    url = URL.create(
        drivername="postgresql+psycopg",
        username=config["user"],
        password=config["password"],
        host=config["host"],
        port=int(config["port"]),
        database=config["database"],
    )
    return create_engine(url)


def main():
    parser = argparse.ArgumentParser(
        description="Show all URLs associated with a case."
    )
    parser.add_argument(
        "case_id",
        type=int,
        help="Case ID from cases_after_search.id column",
    )
    parser.add_argument(
        "--schema",
        type=str,
        default="courts_final",
        help="Postgres schema holding the crawl tables (default: courts_final)",
    )
    parser.add_argument(
        "--table-prefix",
        type=str,
        default="ny_",
        help="Per-state table prefix, e.g. 'ny_' or 'fl_' (default: ny_)",
    )

    args = parser.parse_args()

    engine = create_scrapping_engine()
    cases_table = f"{args.schema}.{args.table_prefix}cases_after_search"
    documents_table = f"{args.schema}.{args.table_prefix}docket_documents"

    try:
        # Get case URLs
        case_query = text(f"""
            SELECT
                id,
                case_id,
                docket_id,
                query_link,
                case_link
            FROM {cases_table}
            WHERE id = :case_id
        """)

        with engine.connect() as conn:
            result = conn.execute(case_query, {"case_id": args.case_id})
            case_row = result.fetchone()

        if not case_row:
            print(f"Case {args.case_id} not found")
            return

        case_data = dict(case_row._mapping)
        docket_id = case_data["docket_id"]

        print("=" * 80)
        print(f"CASE URLS - Case ID: {args.case_id}")
        print("=" * 80)
        print(f"\nCase: {case_data['case_id']}")
        print(f"Docket ID: {docket_id}")
        print()
        print("📋 CASE URLS:")
        print(f"  Query Link:  {case_data['query_link']}")
        print(f"  Case Link:   {case_data['case_link']}")

        # Get document URLs
        docs_query = text(f"""
            SELECT
                id,
                document_name,
                document_link,
                document_bucket_link,
                document_confirmation_link,
                document_confirmation_bucket_link
            FROM {documents_table}
            WHERE docket_id = :docket_id
            ORDER BY id
        """)

        with engine.connect() as conn:
            result = conn.execute(docs_query, {"docket_id": docket_id})
            docs = result.fetchall()

        print(f"\n📄 DOCUMENT URLS ({len(docs)} documents):")
        for doc in docs:
            doc_data = dict(doc._mapping)
            print(f"\n  Document {doc_data['id']}: {doc_data['document_name']}")
            print(f"    View URL:        {doc_data['document_link']}")
            print(f"    GCS Path:        {doc_data['document_bucket_link']}")
            print(f"    Confirmation:    {doc_data['document_confirmation_link']}")
            print(f"    Confirm GCS:     {doc_data['document_confirmation_bucket_link']}")

        print("\n" + "=" * 80)

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
