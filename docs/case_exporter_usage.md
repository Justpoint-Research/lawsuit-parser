# Case Exporter Usage Guide

The Case Exporter provides utilities to export complete court case information from the PostgreSQL database into denormalized JSON files, along with downloading associated PDF documents from Google Cloud Storage.

## Overview

The case exporter creates a comprehensive snapshot of a court case including:
- All case metadata from the `court_cases` table
- All associated documents from the `court_documents` table
- Downloaded PDF files from GCS (both main documents and confirmations)
- A denormalized JSON file containing all information in a single, easy-to-consume format

## Features

- **Denormalized JSON**: All case and document data in a single JSON file
- **Automatic GCS Downloads**: Downloads all PDF files referenced in the case
- **Organized Directory Structure**: Each case gets its own directory with subdirectories for documents
- **Error Handling**: Gracefully handles missing files and continues with export
- **Batch Processing**: Can export multiple cases at once

## Setup

### Prerequisites

1. **Cloud SQL Proxy** must be running:
   ```bash
   make run-proxy
   ```
   This connects to both databases:
   - hidden-danger on port 5432
   - scrapping on port 5433

2. **Google Cloud Authentication**:
   ```bash
   gcloud auth application-default login
   ```
   This allows the app to download PDF files from GCS buckets.

3. **Verify GCS Access** (optional):
   ```bash
   uv run python scripts/list_gcs_buckets.py list-buckets
   ```
   This shows available GCS buckets you have access to.

### Finding the Correct GCS Bucket

The PDF files are stored in a GCS bucket named **`court-docs`** under the `document_link/` prefix.

To verify bucket access:

```bash
# List all buckets
uv run python scripts/list_gcs_buckets.py list-buckets

# List files in the court-docs bucket
uv run python scripts/list_gcs_buckets.py list-files --bucket court-docs --limit 10

# Search for document files
uv run python scripts/list_gcs_buckets.py find-files --bucket court-docs --search "document_"
```

**Note**: Documents are stored at `gs://court-docs/document_link/...`

### Export a Single Case

Export a specific case by its database ID (`court_cases.id`):

```bash
# Using default output directory (data/cases)
uv run python scripts/export_case.py 1229

# Specify output directory (bucket defaults to court-docs)
uv run python scripts/export_case.py 1229 --output-dir exports/my_cases
```

### Export Random Sample of Cases

Export a random sample of cases (default: 100):

```bash
# Export 100 random cases
uv run python scripts/export_random_cases.py

# Export specific number of cases
uv run python scripts/export_random_cases.py --count 50

# Specify output directory
uv run python scripts/export_random_cases.py --count 100 --output-dir data/sample_cases
```

### Export Specific Cases

Export specific cases by providing comma-separated IDs:

```bash
uv run python scripts/export_random_cases.py --case-ids "273,51,70,350"
```

## Output Structure

Each exported case creates the following directory structure:

```
data/cases/
└── case_1229/
    ├── case_1229.json              # Denormalized case data
    ├── documents/                  # Main document PDFs
    │   ├── document_xyz123.pdf
    │   └── document_abc456.pdf
    └── confirmations/              # Confirmation document PDFs
        ├── document_xyz123.pdf
        └── document_abc456.pdf
```

## JSON File Format

The exported JSON file has the following structure:

```json
{
  "case_info": {
    "id": 51,
    "docket_id": "L9M_PLUS_TDHmMBLleTS3S1nzVQ==",
    "case_id": "905261-26",
    "caption": "William Ringo v. AngioDynamics, Inc. et al",
    "court": "Albany County Supreme Court",
    "case_status": "Pre-RJI",
    "case_type": "Torts - Product Liability",
    "case_received_date": "05/06/2026",
    ...
  },
  "documents": [
    {
      "id": 24373,
      "document_name": "SUMMONS + COMPLAINT",
      "document_link": "https://...",
      "document_bucket_link": "document_link/document_xyz.pdf",
      "local_document_path": "documents/document_xyz.pdf",
      "filed_by": "SMITH, EDWARD LEO",
      "filed_create": "05/06/2026",
      ...
    }
  ],
  "summary": {
    "total_documents": 1,
    "case_id": "905261-26",
    "caption": "William Ringo v. AngioDynamics, Inc. et al",
    "court": "Albany County Supreme Court",
    "exported_at": "2026-08-10T14:09:59.034937"
  }
}
```

## Using as a Python Module

You can also use the `CaseExporter` class directly in your Python code:

```python
from pathlib import Path
from sqlalchemy import create_engine
from lawsuit_parser.utils import CaseExporter, load_db_config

# Create engine for scrapping database (port 5433)
config = load_db_config()
config['port'] = 5433

from sqlalchemy.engine import URL
url = URL.create(
    drivername='postgresql+psycopg',
    username=config['user'],
    password=config['password'],
    host=config['host'],
    port=int(config['port']),
    database=config['database'],
)
engine = create_engine(url)

# Create exporter
exporter = CaseExporter(
    engine=engine,
    output_dir=Path("data/my_exports"),
    gcs_bucket_name="court-docs"  # Default bucket
)

# Export a case
try:
    json_path = exporter.export_case_by_id(1229)
    print(f"Exported to: {json_path}")
finally:
    engine.dispose()
```

## Finding Cases with Documents

To find cases that have associated documents:

```python
from sqlalchemy import create_engine, text
from lawsuit_parser.utils import load_db_config

config = load_db_config()
config['port'] = 5433

from sqlalchemy.engine import URL
url = URL.create(
    drivername='postgresql+psycopg',
    username=config['user'],
    password=config['password'],
    host=config['host'],
    port=int(config['port']),
    database=config['database'],
)
engine = create_engine(url)

query = text('''
SELECT c.id, c.case_id, c.caption, COUNT(d.id) as doc_count
FROM public.court_cases c
LEFT JOIN public.court_documents d ON c.case_id = d.case_id
WHERE c.case_id IS NOT NULL AND c.case_id != 'Not Assigned'
GROUP BY c.id, c.case_id, c.caption
HAVING COUNT(d.id) > 0
ORDER BY doc_count DESC
LIMIT 10
''')

with engine.connect() as conn:
    result = conn.execute(query)
    for row in result:
        print(f"Case ID: {row[0]}, Documents: {row[3]}, Caption: {row[2]}")

engine.dispose()
```

## Troubleshooting

### GCS Bucket Not Found

If you see errors like "The specified bucket does not exist", you need to:

1. Find the correct GCS bucket name
2. Ensure you have read access to the bucket
3. Specify the correct bucket name using the `--bucket` parameter

Try common bucket names:
- `nyscef-documents`
- `nyscef-files`
- `court-documents`
- `scrapping-documents`

### No Documents Exported

Some cases may not have associated documents in the database. This happens when:
- The case's `case_id` is "Not Assigned"
- Documents haven't been scraped yet (`documents_scrapped_at` is NULL)
- The `case_id` in `court_cases` doesn't match any `case_id` in `court_documents`

The JSON file will still be created with an empty `documents` array.

### Database Connection Issues

Make sure:
1. Cloud SQL Proxy is running: `make run-proxy`
2. The proxy is connected to the scrapping database on port 5433
3. Your credentials are configured in `~/.config/lawsuit-parser/secrets.toml`

## Performance Tips

- Exporting cases with many documents will take longer due to PDF downloads
- Use `--count` parameter to control batch size
- GCS downloads are skipped for files that already exist (re-running is safe)
- Failed downloads are logged as warnings but don't stop the export

## Related Documentation

- [Court Tables Relational Map](court_tables_relationships.md) - Understanding table relationships
- [Database Configuration](../config/README.md) - Setting up database access