#!/usr/bin/env python3
"""Check for additional metadata sources for a case."""

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
        description="Check for additional metadata sources for a case."
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
    log_events_table = f"{args.schema}.{args.table_prefix}log_events"

    try:
        # Get case info
        case_query = text(f"""
            SELECT court_id, case_id, docket_id
            FROM {cases_table}
            WHERE id = :case_id
        """)

        with engine.connect() as conn:
            result = conn.execute(case_query, {"case_id": args.case_id})
            case_row = result.fetchone()

        court_id = case_row[0]
        case_id_str = case_row[1]
        docket_id = case_row[2]

        print("=" * 80)
        print("ADDITIONAL METADATA SOURCES")
        print("=" * 80)

        # Check log events for this court
        print(f"\n📅 Court Log Events (court_id={court_id}):")
        log_query = text(f"""
            SELECT *
            FROM {log_events_table}
            WHERE court_id = :court_id
            ORDER BY created_at DESC
            LIMIT 5
        """)

        with engine.connect() as conn:
            result = conn.execute(log_query, {"court_id": court_id})
            log_rows = result.fetchall()

        if log_rows:
            print(f"   Found {len(log_rows)} recent events for this court")
            # Show first event structure
            if log_rows:
                log_data = dict(log_rows[0]._mapping)
                print(f"\n   Log event columns:")
                for key, value in sorted(log_data.items()):
                    value_str = str(value)[:80] if value else "None"
                    print(f"      {key:20} = {value_str}")
        else:
            print("   No log events found for this court")

        # Check documents in detail
        print(f"\n📄 Document Details:")
        docs_query = text(f"""
            SELECT
                id,
                document_name,
                document_details,
                filed_by,
                assigned_judge,
                document_status,
                ocr_created,
                ocr_transcription_id
            FROM {documents_table}
            WHERE docket_id = :docket_id
            ORDER BY id
        """)

        with engine.connect() as conn:
            result = conn.execute(docs_query, {"docket_id": docket_id})
            docs = result.fetchall()

        for doc in docs:
            print(f"\n   Document {doc[0]}:")
            print(f"      Name: {doc[1]}")
            print(f"      Details: {doc[2]}")
            print(f"      Filed by: {doc[3]}")
            print(f"      Assigned Judge: {doc[4]}")
            print(f"      Status: {doc[5]}")
            print(f"      OCR Created: {doc[6]}")
            print(f"      OCR Transcription ID: {doc[7]}")

        # Check if there are any other schemas with relevant data
        print(f"\n🔍 Checking for other schemas:")
        schema_query = text("""
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY schema_name
        """)

        with engine.connect() as conn:
            result = conn.execute(schema_query)
            schemas = [row[0] for row in result]

        for schema in schemas:
            print(f"   - {schema}")

        # Check public schema tables
        print(f"\n📊 Checking public schema for court-related tables:")
        public_tables_query = text("""
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            AND (tablename LIKE '%court%' OR tablename LIKE '%case%')
            ORDER BY tablename
        """)

        with engine.connect() as conn:
            result = conn.execute(public_tables_query)
            public_tables = [row[0] for row in result]

        for table in public_tables:
            print(f"   - public.{table}")

            # Get row count
            count_query = text(f"SELECT COUNT(*) FROM public.{table}")
            with engine.connect() as conn:
                count = conn.execute(count_query).scalar()
            print(f"     ({count:,} rows)")

        print("\n" + "=" * 80)

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
