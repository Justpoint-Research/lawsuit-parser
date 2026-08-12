# Scripts

This directory contains data processing scripts, utilities, and automation tools.

## Example Scripts

### `example_query.py`

Demonstrates database connectivity and query execution with the lawsuit_parser package.

```bash
# Test database connection
python scripts/example_query.py test-connection

# List tables in the database
python scripts/example_query.py list-tables --limit 10

# Show database schemas
python scripts/example_query.py show-schemas

# Run a custom query
python scripts/example_query.py run-query "SELECT version()"

# Get help for all commands
python scripts/example_query.py --help
```

### `list_gcs_buckets.py`

Utilities for working with Google Cloud Storage buckets. Essential for setting up PDF preview in the Case Browser app.

```bash
# Check if you're authenticated with GCS
python scripts/list_gcs_buckets.py check-auth

# List all available GCS buckets
python scripts/list_gcs_buckets.py list-buckets

# List files in a specific bucket
python scripts/list_gcs_buckets.py list-files --bucket BUCKET_NAME
python scripts/list_gcs_buckets.py list-files --bucket BUCKET_NAME --prefix document_link/
python scripts/list_gcs_buckets.py list-files --bucket BUCKET_NAME --limit 50

# Search for files matching a pattern
python scripts/list_gcs_buckets.py find-files --bucket BUCKET_NAME --search "document_"

# Get help
python scripts/list_gcs_buckets.py --help
```

**Note:** Requires authentication: `gcloud auth application-default login`

### `export_case.py`

Export a single court case with all documents to denormalized JSON and download PDFs from GCS.

```bash
# Export a specific case
uv run python scripts/export_case.py 1229

# Specify output directory and bucket
uv run python scripts/export_case.py 1229 --output-dir data/my_cases --bucket nyscef-documents
```

See [Case Exporter Usage Guide](../docs/case_exporter_usage.md) for detailed documentation.

### `export_random_cases.py`

Export a random sample of court cases with all documents.

```bash
# Export 100 random cases (default)
uv run python scripts/export_random_cases.py

# Export specific number
uv run python scripts/export_random_cases.py --count 50

# Export specific cases by ID
uv run python scripts/export_random_cases.py --case-ids "273,51,70"

# Custom output directory
uv run python scripts/export_random_cases.py --count 100 --output-dir data/sample_cases
```

**Requires:**
- Cloud SQL Proxy running (`make run-proxy`)
- GCS authentication (`gcloud auth application-default login`)

### `examine_court_tables.py`

Explores the structure of court-related tables in the scrapping database.

```bash
uv run python scripts/examine_court_tables.py
```

This script:
- Connects to the scrapping database (port 5433)
- Lists all tables starting with 'court'
- Shows column information and sample data
- Identifies GCS link columns

## Guidelines

- Use Click or argparse for command-line interfaces
- Make scripts executable with proper shebang lines (`#!/usr/bin/env python3`)
- Document script usage with `--help` flags
- Import from the main `lawsuit_parser` package for shared functionality
- Add parent directory to path: `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`
