# Event Extraction Pipeline

A modular, extensible pipeline for extracting legal events and timelines from parsed Docling documents.

## Overview

This pipeline processes legal case documents to extract:
- **Parties** (plaintiffs, defendants, attorneys, etc.)
- **Events** (filings, motions, hearings, rulings, etc.)
- **Timelines** (chronological sequence of events)
- **Entities** (dates, locations, monetary amounts, case citations, etc.)

## Architecture

The pipeline uses a **stage-based architecture** that is designed for extensibility:

### Stage 1: Metadata Extraction
**Purpose:** Extract ground truth metadata from all available sources

**Inputs:**
- Database records (PostgreSQL)
- PDF file metadata
- Docling parsed documents (`.docling.json`, `.json`)
- Document headers (CM/ECF information)

**Outputs:**
- `files_scan.json` - Complete metadata scan with dates extracted
- `gliner_config.json` - GLiNER configuration with dynamic actor labels

### Stage 2: GLiNER Entity Detection
**Purpose:** Extract all entity mentions using zero-shot NER

**Inputs:**
- `gliner_config.json` from Stage 1
- Canonical text from documents

**Outputs:**
- `entities.json` - All detected entities with locations and scores

### Future Stages (Extensible)

The pipeline is designed to support additional stages:
- **Stage 3:** Event extraction from entities
- **Stage 4:** Timeline construction
- **Stage 5:** Event relationship extraction
- **Stage 6:** Temporal ordering and resolution

## Installation

The event extraction pipeline is part of the `lawsuit-parser` package. Ensure you have:

```bash
# Install dependencies
pip install -e .

# Required packages:
# - PyPDF2 (for PDF metadata)
# - gliner (for entity extraction)
# - pydantic (for data validation)
# - sqlalchemy (for database queries)
```

## Configuration

Configuration is managed via `config/event_extraction.toml`:

```toml
[paths]
data_root = "data/cases"      # source case data: documents/, confirmations/, docling/
output_root = "data/extraction"  # pipeline-generated artifacts, wipeable independently
events_dir = "events"

[stage_1]
extract_from_database = true
extract_from_pdfs = true
extract_from_docling = true
extract_from_confirmations = true  # filer/judge/timestamp from confirmations/ notices
date_patterns = [
    "\\d{1,2}/\\d{1,2}/\\d{4}",
    "\\d{4}-\\d{2}-\\d{2}",
    # ... more patterns
]

[stage_2]
model = "urchade/gliner_multi-v2.1"
threshold = 0.5
batch_size = 8
use_gpu = true
static_labels = [
    "temporal expression",
    "legal action or event",
    "court",
    # ... more labels
]
```

## Usage

### Command Line Interface

```bash
# Run all stages for a case
python scripts/run_event_extraction.py case_67

# Run specific stages
python scripts/run_event_extraction.py case_67 --stages 1 2

# Check pipeline status
python scripts/run_event_extraction.py case_67 --status

# Force re-run (overwrite existing outputs)
python scripts/run_event_extraction.py case_67 --force

# Use custom config
python scripts/run_event_extraction.py case_67 --config my_config.toml
```

### Python API

```python
from lawsuit_parser.event_extraction import EventExtractionPipeline

# Initialize pipeline
pipeline = EventExtractionPipeline()

# Run all stages
pipeline.run_all_stages("case_67")

# Run specific stages
pipeline.run_stages("case_67", stages=[1, 2])

# Check status
pipeline.print_status("case_67")
```

## Data Directory Structure

Source case data and pipeline outputs live under separate roots
(`data_root` and `output_root`), so an iteration's generated artifacts can
be deleted and regenerated without touching source data:

```
data/cases/<case_id>/                       # data_root - source data
├── documents/                              # the actual filed documents
│   ├── document_<id>.pdf
│   └── document_<id>.json                  # Docling-simplified parse
├── confirmations/                          # e-filing acknowledgement notices
│   ├── document_<id>.pdf                   # same id/name as its documents/ counterpart
│   └── document_<id>.json                  # metadata source only - filer, judge,
│                                            # timestamp; Stage 2 never runs on these
└── docling/
    ├── documents/document_<id>.docling.json
    └── confirmations/document_<id>.docling.json

data/extraction/<case_id>/                  # output_root - generated, safe to wipe
├── events/
│   ├── files_scan.json                     # Stage 1: Metadata scan
│   ├── gliner_config.json                  # Stage 1: GLiNER configuration
│   └── entities.json                       # Stage 2: Detected entities
└── stages/
```

## Output Format

### files_scan.json

```json
{
  "case_id": "case_67",
  "scan_timestamp": "2026-08-20T10:00:00Z",
  "database_metadata": {
    "case_number": "3:24-cv-12345",
    "court": "Northern District of California",
    "plaintiff": ["Jane Doe"],
    "defendant": ["ACME Inc."],
    "case_filed_date": "2024-01-15"
  },
  "documents": [
    {
      "doc_id": "doc_000",
      "file_name": "complaint.pdf",
      "document_title": "Complaint",
      "filing_date": "2024-01-15",
      "extracted_dates": [...]
    }
  ],
  "parties_discovered": [
    {
      "name": "Jane Doe",
      "role": "plaintiff",
      "source": "database",
      "aliases": ["Doe", "Ms. Doe"]
    }
  ]
}
```

### gliner_config.json

```json
{
  "model": "urchade/gliner_multi-v2.1",
  "threshold": 0.5,
  "batch_size": 8,
  "labels": {
    "static": ["temporal expression", "legal action or event", ...],
    "dynamic": ["plaintiff (Jane Doe)", "defendant (ACME Inc.)", ...]
  },
  "actors": [
    {
      "canonical_name": "Jane Doe",
      "role": "plaintiff",
      "aliases": ["Doe", "Ms. Doe"],
      "gliner_label": "plaintiff (Jane Doe)"
    }
  ]
}
```

### entities.json

```json
{
  "case_id": "case_67",
  "extraction_timestamp": "2026-08-20T10:15:00Z",
  "model_config": {
    "model": "urchade/gliner_multi-v2.1",
    "threshold": 0.5
  },
  "entities": [
    {
      "entity_id": "ent_001",
      "text": "Jane Doe",
      "label": "plaintiff (Jane Doe)",
      "score": 0.95,
      "doc_id": "doc_000",
      "char_start": 1234,
      "char_end": 1242,
      "linked_actor": "Jane Doe",
      "context": "This action was filed by **Jane Doe** against ACME Inc. on January 15, 2024. The complaint alleges breach of contract. ACME disputes the claim in its entirety. A hearing has been scheduled for March 2024."
    }
  ],
  "entity_counts": {
    "temporal expression": 45,
    "plaintiff (Jane Doe)": 23,
    "defendant (ACME Inc.)": 31
  }
}
```

## Extending the Pipeline

To add a new stage:

1. **Create stage class** in `lawsuit_parser/event_extraction/stages/`:

```python
# stage_3_timeline.py
from ..base import BaseStage
from ..models import EventTimeline

class Stage3Timeline(BaseStage):
    stage_number = 3
    stage_name = "timeline"

    def run(self, case_id: str, config: dict) -> None:
        # Load entities from Stage 2
        entities = self.load_artifact(case_id, "entities.json", EntitiesArtifact)

        # Build timeline
        timeline = self.build_timeline(entities)

        # Save output
        self.save_artifact(case_id, "events.json", timeline)

    def validate_inputs(self, case_id: str) -> bool:
        return self.artifact_exists(case_id, "entities.json")

    def get_outputs(self, case_id: str) -> list[Path]:
        return [self.get_events_dir(case_id) / "events.json"]
```

2. **Register stage** in `stages/__init__.py`:

```python
from .stage_3_timeline import Stage3Timeline

STAGES = [
    Stage1Metadata,
    Stage2GLiNER,
    Stage3Timeline,  # Add new stage
]
```

3. **Add configuration** (optional) in `config/event_extraction.toml`:

```toml
[stage_3]
# Stage 3 specific configuration
```

That's it! The pipeline orchestrator will automatically discover and run the new stage.

## Design Principles

1. **Immutability** - Once a stage completes, its outputs are immutable
2. **Idempotency** - Running a stage twice produces identical results
3. **Provenance** - All extracted data includes source references and offsets
4. **Extensibility** - New stages can be added without modifying existing ones
5. **Validation** - Each stage validates its inputs before execution
6. **Error Handling** - Failures are logged but don't prevent other stages

## Integration with Existing Pipeline

This event extraction pipeline complements the existing extraction pipeline (`lawsuit_parser/extraction/`):

- **Existing pipeline** (Stages 0-4): Focused on NuExtract3-based metadata and GLiNER spans
- **Event pipeline** (Stages 1-N): Focused on event extraction and timeline construction

Both can coexist and share data:
- Event pipeline can read canonical text from existing pipeline's `documents/` directory
- Event pipeline outputs to separate `events/` directory

## Troubleshooting

### Case directory not found
Ensure the case exists in `data/cases/<case_id>/` with at least some PDF or parsed files.

### Database connection errors
If database is not configured, Stage 1 will skip database extraction gracefully. This is OK if you're only using file-based metadata.

### GLiNER GPU errors
If you encounter GPU errors, set `use_gpu = false` in config or ensure CUDA is properly installed.

### Missing dependencies
Install PyPDF2: `pip install PyPDF2`

## License

Part of the lawsuit-parser project.
