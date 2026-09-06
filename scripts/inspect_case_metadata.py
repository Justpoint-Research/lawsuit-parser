#!/usr/bin/env python3
"""Inspect metadata available for a specific case.

This script queries all available metadata for a case and compares it with
what is currently exported to identify potentially useful fields.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils import load_db_config


def create_scrapping_engine():
    """Create SQLAlchemy engine for the scrapping database (port 5433)."""
    config = load_db_config()
    config["port"] = 5433  # Scrapping database port

    url = URL.create(
        drivername="postgresql+psycopg",
        username=config["user"],
        password=config["password"],
        host=config["host"],
        port=int(config["port"]),
        database=config["database"],
    )
    return create_engine(url)


def inspect_case(case_id: int, schema: str = "courts_final", table_prefix: str = "ny_"):
    """Inspect all metadata available for a case."""
    engine = create_scrapping_engine()

    try:
        print("=" * 80)
        print(f"CASE METADATA INSPECTION - Case ID: {case_id}")
        print("=" * 80)

        # Get the case from cases_after_search
        cases_table = f"{schema}.{table_prefix}cases_after_search"
        case_query = text(f"SELECT * FROM {cases_table} WHERE id = :case_id")

        with engine.connect() as conn:
            result = conn.execute(case_query, {"case_id": case_id})
            case_row = result.fetchone()

        if not case_row:
            print(f"❌ Case with id={case_id} not found in {cases_table}")
            return

        case_data = dict(case_row._mapping)
        docket_id = case_data["docket_id"]

        print(f"\n📋 Case Info:")
        print(f"   Case ID: {case_data.get('case_id')}")
        print(f"   Docket ID: {docket_id}")
        print(f"   Caption: {case_data.get('caption')}")
        print(f"   Court: {case_data.get('court')}")
        print(f"   Status: {case_data.get('case_status')}")

        # Show all columns and their values
        print(f"\n📊 All Case Columns ({len(case_data)} fields):")
        for key, value in sorted(case_data.items()):
            value_str = str(value)[:100] if value else "None"
            print(f"   {key:30} = {value_str}")

        # Check documents table
        docs_table = f"{schema}.{table_prefix}docket_documents"
        docs_query = text(f"SELECT * FROM {docs_table} WHERE docket_id = :docket_id LIMIT 1")

        with engine.connect() as conn:
            result = conn.execute(docs_query, {"docket_id": docket_id})
            doc_row = result.fetchone()

        if doc_row:
            doc_data = dict(doc_row._mapping)
            print(f"\n📄 Document Columns ({len(doc_data)} fields):")
            for key in sorted(doc_data.keys()):
                print(f"   {key}")

        # Check case history
        history_table = f"{schema}.{table_prefix}cases"
        history_query = text(f"""
            SELECT COUNT(*) as count, MIN(created_at) as first_seen, MAX(created_at) as last_seen
            FROM {history_table}
            WHERE docket_id = :docket_id
        """)

        with engine.connect() as conn:
            result = conn.execute(history_query, {"docket_id": docket_id})
            history_stats = dict(result.fetchone()._mapping)

        print(f"\n📜 Case History:")
        print(f"   Snapshots: {history_stats['count']}")
        print(f"   First seen: {history_stats['first_seen']}")
        print(f"   Last seen: {history_stats['last_seen']}")

        # Get all columns from history table
        if history_stats['count'] > 0:
            history_cols_query = text(f"SELECT * FROM {history_table} WHERE docket_id = :docket_id LIMIT 1")
            with engine.connect() as conn:
                result = conn.execute(history_cols_query, {"docket_id": docket_id})
                history_row = result.fetchone()
                if history_row:
                    history_data = dict(history_row._mapping)
                    print(f"\n   History table columns ({len(history_data)} fields):")
                    for key in sorted(history_data.keys()):
                        print(f"      {key}")

        # Check transcriptions
        trans_table = f"{schema}.{table_prefix}docket_documents_transcriptions"
        trans_query = text(f"SELECT COUNT(*) as count FROM {trans_table} WHERE case_id = :case_id")

        with engine.connect() as conn:
            result = conn.execute(trans_query, {"case_id": case_id})
            trans_count = result.fetchone()[0]

        print(f"\n📝 OCR Transcriptions: {trans_count} pages")

        # Check if there are any additional metadata tables
        print(f"\n🔍 Checking for additional related tables...")

        # Look for tables that might contain case metadata
        related_tables_query = text(f"""
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = :schema
            AND tablename LIKE :prefix
            ORDER BY tablename
        """)

        with engine.connect() as conn:
            result = conn.execute(related_tables_query, {"schema": schema, "prefix": f"{table_prefix}%"})
            tables = [row[0] for row in result]

        print(f"\n   Available {table_prefix} tables in {schema}:")
        for table in tables:
            print(f"      - {table}")

        print("\n" + "=" * 80)
        print("ANALYSIS COMPLETE")
        print("=" * 80)

    finally:
        engine.dispose()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Inspect metadata available for a case."
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
        help="Postgres schema (default: courts_final)",
    )
    parser.add_argument(
        "--table-prefix",
        type=str,
        default="ny_",
        help="Table prefix (default: ny_)",
    )

    args = parser.parse_args()

    inspect_case(args.case_id, args.schema, args.table_prefix)


if __name__ == "__main__":
    main()
