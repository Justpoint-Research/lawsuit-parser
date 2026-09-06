# PDF Metadata Extraction Implementation

## Overview
Added PDF metadata extraction to the `CaseExporter` to capture valuable information from PDF files that is not available in the database.

## Changes Made

### 1. Updated `lawsuit_parser/utils/case_exporter.py`

#### Added imports:
```python
import subprocess  # For running pdfinfo command
```

#### New method: `_extract_pdf_metadata()`
Extracts metadata from PDF files using the `pdfinfo` command-line tool:
- **Author** - Person who created the PDF
- **CreationDate** - When PDF was originally created
- **ModDate** - When PDF was last modified
- **Creator** - Software used to create PDF
- **Producer** - PDF processing pipeline
- **Pages** - Page count
- **PDF version** - PDF format version
- **File size** - Size in bytes
- Plus: Encrypted, Tagged, JavaScript, Form fields, etc.

Falls back gracefully if `pdfinfo` is not available.

#### Updated method: `_download_case_files()`
Now extracts PDF metadata after downloading each file:
- Extracts metadata for main documents
- Extracts metadata for confirmation documents
- Returns dictionary of metadata keyed by document ID
- Handles errors gracefully without breaking the export

#### Updated method: `_create_denormalized_json()`
Now includes PDF metadata in the exported JSON:
- Accepts `pdf_metadata_by_doc` parameter
- Adds `pdf_metadata` field to each document
- Adds `confirmation_pdf_metadata` field for confirmation PDFs

## Example Output

### Document with PDF Metadata:
```json
{
  "id": 57968,
  "document_name": "SUMMONS + COMPLAINT",
  "filed_by": "SEEGER, CHRISTOPHER A",
  "filed_create": "10/13/2025",
  "local_document_path": "documents/document_HhcQURsBeCTrTeIyWJZk1g==.pdf",

  "pdf_metadata": {
    "Author": "Gold, Danielle",
    "Creator": "Acrobat PDFMaker 25 for Word",
    "CreationDate": "Mon Oct 13 23:06:19 2025 CEST",
    "ModDate": "Tue Oct 14 17:51:28 2025 CEST",
    "Pages": "61",
    "File size": "537426 bytes",
    "PDF version": "1.6"
  },

  "confirmation_pdf_metadata": {
    "Title": "Confirmation Notice",
    "Creator": "JasperReports Library version 6.21.2",
    "CreationDate": "Mon Jun 22 22:37:32 2026 CEST",
    "Pages": "1"
  }
}
```

## Key Insights from PDF Metadata

### 1. Document Creator vs Filing Attorney
The PDF `Author` field often differs from the database `filed_by`:
- **Database (filed_by):** "SEEGER, CHRISTOPHER A" (attorney of record)
- **PDF (Author):** "Gold, Danielle" (actual document creator)

This reveals the support staff/paralegals who prepared documents.

### 2. Precise Timestamps
- **Database:** Date only ("10/13/2025")
- **PDF:** Full timestamp with timezone ("Mon Oct 13 23:06:19 2025 CEST")

### 3. Document Revisions
The `ModDate` field shows when documents were revised:
- Created: Oct 13 23:06:19 2025
- Modified: Oct 14 17:51:28 2025
- Shows 1-day review/revision cycle

### 4. Document Complexity
Page counts indicate document complexity:
- Summons + Complaint: 61 pages
- Answer: 10 pages
- Notice of Discontinuance: 1 page

### 5. Software Stack
All attorney-filed documents show consistent processing:
1. Created with: "Acrobat PDFMaker for Word"
2. Processed through: PDFsharp → Adobe PDF Library → iText
3. Court's standardized pipeline

Confirmation documents are system-generated:
- Creator: "JasperReports Library"
- Producer: "OpenPDF"

## Dependencies

Requires `pdfinfo` command-line tool (from poppler-utils):
- **macOS:** `brew install poppler`
- **Ubuntu/Debian:** `apt-get install poppler-utils`
- **Windows:** Download from poppler website

If not available, extraction is silently skipped with a warning message.

## Testing

Tested with case 227:
- ✅ All PDF metadata extracted successfully
- ✅ Both document and confirmation metadata captured
- ✅ JSON export includes all metadata fields
- ✅ Graceful handling when files exist (metadata extracted from existing files)

## Case 227 Source URLs

### Case-Level URLs:
- **Query Link:** https://iapps.courts.state.ny.us/nyscef/CaseSearch?TAB=courtDateRange
- **Case Link:** https://iapps.courts.state.ny.us/nyscef/DocumentList?docketId=twsuPpAafWf9eM/p6GOK/Q==&display=all

### Document URLs (3 documents):

#### Document 57968 (SUMMONS + COMPLAINT):
- View: https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=HhcQURsBeCTrTeIyWJZk1g==
- Confirmation: https://iapps.courts.state.ny.us/nyscef/ConfirmationNotice?docId=HhcQURsBeCTrTeIyWJZk1g==

#### Document 57969 (ANSWER):
- View: https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=A48Z8QqCgjoEOLm3r0kIfw==
- Confirmation: https://iapps.courts.state.ny.us/nyscef/ConfirmationNotice?docId=A48Z8QqCgjoEOLm3r0kIfw==

#### Document 57970 (NOTICE OF DISCONTINUANCE):
- View: https://iapps.courts.state.ny.us/nyscef/ViewDocument?docIndex=dzND6tKRG09UvJIvnSzGbA==
- Confirmation: https://iapps.courts.state.ny.us/nyscef/ConfirmationNotice?docId=dzND6tKRG09UvJIvnSzGbA==

All URLs are already included in the exported JSON under the respective fields.
