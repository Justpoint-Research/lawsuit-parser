# Event Extraction Pipeline Design

## Overview

A modular, extensible pipeline for extracting legal events and timelines from parsed Docling documents.

## Architecture

### Directory Structure
```
lawsuit_parser/event_extraction/
├── __init__.py
├── pipeline.py          # Main pipeline orchestrator with stage registry
├── base.py              # Base stage class and protocols
├── models.py            # Pydantic data models
├── config.py            # Configuration management
├── stages/
│   ├── __init__.py
│   ├── stage_1_metadata.py    # Metadata extraction
│   └── stage_2_gliner.py      # GLiNER entity detection
└── utils.py             # Shared utilities
```

### Output Directory Structure
```
data/cases/<case_id>/events/
├── files_scan.json       # Stage 1: Metadata and dates from all sources
├── gliner_config.json    # Stage 1: GLiNER configuration with dynamic labels
├── entities.json         # Stage 2: Detected entities
└── events.json           # Future: Timeline of events
```

## Stage Definitions

### Stage 1: Metadata Extraction

**Purpose:** Extract ground truth metadata from all available sources

**Input Sources:**
1. Database (PostgreSQL)
   - Case metadata table
   - Document metadata table
   - Court records
2. PDF files
   - File metadata (creation date, modified date)
   - PDF document properties
3. Docling parsed files
   - `.docling.json` - Full document structure
   - `.json` - Simplified ParsedDocument
   - Headers containing CM/ECF data

**Outputs:**

1. `files_scan.json`:
```json
{
  "case_id": "case_67",
  "scan_timestamp": "2026-08-20T10:00:00Z",
  "database_metadata": {
    "case_number": "3:24-cv-12345",
    "court": "Northern District of California",
    "plaintiff": ["Jane Doe"],
    "defendant": ["ACME Inc.", "John Smith"],
    "case_filed_date": "2024-01-15",
    "status": "Active"
  },
  "documents": [
    {
      "doc_id": "doc_000",
      "file_name": "complaint.pdf",
      "document_number": "1",
      "document_title": "Complaint",
      "filing_date": "2024-01-15",
      "filed_by": "Jane Doe",
      "pdf_metadata": {
        "created": "2024-01-14T15:30:00Z",
        "modified": "2024-01-14T16:45:00Z",
        "pages": 42
      },
      "docling_metadata": {
        "title": "Complaint for Damages",
        "header": "Case 3:24-cv-12345 Document 1 Filed 01/15/2024",
        "cm_ecf": {
          "case_number": "3:24-cv-12345",
          "document_number": "1",
          "filing_date": "01/15/2024",
          "page_info": "Page 1 of 42"
        }
      },
      "extracted_dates": [
        {"text": "01/15/2024", "source": "cm_ecf_header", "type": "filing_date"},
        {"text": "January 14, 2024", "source": "document_body", "type": "event_date"},
        {"text": "2024-01-10", "source": "document_body", "type": "event_date"}
      ]
    }
  ],
  "parties_discovered": [
    {"name": "Jane Doe", "role": "plaintiff", "source": "database"},
    {"name": "ACME Inc.", "role": "defendant", "source": "database"},
    {"name": "John Smith", "role": "defendant", "source": "database"}
  ]
}
```

2. `gliner_config.json`:
```json
{
  "model": "urchade/gliner_multi-v2.1",
  "threshold": 0.5,
  "batch_size": 8,
  "labels": {
    "static": [
      "temporal expression",
      "legal action or event",
      "court",
      "geographic location",
      "monetary amount",
      "document reference"
    ],
    "dynamic": [
      "plaintiff (Jane Doe)",
      "defendant (ACME Inc.)",
      "defendant (John Smith)",
      "attorney",
      "witness",
      "judge"
    ]
  },
  "actors": [
    {
      "canonical_name": "Jane Doe",
      "role": "plaintiff",
      "aliases": ["Doe", "Ms. Doe", "Plaintiff"],
      "gliner_label": "plaintiff (Jane Doe)"
    },
    {
      "canonical_name": "ACME Inc.",
      "role": "defendant",
      "aliases": ["ACME", "ACME Corporation", "Defendant ACME"],
      "gliner_label": "defendant (ACME Inc.)"
    }
  ]
}
```

### Stage 2: GLiNER Entity Detection

**Purpose:** Extract all entity mentions using GLiNER with configured labels

**Input:**
- `gliner_config.json` from Stage 1
- Canonical text from `data/cases/<case_id>/documents/*.txt`
- Segmentation from existing pipeline (if available) or create simple paragraph-based segments

**Process:**
1. Load GLiNER model
2. Run batch prediction on all document segments
3. Realign spans to canonical text offsets
4. Link entities to known actors from Stage 1

**Output:** `entities.json`
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
      "context": "...filed by Jane Doe against..."
    },
    {
      "entity_id": "ent_002",
      "text": "January 15, 2024",
      "label": "temporal expression",
      "score": 0.89,
      "doc_id": "doc_000",
      "char_start": 2456,
      "char_end": 2472,
      "linked_actor": null,
      "context": "...filed on January 15, 2024 in..."
    }
  ],
  "entity_counts": {
    "temporal expression": 45,
    "plaintiff (Jane Doe)": 23,
    "defendant (ACME Inc.)": 31,
    "legal action or event": 18,
    "monetary amount": 7
  }
}
```

## Extensibility Design

### Stage Registry Pattern

```python
class Stage(Protocol):
    """Base protocol for pipeline stages."""

    stage_number: int
    stage_name: str

    def run(self, case_id: str, config: dict) -> None:
        """Execute the stage."""
        ...

    def validate_inputs(self, case_id: str) -> bool:
        """Check if required inputs are available."""
        ...

    def get_outputs(self, case_id: str) -> list[Path]:
        """Return paths to stage outputs."""
        ...
```

### Adding New Stages

To add Stage 3 (Event Timeline Builder):

```python
# lawsuit_parser/event_extraction/stages/stage_3_timeline.py

class Stage3Timeline(BaseStage):
    stage_number = 3
    stage_name = "timeline"

    def run(self, case_id: str, config: dict) -> None:
        # Load entities from Stage 2
        entities = self.load_stage_output(case_id, 2, "entities.json")

        # Build timeline
        timeline = self.build_timeline(entities)

        # Save output
        self.save_output(case_id, "events.json", timeline)
```

Register in `stages/__init__.py`:
```python
from .stage_1_metadata import Stage1Metadata
from .stage_2_gliner import Stage2GLiNER
from .stage_3_timeline import Stage3Timeline

STAGES = [
    Stage1Metadata,
    Stage2GLiNER,
    Stage3Timeline,
]
```

## Implementation Plan

1. **Core Infrastructure** (`base.py`, `models.py`, `config.py`, `utils.py`)
   - BaseStage abstract class
   - Pydantic models for all artifacts
   - Configuration management
   - File I/O utilities

2. **Stage 1: Metadata Extraction** (`stages/stage_1_metadata.py`)
   - Database query builder
   - PDF metadata extractor
   - Docling file parser
   - Date extraction with regex patterns
   - Actor discovery and normalization
   - GLiNER config generator

3. **Stage 2: GLiNER Detection** (`stages/stage_2_gliner.py`)
   - Integration with existing GLiNER runner
   - Batch processing
   - Span realignment
   - Entity linking to actors

4. **Pipeline Orchestrator** (`pipeline.py`)
   - Stage registry
   - Dependency validation
   - Execution order
   - Error handling and logging

## Configuration

`config/event_extraction.toml`:
```toml
[paths]
data_root = "data/cases"
events_dir = "events"

[stage_1]
extract_from_database = true
extract_from_pdfs = true
extract_from_docling = true
date_patterns = [
    "\\d{1,2}/\\d{1,2}/\\d{4}",
    "\\d{4}-\\d{2}-\\d{2}",
    "(?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},\\s+\\d{4}"
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
    "geographic location",
    "monetary amount",
    "document reference"
]
```

## Key Design Principles

1. **Immutability:** Once a stage completes, its outputs are immutable
2. **Idempotency:** Running a stage twice produces identical results
3. **Provenance:** All extracted data includes source references
4. **Extensibility:** New stages can be added without modifying existing ones
5. **Validation:** Each stage validates its inputs before execution
6. **Error Handling:** Failures are logged but don't prevent other stages from running