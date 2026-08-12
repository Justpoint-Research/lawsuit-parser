# Local Case Browser

A lightweight Streamlit app for browsing exported court cases from the local `data/cases` directory.

## Overview

The Local Case Browser allows you to:
- Browse cases exported to the `data/cases` directory
- View all case information from JSON files
- See all documents associated with a case
- Preview PDF documents directly from local storage
- Navigate between cases using Next/Prev buttons or dropdown selector

## Key Differences from Database Browser

| Feature | Database Browser (`case_browser.py`) | Local Browser (`case_browser_local.py`) |
|---------|--------------------------------------|------------------------------------------|
| **Data Source** | PostgreSQL database via Cloud SQL Proxy | Local JSON files in `data/cases/` |
| **PDF Source** | Downloads from GCS bucket | Reads from local filesystem |
| **Dependencies** | Requires database connection, GCS auth | No external dependencies |
| **Performance** | Network latency for DB queries and GCS | Instant - all data is local |
| **Use Case** | Live browsing of full dataset | Offline analysis of exported sample |

## Prerequisites

### 1. Export Cases First

Before using the local browser, you need to export cases:

```bash
# Export 20 random cases
uv run python scripts/export_random_cases.py --count 20

# Or export specific cases
uv run python scripts/export_random_cases.py --case-ids "273,51,70,350"
```

This creates the directory structure:
```
data/cases/
├── case_51/
│   ├── case_51.json
│   ├── documents/
│   │   └── *.pdf
│   └── confirmations/
│       └── *.pdf
├── case_70/
│   ├── case_70.json
│   ├── documents/
│   └── confirmations/
...
```

## Usage

### Start the Local Browser

```bash
streamlit run apps/case_browser_local.py
```

The app will open in your browser at http://localhost:8501

### Navigation

**Sidebar Controls:**
- **⏮️ First** - Jump to first case
- **◀️ Prev** - Previous case
- **Next ▶️** - Next case
- **Select Case** - Dropdown to jump to any case

**Document Preview:**
- Click **📖 Preview Document** to view the main PDF
- Click **📋 Preview Confirmation** to view confirmation PDF
- Uncheck to close preview

### Features

1. **Fast Loading** - No database queries, all data loaded from local JSON
2. **Offline Capable** - Works without internet or database connection
3. **PDF Preview** - Embedded PDF viewer for documents
4. **File Size Info** - Shows size of each PDF file
5. **Case Statistics** - Shows total cases and PDFs loaded

## Data Structure

Each case JSON file contains:

```json
{
  "case_info": {
    "id": 51,
    "case_id": "905261-26",
    "caption": "William Ringo v. AngioDynamics, Inc. et al",
    "court": "Albany County Supreme Court",
    "case_status": "Pre-RJI",
    ...
  },
  "documents": [
    {
      "document_name": "SUMMONS + COMPLAINT",
      "filed_by": "SMITH, EDWARD LEO",
      "filed_create": "05/06/2026",
      "local_document_path": "documents/document_xyz.pdf",
      "local_confirmation_path": "confirmations/document_xyz.pdf",
      ...
    }
  ],
  "summary": {
    "total_documents": 1,
    "exported_at": "2026-08-10T14:09:59.034937"
  }
}
```

## Advantages

✅ **No Database Required** - Works without Cloud SQL Proxy
✅ **No GCS Authentication** - PDFs are already downloaded locally
✅ **Fast Performance** - All data is local, no network latency
✅ **Portable** - Can share the `data/cases` folder with others
✅ **Offline Analysis** - Work without internet connection
✅ **Version Control Friendly** - Export specific cases for testing

## Use Cases

1. **Development & Testing** - Test with a small dataset without full database
2. **Offline Analysis** - Analyze cases on laptop without database access
3. **Data Sharing** - Export and share specific cases with team members
4. **Case Studies** - Create curated sets of interesting cases
5. **Performance Testing** - Test UI changes without database overhead

## Troubleshooting

### No Cases Found

If you see "No cases found in data/cases directory":

1. Export cases first:
   ```bash
   uv run python scripts/export_random_cases.py --count 20
   ```

2. Verify the data directory exists:
   ```bash
   ls -la data/cases/
   ```

### PDF Not Found

If a PDF shows "⚠️ Document file not found":

1. The document may have failed to download during export (check export logs)
2. Some documents may have URL-encoded slashes that create unexpected subdirectories
3. Re-export the case to try downloading again

### Case Not Loading

If a case fails to load:

1. Check that the JSON file exists and is valid
2. Verify file permissions
3. Check the Streamlit error message for details

## Related Documentation

- [Case Exporter Usage Guide](case_exporter_usage.md) - How to export cases
- [Database Browser](../apps/README.md) - Original database-connected browser
- [Court Tables Relationships](court_tables_relationships.md) - Database schema

## Performance Notes

- **Instant Loading** - Cases load in milliseconds from JSON
- **PDF Rendering** - Large PDFs (>10MB) may take a moment to render
- **Memory Usage** - Each PDF preview is loaded into memory for display
- **Recommended Dataset Size** - Works well with 20-100 cases, tested up to 1000 cases
