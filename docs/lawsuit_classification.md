# Lawsuit Classification System

A two-stage classification system for categorizing lawsuits into multiple labels:
- **Product Liability**: Defective or dangerous product claims
- **Personal Injury**: Physical or emotional harm claims
- **Class Action**: Lawsuits on behalf of a larger group

## Architecture

### Stage 1: Zero-Shot LLM Classifier (Training Data Generation)
Uses Qwen/Ollama to analyze case documents and metadata, generating labeled training data.

**Input**:
- Case metadata (caption, court, case type, document names)
- Document text (first N pages of initial filings)

**Output**:
- JSON file with multilabel classifications
- Confidence scores and reasoning for each label

### Stage 2: BERT-Based Classifier (Fast Inference)
Trains a Legal-BERT model on LLM-generated training data for fast classification.

**Advantages**:
- 10-100x faster than LLM approach
- Consistent latency
- No API dependencies
- Can run on CPU or GPU

## Configuration

All settings are in `config/event_extraction.toml`:

```toml
[lawsuit_classification]
# LLM settings for zero-shot classification
llm_backend = "ollama"
llm_model = "qwen3:30b-a3b"
llm_base_url = "http://localhost:11434"

# Document selection
classification_page_count = 3      # Pages per document
classification_doc_count = 2       # Documents per case

# Categories
categories = [
    "product_liability",
    "personal_injury",
    "class_action"
]

# Confidence threshold
min_confidence = 0.6

# BERT model settings
bert_model = "nlpaueb/legal-bert-base-uncased"
bert_max_length = 512
bert_batch_size = 8

# Paths
training_data_path = "data/classification/training_data.json"
model_save_path = "data/classification/bert_classifier"
```

Prompts are in `config/llm_prompts.toml` under `[lawsuit_classification]`.

## Usage

### Step 1: Generate Training Data with LLM

Classify cases using Qwen/Ollama to generate labeled training data:

```bash
# Classify all cases
python scripts/zero_shot_classifier.py

# Classify specific cases
python scripts/zero_shot_classifier.py case_95 case_227

# Force re-classification
python scripts/zero_shot_classifier.py --force

# Custom output path
python scripts/zero_shot_classifier.py --output data/my_training_data.json
```

**Output**: `data/classification/training_data.json`

Example output structure:
```json
{
  "metadata": {
    "created_at": "2026-09-04T...",
    "model": "qwen3:30b-a3b",
    "total_cases": 100,
    "categories": ["product_liability", "personal_injury", "class_action"]
  },
  "results": [
    {
      "case_id": "case_95",
      "caption": "BONNIE DARLING v. LOREAL USA, INC. et al",
      "court": "New York County Supreme Court",
      "case_type": "Torts - Product Liability",
      "labels": [
        {
          "category": "product_liability",
          "confidence": 0.95,
          "reasoning": "Plaintiff alleges hair relaxer products caused harm..."
        },
        {
          "category": "personal_injury",
          "confidence": 0.88,
          "reasoning": "Claims include physical injury and medical expenses..."
        }
      ],
      "classified_at": "2026-09-04T...",
      "documents_used": ["document_..._complaint.pdf"]
    }
  ]
}
```

### Step 2: Train BERT Classifier

Train a fast BERT model using the LLM-generated training data:

```bash
# Train with default settings
python scripts/train_bert_classifier.py

# Custom training
python scripts/train_bert_classifier.py \
  --data data/classification/training_data.json \
  --model nlpaueb/legal-bert-base-uncased \
  --epochs 5 \
  --batch-size 16 \
  --learning-rate 2e-5

# Resume from checkpoint
python scripts/train_bert_classifier.py --resume

# Evaluate only (no training)
python scripts/train_bert_classifier.py --evaluate
```

**Training outputs**:
- Trained model saved to `data/classification/bert_classifier/`
- Includes: pytorch_model.bin, config.json, tokenizer files
- Best model selected by F1-macro score on validation set

**Expected performance** (varies by training data quality and size):
- F1-macro: 0.75-0.90
- Per-category F1: 0.70-0.95
- Inference speed: ~10-50 cases/second (GPU), ~2-5 cases/second (CPU)

### Step 3: Classify New Cases

Use either BERT (fast) or LLM (flexible) for inference:

```bash
# Classify with BERT (fast, requires trained model)
python scripts/classify_lawsuit.py case_95 --use-bert

# Classify with LLM (slower, no training needed)
python scripts/classify_lawsuit.py case_95 --use-llm

# Classify multiple cases
python scripts/classify_lawsuit.py case_95 case_227 case_309 --use-bert

# Classify all cases
python scripts/classify_lawsuit.py --all --use-bert

# Save results to file
python scripts/classify_lawsuit.py --all --use-bert --output results.json
```

## Data Flow

```
┌─────────────────────────────────────────────────────┐
│ Input: Case Documents + Metadata                    │
│ - documents/*.pdf (parsed with Docling)            │
│ - case_*.json (database metadata)                   │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │ Stage 1: Zero-Shot LLM           │
    │ (scripts/zero_shot_classifier.py)│
    │                                   │
    │ - Load case metadata & documents │
    │ - Build prompt with context      │
    │ - Call Qwen/Ollama for labels    │
    │ - Save training data             │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │ Training Data                    │
    │ (training_data.json)             │
    │ - Case texts                     │
    │ - Multilabel annotations         │
    │ - Confidence scores              │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │ Stage 2: BERT Training           │
    │ (scripts/train_bert_classifier.py)│
    │                                   │
    │ - Load & split training data     │
    │ - Fine-tune Legal-BERT           │
    │ - Validate & save best model     │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │ Trained Model                    │
    │ (bert_classifier/)               │
    │ - pytorch_model.bin              │
    │ - config.json                    │
    │ - tokenizer files                │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │ Inference                        │
    │ (scripts/classify_lawsuit.py)    │
    │                                   │
    │ - Fast BERT classification       │
    │ - OR LLM classification          │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │ Output: Classifications          │
    │ - Category labels                │
    │ - Confidence scores              │
    └──────────────────────────────────┘
```

## Input Data Structure

The classifier expects cases organized as:

```
data/cases/
├── case_95/
│   ├── case_95.json              # Metadata from database
│   ├── documents/                # PDF documents
│   │   ├── document_XXX.pdf
│   │   └── document_XXX.txt      # Extracted text (optional)
│   └── docling/                  # Docling parsed output (optional)
│       └── documents/
│           └── document_XXX.docling.json
├── case_227/
│   └── ...
└── case_309/
    └── ...
```

**case_*.json structure**:
```json
{
  "case_info": {
    "case_id": "152401/2026",
    "caption": "PLAINTIFF v. DEFENDANT",
    "court": "New York County Supreme Court",
    "case_type": "Torts - Product Liability",
    "case_received_date": "02/26/2026",
    "case_status": "Active"
  },
  "documents": [
    {
      "document_name": "SUMMONS + COMPLAINT",
      "filed_by": "ATTORNEY NAME",
      "filed_create": "02/26/2026",
      "local_document_path": "documents/document_XXX.pdf"
    }
  ]
}
```

## Document Selection

The classifier intelligently selects documents for analysis:

1. **Priority documents** (matched by filename keywords):
   - complaint, petition, summons, verified
   - amended_complaint, class_action_complaint

2. **Fallback**: First N documents (sorted by filing date)

3. **Text extraction priority**:
   - `.txt` files (canonical text)
   - `.docling.json` files (Docling output)
   - Raw PDF (not implemented)

## Categories

### Product Liability
**Criteria**: Claims about defective or dangerous products causing harm
- Design defects
- Manufacturing defects
- Failure to warn
- Breach of warranty
- Medical devices, drugs, consumer products

### Personal Injury
**Criteria**: Claims for physical or emotional harm
- Car accidents, slip and fall
- Medical malpractice
- Assault, wrongful death
- Bodily injury, pain and suffering
- Medical expenses, loss of consortium

### Class Action
**Criteria**: Lawsuit on behalf of a larger group
- Phrases: "class action", "on behalf of all", "class members"
- "class certification", "numerosity"
- References to Fed. R. Civ. P. 23
- Multiple similarly situated plaintiffs

**Note**: Multiple categories can apply to the same case. For example, a product liability case can also be a class action and involve personal injury claims.

## Performance Considerations

### LLM Classification (Zero-Shot)
- **Speed**: ~10-30 seconds per case (depends on document length)
- **Cost**: Local (free with Ollama), or API costs if using cloud LLM
- **Quality**: High, can understand nuanced legal language
- **Use case**: Generating training data, handling edge cases

### BERT Classification
- **Speed**: ~0.02-0.5 seconds per case (GPU), ~0.2-2 seconds (CPU)
- **Cost**: One-time training cost, then free
- **Quality**: 75-90% accuracy (depends on training data)
- **Use case**: Batch processing, production inference

## Extending the System

### Adding New Categories

1. Update config (`config/event_extraction.toml`):
```toml
categories = [
    "product_liability",
    "personal_injury",
    "class_action",
    "employment_discrimination",  # New category
]
```

2. Update prompt template (`config/llm_prompts.toml`):
```toml
[lawsuit_classification]
template = """
...
4. EMPLOYMENT_DISCRIMINATION: Claims of workplace discrimination...
...
"""
```

3. Update response schema in `zero_shot_classifier.py`:
```python
CLASSIFICATION_RESPONSE_SCHEMA = {
    "properties": {
        "labels": {
            "items": {
                "properties": {
                    "category": {
                        "enum": ["product_liability", "personal_injury",
                                "class_action", "employment_discrimination"]
                    }
                }
            }
        }
    }
}
```

4. Re-generate training data and re-train model

### Using a Different LLM Backend

The system supports any Ollama model:

```toml
llm_model = "llama3:70b"           # Larger model
llm_model = "mistral:7b"           # Smaller/faster model
llm_model = "qwen2.5:14b"          # Alternative model
```

Or use a different backend by implementing the call interface.

### Using a Different BERT Model

```toml
bert_model = "bert-base-uncased"              # General BERT
bert_model = "nlpaueb/legal-bert-base-uncased"  # Legal domain
bert_model = "nlpaueb/legal-bert-small-uncased" # Faster/smaller
bert_model = "saibo/legal-roberta-base"       # RoBERTa variant
```

## Troubleshooting

### "No documents found for case_X"
- Ensure case directory has `documents/` subdirectory with PDFs
- Check that documents are named with `.pdf` extension

### "No metadata found for case_X"
- Ensure case directory has `case_X.json` file
- Check JSON file is valid and has `case_info` section

### "BERT model not found"
- Train the model first: `python scripts/train_bert_classifier.py`
- Or specify custom path: `--model-path /path/to/model`

### "Ollama connection failed"
- Ensure Ollama is running: `ollama serve`
- Check model is pulled: `ollama pull qwen3:30b-a3b`
- Verify base_url in config matches Ollama port

### Low classification quality
- Increase `classification_page_count` to analyze more content
- Increase `classification_doc_count` to read more documents
- Generate more training data for BERT model
- Try a larger LLM model (e.g., qwen3:30b instead of 7b)

## Files

**Scripts**:
- `scripts/zero_shot_classifier.py` - Generate training data with LLM
- `scripts/train_bert_classifier.py` - Train BERT classifier
- `scripts/classify_lawsuit.py` - Classify cases (BERT or LLM)

**Config**:
- `config/event_extraction.toml` - Classification settings
- `config/llm_prompts.toml` - Prompt templates

**Data**:
- `data/classification/training_data.json` - LLM-generated labels
- `data/classification/bert_classifier/` - Trained BERT model

**Documentation**:
- `docs/lawsuit_classification.md` - This file
