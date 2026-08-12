# Court Tables Relational Map

## Tables Overview

**court_cases** (2,897 rows)
- Main table containing current/active court cases
- Primary key: `id` (bigint)
- Case identifier: `case_id` (text) - the actual case number like "810760/2021E"
- Docket identifier: `docket_id` (text) - encoded docket identifier

**court_casesbacks** (4.6M rows)
- Historical archive/backup of court cases
- Same schema as court_cases (minus documents_scrapped_at column)
- Primary key: `id` (bigint)
- Contains historical snapshots of cases over time

**court_documents** (170K rows)
- Individual documents filed in court cases
- Primary key: `id` (bigint)
- Links to cases via `case_id` (text) and `docket_id` (text)

**court_log_events** (167K rows)
- Event logs at the court level
- Primary key: `id` (bigint)
- Links to court information via `court_id` (text)

**court_transcriptions** (0 rows - empty)
- OCR transcriptions of document pages
- Primary key: `id` (bigint)
- Links via `case_id` (bigint) - references court_cases.id or court_documents.id
- Links via `case_file_id` (bigint) - likely references court_documents.id

## How They Link

### Main Case Linking (via case_id text field):
```
court_cases.case_id (text: "810760/2021E")
    ↓
court_casesbacks.case_id (text: "810760/2021E")
    ↓
court_documents.case_id (text: "810760/2021E")
```

**Relationship**: All documents and historical records for a case share the same `case_id` value.

### Alternative Linking (via docket_id):
```
court_cases.docket_id (text: encoded identifier)
    ↓
court_casesbacks.docket_id (text: same encoded identifier)
    ↓
court_documents.docket_id (text: same encoded identifier)
```

**Relationship**: `docket_id` provides an alternative way to link cases and documents.

### Court Linking (via court_id):
```
court_cases.court_id (text: "119", "3", etc.)
    ↓
court_casesbacks.court_id (text: same court identifier)
    ↓
court_log_events.court_id (text: same court identifier)
```

**Relationship**: Links cases to specific courts and their event logs.

### Transcription Linking (via numeric IDs):
```
court_transcriptions.case_id (bigint)
    → court_cases.id (bigint)

court_transcriptions.case_file_id (bigint)
    → court_documents.id (bigint)
```

**Relationship**: Transcriptions link to the numeric primary keys, not the text case_id field.

## Key Notes

1. **No formal foreign key constraints** - All relationships are implicit through shared column names
2. **case_id is the primary linking field** - Appears in 4 tables and connects cases to their documents and history
3. **Data type inconsistency** - case_id is text in most tables but bigint in court_transcriptions (links to different field)
4. **court_casesbacks appears to be a temporal archive** - Much larger than court_cases, likely stores historical snapshots