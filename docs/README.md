# Documentation Index

## Event Extraction Pipeline

- **[Event Extraction Usage Guide](event_extraction_usage.md)** - What the pipeline does, config, CLI/Python API, extending it with a new stage
- **[Pipeline Outputs Reference](pipeline_outputs.md)** - Every stage's output files/fields and which tool (regex/LLM/library) produces each one
- **[How actors.json is Generated](actors_generation.md)** - Deep dive into Stage 1's actor-roster extraction (multi-source scan, dedup, LLM validation)

## Case Management

- **[Case Exporter Usage](case_exporter_usage.md)** - Exporting court cases (JSON + PDFs) from the database/GCS, PDF metadata extraction
- **[Local Case Browser](local_case_browser.md)** - Streamlit app for browsing locally-exported cases

## Classification

- **[Lawsuit Classification](lawsuit_classification.md)** - LLM-labeled training data → BERT classifier pipeline

## Database

- **[Court Tables Relationships](court_tables_relationships.md)** - Current `courts_final` schema and the deprecated `public.court_*` tables

## Quick Links

- Main [README.md](../README.md) - project overview and setup
- `config/event_extraction.toml` - event extraction + classification config
- `config/database.toml` / [config/README.md](../config/README.md) - database connection config
- `scripts/run_event_extraction.py`, `scripts/export_case.py` / `export_cases.py` - primary entry points
