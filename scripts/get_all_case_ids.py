#!/usr/bin/env python3
"""Get all case IDs from ny_cases_after_search table."""

import sys
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils import load_db_config


def main():
    """Get all case IDs and save to file."""
    # Create engine for scrapping database (port 5433)
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
    engine = create_engine(url)

    try:
        # Count total cases
        count_query = text("SELECT COUNT(DISTINCT id) FROM courts_final.ny_cases_after_search")

        with engine.connect() as conn:
            result = conn.execute(count_query)
            total_cases = result.scalar()

        print(f"Total cases in ny_cases_after_search: {total_cases:,}")

        # Get all case IDs
        ids_query = text("SELECT DISTINCT id FROM courts_final.ny_cases_after_search ORDER BY id")

        with engine.connect() as conn:
            result = conn.execute(ids_query)
            case_ids = [row[0] for row in result]

        print(f"Case IDs range: {min(case_ids)} to {max(case_ids)}")
        print(f"\nSaving case IDs to file...")

        # Save to file
        output_file = Path("ny_case_ids.txt")
        with open(output_file, "w") as f:
            f.write(",".join(map(str, case_ids)))

        print(f"Saved {len(case_ids)} case IDs to {output_file}")

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
