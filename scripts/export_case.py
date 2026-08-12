#!/usr/bin/env python3
"""Export a single court case by ID.

This script exports a specific court case with all its documents to a
denormalized JSON file and downloads associated PDFs from GCS.

Usage:
    python scripts/export_case.py 1229 --output-dir data/cases
    python scripts/export_case.py 1229  # Uses default output: data/cases

Requirements:
    - Cloud SQL Proxy must be running (make run-proxy)
    - Google Cloud authentication configured (gcloud auth application-default login)
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine

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


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Export a single court case with documents."
    )
    parser.add_argument(
        "case_id",
        type=int,
        help="Case ID from court_cases.id column",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cases"),
        help="Output directory for exported case (default: data/cases)",
    )
    parser.add_argument(
        "--bucket",
        type=str,
        default="court-docs",
        help="GCS bucket name (default: court-docs)",
    )

    args = parser.parse_args()

    print("=" * 80)
    print(f"Exporting Case ID: {args.case_id}")
    print("=" * 80)

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Create engine and exporter
    engine = create_scrapping_engine()

    try:
        exporter = CaseExporter(
            engine=engine,
            output_dir=args.output_dir,
            gcs_bucket_name=args.bucket,
        )

        # Export the case
        json_path = exporter.export_case_by_id(args.case_id)

        print("\n" + "=" * 80)
        print("✓ Export successful!")
        print(f"  JSON file: {json_path}")
        print(f"  Case directory: {json_path.parent}")
        print("=" * 80)

    except ValueError as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Failed to export case: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()