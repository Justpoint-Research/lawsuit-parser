# Event Extraction Pipeline - Implementation Summary

## Overview

A complete, extensible event extraction pipeline has been implemented for extracting legal events and timelines from parsed Docling documents. The pipeline is designed to be modular and easily extensible with new stages.

## What Was Implemented

### Core Architecture

**Directory Structure:**
```
lawsuit_parser/event_extraction/
├── __init__.py              # Package exports
├── base.py                  # Base stage class and protocols
├── models.py                # Pydantic data models
├── config.py                # Configuration management
├── pipeline.py              # Pipeline orchestrator
├── utils.py                 # Utility functions
└── stages/
    ├── __init__.py          # Stage registry
    ├── stage_1_metadata.py  # Metadata extraction
    └── stage_2_gliner.py    # GLiNER entity detection
```

### Stage 1: Metadata Extraction

**Purpose:** Extract ground truth metadata from all available sources

**Features Implemented:**
- ✅ Database metadata extraction (PostgreSQL)
  - Case information (case number, court, filing date, status)
  - Party extraction (plaintiffs, defendants)
  - Graceful handling when database is unavailable
- ✅ PDF metadata extraction
  - Creation/modification dates
  - Author, title
  - Page count
- ✅ Docling file parsing
  - Document title from first heading
  - Page headers (CM/ECF information)
  - Case number, document number, filing date from headers
- ✅ Date extraction with configurable regex patterns
  - Multiple date formats supported (MM/DD/YYYY, YYYY-MM-DD, Month DD, YYYY)
  - Character offset tracking in canonical text
- ✅ Party discovery and normalization
  - Name normalization (corporate suffixes, case-folding)
  - Alias generation (last names, short forms, acronyms)
  - Role normalization (plaintiff, defendant, etc.)
- ✅ GLiNER configuration generation
  - Static labels for general entity types
  - Dynamic labels for discovered parties (e.g., "plaintiff (Jane Doe)")
  - Actor definitions with aliases and labels

**Outputs:**
- `files_scan.json` - Complete metadata scan
- `gliner_config.json` - GLiNER configuration with dynamic labels

### Stage 2: GLiNER Entity Detection

**Purpose:** Extract all entity mentions using zero-shot NER

**Features Implemented:**
- ✅ GLiNER model integration
  - Reuses existing `GlinerRunner` from extraction pipeline
  - GPU/CPU support
  - Configurable threshold and batch size
- ✅ Text segmentation
  - Intelligent paragraph-based segmentation
  - Overlapping segments for continuity
  - Offset tracking for accurate positioning
- ✅ Entity extraction
  - Both static and dynamic labels
  - Batch processing for efficiency
  - Confidence scoring
- ✅ Span realignment
  - Validates extracted spans against canonical text
  - Searches for correct positions when misaligned
  - Provenance tracking with character offsets
- ✅ Entity linking
  - Links entities to known actors from Stage 1
  - Matches against canonical names and aliases
  - Handles dynamic actor labels
- ✅ Context extraction
  - 100 characters before/after each entity
  - Highlights entity in context

**Outputs:**
- `entities.json` - All detected entities with locations, scores, and context

### Pipeline Orchestrator

**Features Implemented:**
- ✅ Stage registration system
  - Automatic discovery of stages via registry
  - Stage number and name tracking
- ✅ Dependency validation
  - Checks inputs exist before running
  - Validates stage outputs
- ✅ Flexible execution
  - Run all stages
  - Run specific stages
  - Force re-run option
- ✅ Status reporting
  - Shows which stages are complete
  - Shows which stages can run
  - Lists all outputs and their existence
- ✅ Configuration management
  - Loads from TOML file
  - Stage-specific configurations
  - Override options

### Configuration System

**Features Implemented:**
- ✅ Pydantic-based configuration models
- ✅ TOML file support
- ✅ Default values with fallbacks
- ✅ Stage-specific configuration sections
- ✅ Path configuration
- ✅ Model configuration (GLiNER, threshold, batch size)
- ✅ Label configuration (static and dynamic)
- ✅ Feature toggles (database, PDF, Docling extraction)

### Utilities

**Features Implemented:**
- ✅ Date extraction with multiple patterns
- ✅ CM/ECF header parsing
- ✅ PDF metadata extraction (with PyPDF2)
- ✅ Party name normalization
- ✅ Alias generation
- ✅ Role normalization
- ✅ Error handling and graceful degradation

### CLI Script

**Features Implemented:**
- ✅ `run_event_extraction.py` script
  - Run all stages or specific stages
  - Force re-run option
  - Status checking
  - Custom config support
  - Custom data root support
  - Help documentation
  - Example usage

### Documentation

**Files Created:**
- ✅ `docs/event_extraction_usage.md` - Complete usage guide
- ✅ `docs/event_extraction_pipeline_design.md` - Architecture and design document
- ✅ `docs/event_extraction_implementation.md` - This implementation summary
- ✅ `config/event_extraction.toml` - Configuration file with comments
- ✅ Updated main `README.md` with event extraction section

## File Manifest

### Core Files (9 files)
1. `lawsuit_parser/event_extraction/__init__.py`
2. `lawsuit_parser/event_extraction/base.py`
3. `lawsuit_parser/event_extraction/models.py`
4. `lawsuit_parser/event_extraction/config.py`
5. `lawsuit_parser/event_extraction/pipeline.py`
6. `lawsuit_parser/event_extraction/utils.py`
7. `lawsuit_parser/event_extraction/stages/__init__.py`
8. `lawsuit_parser/event_extraction/stages/stage_1_metadata.py`
9. `lawsuit_parser/event_extraction/stages/stage_2_gliner.py`

### Configuration (1 file)
10. `config/event_extraction.toml`

### Scripts (1 file)
11. `scripts/run_event_extraction.py`

### Documentation (3 files)
12. `docs/event_extraction_usage.md` (usage guide)
13. `docs/event_extraction_pipeline_design.md` (architecture)
14. `docs/event_extraction_implementation.md` (this file)

**Total: 14 new files**

## How to Use

### Basic Usage

```bash
# Run the complete pipeline for a case
python scripts/run_event_extraction.py case_67

# Check status before running
python scripts/run_event_extraction.py case_67 --status

# Run only Stage 1 (metadata extraction)
python scripts/run_event_extraction.py case_67 --stages 1

# Force re-run all stages
python scripts/run_event_extraction.py case_67 --force
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

# Get detailed status
status = pipeline.get_stage_status("case_67")
```

### Reading Outputs

```python
import json
from pathlib import Path

case_id = "case_67"
events_dir = Path(f"data/cases/{case_id}/events")

# Load files_scan.json
with open(events_dir / "files_scan.json") as f:
    files_scan = json.load(f)
    print(f"Found {len(files_scan['parties_discovered'])} parties")
    print(f"Found {len(files_scan['all_dates'])} dates")

# Load gliner_config.json
with open(events_dir / "gliner_config.json") as f:
    gliner_config = json.load(f)
    print(f"Static labels: {gliner_config['labels']['static']}")
    print(f"Dynamic labels: {gliner_config['labels']['dynamic']}")

# Load entities.json
with open(events_dir / "entities.json") as f:
    entities = json.load(f)
    print(f"Found {len(entities['entities'])} entities")
    print(f"Entity breakdown: {entities['entity_counts']}")
```

## Extensibility

### Adding a New Stage

To add Stage 3 (Timeline Builder), follow these steps:

1. **Create the stage class:**
```python
# lawsuit_parser/event_extraction/stages/stage_3_timeline.py

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
        self.save_artifact(case_id, "timeline.json", timeline)

    def validate_inputs(self, case_id: str) -> bool:
        return self.artifact_exists(case_id, "entities.json")

    def get_outputs(self, case_id: str) -> list[Path]:
        return [self.get_events_dir(case_id) / "timeline.json"]
```

2. **Register the stage:**
```python
# lawsuit_parser/event_extraction/stages/__init__.py

from .stage_3_timeline import Stage3Timeline

STAGES = [
    Stage1Metadata,
    Stage2GLiNER,
    Stage3Timeline,  # Add new stage
]
```

3. **Add configuration (optional):**
```toml
# config/event_extraction.toml

[stage_3]
# Stage 3 specific configuration
```

That's it! The pipeline will automatically discover and execute the new stage.

## Design Principles

1. **Modularity** - Each stage is independent and self-contained
2. **Extensibility** - New stages can be added without modifying existing code
3. **Immutability** - Stage outputs are immutable once created
4. **Idempotency** - Running a stage twice produces identical results
5. **Provenance** - All extracted data includes source references and character offsets
6. **Validation** - Each stage validates inputs before execution
7. **Error Handling** - Graceful degradation when optional features unavailable
8. **Configuration** - All tunables externalized to config file

## Testing

To test the pipeline, you'll need:

1. **Case data** in `data/cases/<case_id>/`
   - PDF files
   - Docling parsed files (`.docling.json`)
   - Optional: canonical text in `documents/`

2. **Database** (optional)
   - PostgreSQL connection configured
   - Case and party tables populated

3. **GLiNER model**
   - Will auto-download on first use
   - Requires internet connection

Run the pipeline on a test case:
```bash
python scripts/run_event_extraction.py test_case_001 --stages 1 2
```

## Next Steps

Potential future enhancements:

1. **Stage 3: Event Extraction**
   - Extract legal events from entity combinations
   - Link events to temporal expressions
   - Classify event types (filing, motion, hearing, ruling, etc.)

2. **Stage 4: Timeline Construction**
   - Order events chronologically
   - Resolve relative dates
   - Build causal chains

3. **Stage 5: Relationship Extraction**
   - Extract relationships between parties
   - Link events to parties
   - Build event graphs

4. **Improvements:**
   - Better date parsing and normalization
   - Enhanced entity linking
   - Coreference resolution
   - Event deduplication
   - Confidence calibration

## Summary

The event extraction pipeline is fully implemented and ready to use. It provides:

- ✅ Modular, extensible architecture
- ✅ Two complete stages (metadata + entities)
- ✅ Comprehensive configuration system
- ✅ CLI and Python API
- ✅ Full documentation
- ✅ Integration with existing codebase
- ✅ Error handling and graceful degradation

The pipeline can process legal case documents to extract parties, dates, and entities with full provenance tracking, laying the foundation for event timeline construction and analysis.
