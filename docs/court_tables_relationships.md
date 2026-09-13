# Court Tables Relational Map

## Current schema

`CaseExporter` and every export script read from schema `courts_final`, tables prefixed per state
(`ny_`, `fl_`, ...):

- `{prefix}cases_after_search` - current/active cases (primary key `id`)
- `{prefix}docket_documents` - documents filed per case, joined to cases on `docket_id` (**not**
  `case_id` - `case_id`, the human-readable docket number like `"622075/2025"`, is not unique
  across courts; `docket_id` is the scraper-assigned unique identifier)
- `{prefix}cases` - historical snapshots of the cases table (same schema, captures earlier
  `case_status`/`efiling_status` values over time)
- `{prefix}docket_documents_transcriptions` - OCR page transcriptions, linked to documents
- `{prefix}log_events` - per-court scrape-run log (court + date only, no case-level link, so it
  can't be attached to a single case export)

No formal foreign keys - all links are implicit through shared column names. See
[Case Exporter Usage](case_exporter_usage.md) for how these are queried and exported.

## Deprecated schema

An older, unprefixed table set existed under `public` (`court_cases`, `court_documents`,
`court_casesbacks`, `court_log_events`, `court_transcriptions`, joined on `case_id`/`court_id`).
That data now lives in `courts_final` under the current schema above - if you find code or notes
referencing the `public.court_*` tables, treat them as historical.
