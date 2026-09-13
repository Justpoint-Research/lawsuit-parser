# Lawsuit Classification System

Multilabel classification of cases into `product_liability`, `personal_injury`, `class_action`
(config's `[lawsuit_classification].categories`). Two stages: an LLM generates labeled training
data, then a BERT model is fine-tuned on it for fast inference.

## Configuration

All settings live in `config/event_extraction.toml` under `[lawsuit_classification]`:

```toml
case_sources = ["ny_classification"]   # data/cases/<source>/ dirs to pull cases from
llm_providers = ["ollama"]             # providers queried when --providers isn't passed
llm_model = "qwen3:30b-a3b"
llm_base_url = "http://localhost:11434"
anthropic_model = "claude-sonnet-5"    # only called if named in --providers/llm_providers
gemini_model = "gemini-2.5-flash"      # via Vertex AI, ADC auth

classification_page_count = 3          # cap on any single doc's text (not a doc-count cap)
max_text_chars = 100000                # hard cap on total prompt text per case
min_confidence = 0.6

bert_model = "nlpaueb/legal-bert-base-uncased"
bert_max_length = 512
training_data_path = "data/classification/training_data.json"
reconciled_training_data_path = "data/classification/reconciled_training_data.json"
model_save_path = "data/classification/bert_classifier"
case_summaries_path = "data/classification/case_summaries.json"
```

`case_sources` is the scope for `zero_shot_classifier.py`/`summarize_cases_for_classification.py`
when no case IDs are given on the CLI - it globs **every** case directory under each listed
source, not a curated subset. Cloud providers (`anthropic`, `gemini`) cost money per call and are
only queried when named explicitly via `--providers` or `llm_providers`.

Prompts are in `config/llm_prompts.toml` under `[lawsuit_classification]` /
`[classification_summary]`.

## Building a scoped sample (recommended over classifying a whole source)

`case_sources` directories can hold tens of thousands of cases; classifying all of them wastes
LLM calls. `scripts/build_classification_sample.py` reads `data/cases/ny_after_search` (metadata
only, no DB/network needed) and picks a stratified sample - positive: product-liability/personal-
injury case types + caption class-action matches; negative: spread across other `case_type`
buckets - writing `data/classification_sample_ids.json` (`positive_ids`/`negative_ids`).

`scripts/export_classification_sample.py` then downloads (earliest N docs per case, per
`data/classification_sample_doc_selection.json`) and Docling-parses exactly that sample:

```bash
uv run python scripts/export_classification_sample.py --no-gpu --workers 8

# Already downloaded, e.g. from a bulk export - just (re-)parse:
uv run python scripts/export_classification_sample.py --skip-download --no-gpu --workers 8
```

It writes `data/classification_labels.json` (a simple binary label per case, from the sampling
heuristic - not an LLM judgment) and Docling output under `data/extraction/<source>/case_<id>/`.

**Gotcha:** once a sample is built, restrict every downstream step to its case IDs explicitly
(positional args, e.g. `case_<id> case_<id> ...`) rather than relying on `case_sources` scoping -
otherwise a run sweeps in every case under the source, not just the sample. A case classified
before its Docling parse finishes still gets a result (weak: metadata document-names only, no real
text) that counts as "done" and won't be redone by a later default run - only `--force` fixes it.

## Usage

### Step 1: Generate Training Data with LLM

```bash
# Classify specific cases (recommended - see Gotcha above); --limit defaults to 50, use 0 for no cap
uv run python scripts/zero_shot_classifier.py case_95 case_227 --providers ollama --limit 0

# Add a second provider's results to the same cases (kept side by side under provider_results)
uv run python scripts/zero_shot_classifier.py case_95 case_227 --providers anthropic

# Force re-classification (e.g. after fixing a weak-context case)
uv run python scripts/zero_shot_classifier.py case_95 --providers ollama --force
```

Every document in the case is used (not a capped sample), complaint-type filings ordered first so
truncation (`max_text_chars`) drops the least-central documents rather than the complaint.

**Output**: `data/classification/training_data.json`, one entry per case:

```json
{
  "results": [
    {
      "case_id": "case_95",
      "caption": "BONNIE DARLING v. LOREAL USA, INC. et al",
      "court": "New York County Supreme Court",
      "case_type": "Torts - Product Liability",
      "documents_used": ["document_....pdf"],
      "document_names": ["SUMMONS + COMPLAINT"],
      "provider_results": {
        "ollama": {
          "labels": [
            {"category": "product_liability", "confidence": 0.95, "reasoning": "..."}
          ],
          "model": "qwen3:30b-a3b",
          "classified_at": "2026-09-04T..."
        }
      }
    }
  ]
}
```

Each provider's result is stored side by side under `provider_results` so a case classified by
Ollama today can get Claude/Gemini results added later without losing the earlier ones.

### Step 1b: Reconcile Multi-Provider Labels

```bash
uv run python scripts/reconcile_classifications.py --require-all-providers
```

Writes `data/classification/reconciled.json` (majority/union/intersection breakdown for
inspection) and `data/classification/reconciled_training_data.json` - the file the BERT trainer
reads. Default strategy is **intersection**: a category is `1` only when every provider that ran
for the case assigned it; use `--training-strategy majority`/`union` for a looser rule. Cases with
no positive label are kept as negative examples.

### Step 1c: Generate Case Summaries (BERT input)

The raw `text_excerpt` in training data is only the first ~1000 characters of the filings - mostly
boilerplate. Generate a dense factual summary per case instead:

```bash
uv run python scripts/summarize_cases_for_classification.py
```

- Prompt (`[classification_summary]`) is **label-agnostic** - never names the categories or asks a
  yes/no question, so the summary can't leak the label.
- Measured against the **BERT tokenizer** on the exact string the trainer builds, guaranteed to
  fit `bert_max_length`; overflow triggers one re-prompt for a shorter summary, then hard
  truncation as a last resort.
- Defaults to just the cases present in `training_data.json` (i.e. run this *after* Step 1, not
  before) - pass `--all-cases` to summarize every case in `case_sources` instead.
- Output: `data/classification/case_summaries.json`. Resumable; `--force` regenerates.
  `reconcile_classifications.py` auto-merges it into `reconciled_training_data.json` as `summary`.

### Step 2: Train BERT Classifier

```bash
# Train on the LLM summaries (recommended once Step 1c has run)
uv run python scripts/train_bert_classifier.py --input-field summary

# 'auto' (default): uses the summary when present, else the raw excerpt
uv run python scripts/train_bert_classifier.py

uv run python scripts/train_bert_classifier.py \
  --data data/classification/reconciled_training_data.json \
  --model nlpaueb/legal-bert-base-uncased --epochs 5 --batch-size 16 --learning-rate 2e-5

uv run python scripts/train_bert_classifier.py --drop-empty   # drop cases with no positive label
uv run python scripts/train_bert_classifier.py --resume       # resume from checkpoint
uv run python scripts/train_bert_classifier.py --evaluate     # evaluate only, no training
```

Saves to `model_save_path` (`data/classification/bert_classifier/`); best checkpoint selected by
F1-macro on the validation split.

### Step 3: Classify New Cases

```bash
uv run python scripts/classify_lawsuit.py case_95 --use-bert
uv run python scripts/classify_lawsuit.py case_95 --use-llm
uv run python scripts/classify_lawsuit.py case_95 case_227 --use-bert
uv run python scripts/classify_lawsuit.py --all --use-bert --output results.json
```

## Document selection

Every PDF in `case_dir/documents/` is used - not a capped sample - so a label can't be missed
because the deciding allegation is in a later filing. Complaint-type documents (filename matches
`complaint`, `petition`, `summons`, `verified`, `amended_complaint`, `class_action_complaint`) are
ordered first purely so truncation, if it ever triggers, drops less-central documents first. Text
is loaded from a `.txt` sidecar if present, else the case's Docling `.docling.json`.

## Categories

- **product_liability** - defective/dangerous product claims (design/manufacturing defect,
  failure to warn, breach of warranty; medical devices, drugs, consumer products)
- **personal_injury** - physical/emotional harm claims (accidents, malpractice, wrongful death)
- **class_action** - "class action", "on behalf of all", "class certification", Fed. R. Civ. P.
  23 references, multiple similarly-situated plaintiffs

Multiple categories can apply to the same case.

## Extending

**New category**: add it to `config/event_extraction.toml`'s `categories`, extend the prompt
template in `config/llm_prompts.toml`, add it to `CLASSIFICATION_RESPONSE_SCHEMA`'s enum in
`scripts/zero_shot_classifier.py`, then re-run Steps 1-2.

**Different LLM/BERT model**: change `llm_model` (any locally-pulled Ollama tag) or `bert_model`
in config.

## Troubleshooting

- **"No documents/metadata found for case_X"** - needs `case_X/documents/*.pdf` and
  `case_X.json` (with `case_info`) on disk.
- **"BERT model not found"** - train first, or pass `--model-path`.
- **"Ollama connection failed"** - `ollama serve` running? `ollama pull qwen3:30b-a3b`? does
  `llm_base_url` match the port?
- **Low quality** - increase `classification_page_count` (per-document cap, not a document-count
  cap), generate more training data, or try a larger model.

## Files

**Scripts**: `build_classification_sample.py`, `export_classification_sample.py`,
`zero_shot_classifier.py`, `reconcile_classifications.py`,
`summarize_cases_for_classification.py`, `train_bert_classifier.py`, `classify_lawsuit.py`

**Config**: `config/event_extraction.toml` (`[lawsuit_classification]`), `config/llm_prompts.toml`

**Data**: `data/classification/training_data.json` (per-provider LLM labels),
`data/classification/reconciled_training_data.json` (BERT trainer input),
`data/classification/case_summaries.json`, `data/classification/bert_classifier/` (trained model),
`data/classification_sample_ids.json` / `data/classification_labels.json` (sample selection)
