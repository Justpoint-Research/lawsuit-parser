# Event Extraction Pipeline

A modular, stage-based pipeline that extracts legal events and timelines from parsed Docling
documents. For what each stage produces and which tools/regexes/models do the work, see the
[Pipeline Outputs Reference](pipeline_outputs.md) - this doc covers installation, config, and how
to run it.

## Stages

1. **Metadata** - actor roster, document catalog, dates (DB + PDF metadata + Docling + regex + LLM)
2. **GLiNER Entity Detection** - zero-shot NER over canonical text using Stage 1's dynamic labels
3. **Document Summary** - 1-3 sentence LLM summary per document
4. **Date Clustering** - parses/groups dates by paragraph, links co-occurring actors
5. **Event Synthesis** - turns date clusters into timeline events (deterministic quote-extraction
   by default; `use_llm = true` for LLM-synthesized type/description/outcome/curated actors)
6. **Relationship Extraction** - regex-based lawyer-client representation links

## Installation

Part of the `lawsuit-parser` package (`uv pip install -e .`); no separate setup.

## Configuration

`config/event_extraction.toml`, one `[stage_N]` section per stage:

```toml
[paths]
data_root = "data/cases"          # source: documents/, confirmations/, docling/
output_root = "data/extraction"   # generated artifacts, safe to wipe independently
events_dir = "events"

[stage_1]
llm_backend = "ollama"            # or "nuextract"
llm_model = "qwen3:30b-a3b"
llm_base_url = "http://localhost:11434"
validate_actors_with_llm = true
date_patterns = ["\\d{1,2}/\\d{1,2}/\\d{4}", "\\d{4}-\\d{2}-\\d{2}", "..."]

[stage_2]
model = "urchade/gliner_multi-v2.1"
threshold = 0.5
batch_size = 8
use_gpu = true
```

## Usage

### CLI

```bash
uv run python scripts/run_event_extraction.py case_67              # run all stages
uv run python scripts/run_event_extraction.py case_67 --stages 1 2 # specific stages
uv run python scripts/run_event_extraction.py case_67 --status     # show what's done/pending
uv run python scripts/run_event_extraction.py case_67 --force      # overwrite existing outputs
uv run python scripts/run_event_extraction.py case_67 --config my_config.toml
```

### Python API

```python
from lawsuit_parser.event_extraction import EventExtractionPipeline

pipeline = EventExtractionPipeline()
pipeline.run_all_stages("case_67")
pipeline.run_stages("case_67", stages=[1, 2])
pipeline.print_status("case_67")
status = pipeline.get_stage_status("case_67")
```

## Data Directory Structure

Source data and generated outputs live under separate roots so a run's artifacts can be wiped and
regenerated without touching source data - see [Pipeline Outputs Reference](pipeline_outputs.md)
for the full layout under both `data/cases/<case_id>/` and `data/extraction/<case_id>/events/`.

## Extending: Adding a New Stage

```python
# lawsuit_parser/event_extraction/stages/stage_7_whatever.py
from ..base import BaseStage
from ..models import SomeArtifact

class Stage7Whatever(BaseStage):
    stage_number = 7
    stage_name = "whatever"

    def run(self, case_id: str, config: dict) -> None:
        relations = self.load_artifact(case_id, "relations.json", RelationsArtifact)
        result = ...  # build the new artifact
        self.save_artifact(case_id, "whatever.json", result)

    def validate_inputs(self, case_id: str) -> bool:
        return self.artifact_exists(case_id, "relations.json")

    def get_outputs(self, case_id: str) -> list[Path]:
        return [self.get_events_dir(case_id) / "whatever.json"]
```

Register it in `stages/__init__.py`'s `STAGES` list and add an optional `[stage_7]` config
section - the pipeline orchestrator discovers and runs it automatically.

## Design Principles

Stage outputs are immutable once written and idempotent (re-running produces the same result);
every extracted field carries source references/character offsets for provenance; each stage
validates its inputs before running; failures are logged, not fatal to other stages.

## Troubleshooting

- **Case directory not found** - needs `data/cases/<case_id>/` with at least some PDF or parsed
  files.
- **Database connection errors** - Stage 1 skips DB extraction gracefully; fine if you're only
  using file-based metadata (caption/confirmation parsing still runs).
- **GLiNER GPU errors** - set `use_gpu = false` in `[stage_2]`, or check CUDA install.
- **LLM stages (validate_actors_with_llm, Stage 3, Stage 5 with use_llm=true)** - need Ollama
  running (`ollama serve`) with the configured model pulled, or a reachable NuExtract server.
