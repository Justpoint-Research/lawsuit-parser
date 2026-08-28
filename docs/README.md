# Documentation Index

This directory contains all documentation for the lawsuit-parser project.

## Event Extraction Pipeline

The event extraction pipeline is a modular system for extracting legal events and timelines from parsed Docling documents.

### Overview & Usage
- **[Event Extraction Usage Guide](event_extraction_usage.md)** - Complete guide on how to use the event extraction pipeline, including installation, configuration, and examples
- **[Pipeline Design](event_extraction_pipeline_design.md)** - Architectural design and extensibility patterns
- **[Implementation Summary](event_extraction_implementation.md)** - Overview of what was implemented, features, and technical details

### Technical Reference
- **[Pipeline Outputs Reference](pipeline_outputs.md)** - Complete reference for what each stage produces and which tools are used for extraction
- **[How actors.json is Generated](actors_generation.md)** - Deep dive into the actor roster extraction process (Stage 1)
- **[MDL-1954 Analysis: Why Only Generic Placeholders](actors_generation_mdl_1954_analysis.md)** - Case study of extraction failure on MDL documents

## Case Management

- **[Case Exporter Usage](case_exporter_usage.md)** - Guide for exporting court cases with documents to JSON files and downloading PDFs
- **[Local Case Browser](local_case_browser.md)** - Documentation for the Streamlit case browsing application

## Database

- **[Court Tables Relationships](court_tables_relationships.md)** - Database schema and table relationships for court data

## Quick Links

### Getting Started
- Main [README.md](../README.md) - Project overview and setup instructions
- [Event Extraction Usage Guide](event_extraction_usage.md) - Start here for event extraction

### Configuration
- `config/event_extraction.toml` - Event extraction pipeline configuration
- `config/database.toml` - Database connection configuration

### Scripts
- `scripts/run_event_extraction.py` - Run the event extraction pipeline
- `scripts/export_case.py` - Export individual cases
- `scripts/export_random_cases.py` - Batch export cases
