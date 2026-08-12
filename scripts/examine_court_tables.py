#!/usr/bin/env python3
"""Examine the structure of court tables in the scrapping database."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from sqlalchemy import create_engine, text
import tomllib

# Load secrets
secrets_path = Path.home() / ".config" / "lawsuit-parser" / "secrets.toml"
with open(secrets_path, "rb") as f:
    secrets = tomllib.load(f)

# Connect to scrapping database on port 5433
# Try both 'postgres' and 'database' keys for compatibility
secrets_data = secrets.get("postgres", secrets.get("database", {}))
user = secrets_data["user"]
password = secrets_data["password"]
engine = create_engine(
    f"postgresql+psycopg://{user}:{password}@127.0.0.1:5433/postgres"
)

print("=" * 80)
print("EXAMINING COURT TABLES IN SCRAPPING DATABASE")
print("=" * 80)

# Find all court tables
query = text("""
SELECT
    schemaname,
    tablename,
    schemaname || '.' || tablename as full_name
FROM pg_tables
WHERE tablename LIKE 'court%%'
ORDER BY schemaname, tablename
""")

court_tables = pd.read_sql(query, engine)
print(f"\nFound {len(court_tables)} tables starting with 'court':")
for _, row in court_tables.iterrows():
    print(f"  - {row['full_name']}")

print("\n" + "=" * 80)

# Examine each table
for idx, row in court_tables.iterrows():
    schema = row['schemaname']
    table = row['tablename']
    full_name = row['full_name']

    print(f"\nTable: {full_name}")
    print("-" * 80)

    try:
        # Get row count
        count_query = f"SELECT COUNT(*) FROM {full_name}"
        with engine.connect() as conn:
            count = conn.execute(text(count_query)).scalar()
        print(f"Total rows: {count:,}")

        # Get column info
        info_query = f"""
        SELECT
            column_name,
            data_type,
            character_maximum_length,
            is_nullable
        FROM information_schema.columns
        WHERE table_schema = '{schema}' AND table_name = '{table}'
        ORDER BY ordinal_position
        """
        columns_info = pd.read_sql(info_query, engine)
        print(f"\nColumns ({len(columns_info)}):")
        print(columns_info.to_string(index=False))

        # Get sample data
        sample_query = f"SELECT * FROM {full_name} LIMIT 3"
        df = pd.read_sql(sample_query, engine)
        print(f"\nSample data (first 3 rows):")
        print(df.to_string())

        # Look for GCS links
        gcs_columns = [col for col in df.columns if 'url' in col.lower() or 'link' in col.lower() or 'path' in col.lower() or 'gcs' in col.lower()]
        if gcs_columns:
            print(f"\nPotential GCS link columns: {gcs_columns}")

    except Exception as e:
        print(f"Error reading table {full_name}: {e}")

    print("\n")

engine.dispose()
print("\nDone!")
