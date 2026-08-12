#!/usr/bin/env python3
"""Export a random sample of court cases with their documents.

This script connects to the scrapping database (port 5433), selects a random
sample of court cases, and exports them to denormalized JSON files along with
downloading their associated PDF documents from Google Cloud Storage.

Usage:
    python scripts/export_random_cases.py --count 100 --output-dir data/cases

Requirements:
    - Cloud SQL Proxy must be running (make run-proxy)
    - Google Cloud authentication configured (gcloud auth application-default login)
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils import CaseExporter, load_db_config


def create_scrapping_engine():
    """Create SQLAlchemy engine for the scrapping database (port 5433).

    Returns:
        SQLAlchemy engine connected to scrapping database.
    """
    # Load config but override port to 5433 for scrapping database
    config = load_db_config()
    config["port"] = 5433  # Scrapping database port

    from sqlalchemy.engine import URL

    url = URL.create(
        drivername="postgresql+psycopg",
        username=config["user"],
        password=config["password"],
        host=config["host"],
        port=int(config["port"]),
        database=config["database"],
    )
    return create_engine(url)


def get_random_case_ids(engine, count: int = 100) -> list[int]:
    """Get random case IDs from the court_cases table.

    Args:
        engine: SQLAlchemy engine.
        count: Number of random cases to sample.

    Returns:
        List of case IDs.
    """
    query = text("""
        SELECT id
        FROM public.court_cases
        WHERE case_id IS NOT NULL  -- Filter out cases without case_id
        ORDER BY RANDOM()
        LIMIT :count
    """)

    with engine.connect() as conn:
        result = conn.execute(query, {"count": count})
        case_ids = [row[0] for row in result]

    return case_ids


def export_cases(
    case_ids: list[int],
    output_dir: Path,
    bucket_name: str = "court-docs",
):
    """Export multiple cases.

    Args:
        case_ids: List of case IDs to export.
        output_dir: Output directory for exported cases.
        bucket_name: GCS bucket name where documents are stored.
    """
    engine = create_scrapping_engine()

    try:
        exporter = CaseExporter(
            engine=engine,
            output_dir=output_dir,
            gcs_bucket_name=bucket_name,
        )

        total = len(case_ids)
        successful = 0
        failed = 0

        for idx, case_id in enumerate(case_ids, 1):
            try:
                print(f"\n[{idx}/{total}] Exporting case {case_id}...")
                json_path = exporter.export_case_by_id(case_id)
                print(f"✓ Successfully exported to {json_path}")
                successful += 1
            except Exception as e:
                print(f"✗ Failed to export case {case_id}: {e}")
                failed += 1

        print("\n" + "=" * 80)
        print(f"Export complete!")
        print(f"  Successful: {successful}/{total}")
        print(f"  Failed: {failed}/{total}")
        print(f"  Output directory: {output_dir}")
        print("=" * 80)

    finally:
        engine.dispose()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Export random court cases with documents from the scrapping database."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        help="Number of random cases to export (default: 100)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cases"),
        help="Output directory for exported cases (default: data/cases)",
    )
    parser.add_argument(
        "--bucket",
        type=str,
        default="court-docs",
        help="GCS bucket name (default: court-docs)",
    )
    parser.add_argument(
        "--case-ids",
        type=str,
        help="Comma-separated list of specific case IDs to export (skips random sampling)",
    )

    args = parser.parse_args()

    print("=" * 80)
    print("Court Case Exporter")
    print("=" * 80)

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Get case IDs
    if args.case_ids:
        # Parse specific case IDs
        case_ids = [int(x.strip()) for x in args.case_ids.split(",")]
        print(f"Exporting {len(case_ids)} specific cases...")
    else:
        # Get random sample
        print(f"Sampling {args.count} random cases from the database...")
        engine = create_scrapping_engine()
        try:
            case_ids = get_random_case_ids(engine, args.count)
        finally:
            engine.dispose()

        if not case_ids:
            print("Error: No cases found in the database.")
            sys.exit(1)

        print(f"Selected {len(case_ids)} cases for export.")

    # Export cases
    export_cases(case_ids, args.output_dir, args.bucket)


if __name__ == "__main__":
    main()