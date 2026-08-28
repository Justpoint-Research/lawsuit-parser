# Event Extraction Pipeline Outputs

Complete reference for what each pipeline stage produces and which tools are used for extraction.

## Pipeline Overview

The event extraction pipeline has 5 stages that process legal documents to extract structured event data:

```
Database + PDFs → Stage 1 → Stage 2 → Stage 3 → Stage 4 → Stage 5 → Events Timeline
                     ↓         ↓         ↓         ↓         ↓
                  Metadata  Entities  Summaries  Dates    Events
```

All outputs are JSON files in `data/extraction/<case_id>/events/`

---

## Stage 1: Metadata Extraction

**Purpose:** Extract ground-truth metadata from all available sources to build actor roster and document catalog

### Output Files

#### 1. `files_scan.json`
**Model:** `FilesScan`

**Contains:**
- `case_id` - Case identifier
- `scan_timestamp` - When this scan was performed
- `database_metadata` - Case info from PostgreSQL (case number, court, status, filed date)
- `documents[]` - Array of document metadata:
  - `doc_id` - Sequential ID (doc_000, doc_001, ...)
  - `file_name` - Original PDF filename
  - `document_number` - Filing number from CM/ECF or NYSCEF
  - `document_title` - Identified title (LLM-determined)
  - `filing_date` - When filed (from header or confirmation)
  - `filed_by` - Filer name (from confirmation)
  - `pdf_metadata` - Creation/modification dates, page count
  - `docling_metadata` - Title, header, CM/ECF data, signature
  - `confirmation_metadata` - Judge, clerk, timestamp from e-filing notice
  - `extracted_dates[]` - All dates found in this doc with char offsets
  - `referenced_documents[]` - Other docs this one cites (by doc number)
  - `referenced_by[]` - Other doc_ids that cite this one
- `all_dates[]` - Flattened list of all dates from all documents

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| `database_metadata` | **PostgreSQL query** | Direct SQL query to `public.court_cases` table (port 5433) |
| `pdf_metadata.created` | **pypdfium2** | PDF file property extraction (`extract_pdf_metadata()`) |
| `pdf_metadata.modified` | **pypdfium2** | PDF file property extraction |
| `pdf_metadata.pages` | **pypdfium2** | PDF file property extraction |
| `docling_metadata.title` | **Docling parser** | First "title" labeled item on page 1 |
| `docling_metadata.header` | **Docling parser** | All "page_header" items on page 1, joined |
| `docling_metadata.cm_ecf` | **Regex** | Pattern: `Case ([^\s]+) Document (\d+) Filed ([\d/]+)` |
| `docling_metadata.document_signature` | **Regex** | Pattern: `NYSCEF DOC. NO. (\d+)` (`extract_document_signature()`) |
| `document_title` | **LLM** | Ollama or NuExtract reads page 1 text + candidates + Docling title (`identify_document_title_with_llm()`) |
| `filing_date` (from CM/ECF) | **Regex** | Extracted from CM/ECF header pattern |
| `filing_date` (from NYSCEF) | **Regex** | Pattern: `FILED: .+?COUNTY CLERK (\d{1,2}/\d{1,2}/\d{4})` |
| `confirmation_metadata.notice_timestamp` | **Regex** | Pattern: `received an electronic filing on (\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M)` |
| `confirmation_metadata.assigned_judge` | **Regex** | Pattern: `Assigned Judge:\s*(.+)` |
| `confirmation_metadata.filer_name` | **Regex** | Pattern: `^([A-Z][A-Za-z.,'\-\s]*?)\s*\|\s*(email)\s*\|\s*(phone)` |
| `confirmation_metadata.court_clerk` | **Regex** | Pattern: `^(?:Hon\.\s+)?([A-Z][A-Za-z.'\-\s]+?),\s*[\w\s]*?County Clerk` |
| `extracted_dates[]` | **Regex** | Multiple patterns from config (MM/DD/YYYY, YYYY-MM-DD, "Month DD, YYYY") (`extract_dates_from_text()`) |
| `referenced_documents[]` | **Regex** | Patterns: `Doc(?:\.|ument)?\s*(?:No\.?|#)?\s*(\d+)` and `NYSCEF (?:Doc\.? )?(?:No\.? )?(\d+)` (`find_document_references()`) |
| `referenced_by[]` | **Cross-reference resolution** | Built by reversing `referenced_documents` after all doc numbers known |

#### 2. `actors.json`
**Model:** `ActorsArtifact`

**Contains:**
- `case_id` - Case identifier
- `actors[]` - Array of discovered parties/roles:
  - `canonical_name` - Primary name as discovered
  - `role` - One of: plaintiff, defendant, judge, attorney, witness, court_clerk, counsel
  - `is_named` - True if actual person/entity, False if generic placeholder
  - `source` - Where discovered: database, caption, confirmation, llm, generic
  - `aliases[]` - Alternate names/forms
  - `doc_ids[]` - Documents where this actor appears
  - `gliner_label` - GLiNER label format (e.g., "plaintiff (Jane Doe)")

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| Plaintiffs (from DB) | **PostgreSQL query** | Parsed from database caption (not a dedicated column) |
| Defendants (from DB) | **PostgreSQL query** | Parsed from database caption (not a dedicated column) |
| Plaintiffs (from caption) | **Regex + heuristics** | `parse_caption_block()` - finds role markers, separators, filters boilerplate |
| Defendants (from caption) | **Regex + heuristics** | Same as above - searches for "Plaintiff," "against," "Defendant," pattern |
| Judge | **Regex** | From confirmation: `Assigned Judge:\s*(.+)` |
| Filer/Counsel | **Regex** | From confirmation: name\|email\|phone pattern |
| Court Clerk | **Regex** | From confirmation: `Hon. <Name>, <County> County Clerk` pattern |
| Actor aliases | **String manipulation** | `find_party_aliases()` - generates common forms (last name only, initials, etc.) |
| Actor normalization | **String processing** | `normalize_party_name()` - strips Inc., LLC, et al., punctuation for deduplication |
| Actor validation | **LLM** | Ollama or NuExtract validates roster, corrects roles, drops junk (`validate_actors_with_llm()`) |
| GLiNER label text | **LLM** | Part of validation - LLM rephrases names for GLiNER (e.g., "John Smith" → "plaintiff (John Smith)") |
| Generic placeholders | **Hardcoded** | For missing roles: "Witness", "Attorney", "Judge", "Court Clerk" |

**Caption Parsing Algorithm:**
1. Find role markers: `^(plaintiff|defendant|petitioner|respondent)s?$` (case-insensitive)
2. Find separator: `^-?\s*(against|vs?\.)\s*-?`
3. Extract names between boilerplate-filtered lines
4. Filter out: court headers, addresses (ZIP codes, street numbers), docket stamps
5. Split semicolon/and-joined co-parties
6. Strip "et al." and trailing punctuation

#### 3. `products.json`
**Model:** `ActorsArtifact`

**Contains:**
- `case_id` - Case identifier
- `actors[]` - Accused products (same schema as actors.json):
  - `canonical_name` - Product name (e.g., "Depo-Provera")
  - `role` - One of: medical_substance, drug, medical_device, cosmetic_product
  - `attributed_to[]` - Defendant names blamed for this product
  - `aliases[]` - Alternate product names
  - `doc_ids[]` - Documents where product was identified

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| Product identification | **LLM** | Ollama or NuExtract reads case context and identifies accused product (`identify_products_with_llm()`) |
| Product type classification | **LLM** | Model classifies as medical_substance, drug, medical_device, or cosmetic_product |
| Defendant attribution | **LLM** | Model links product to defendant(s) from actor roster |
| Product aliases | **LLM** | Model generates common alternate names |
| Litigation caption hints | **Regex** | Pattern: `In Re (.+?) (?:Litigation|Products? Liability|MDL)` (`find_litigation_captions()`) |
| Document selection for context | **Keyword scoring** | Scores docs by density of: "drug", "device", "defect", "injury", "warning", "label", "adverse", "fda", etc. |

**LLM Context for Product Identification:**
- Case caption
- Court name
- Case type
- Litigation caption candidates (e.g., "Depo-Provera" from "In Re Depo-Provera Litigation")
- Defendant names from actor roster
- 4000-char excerpt from highest-scoring document (centered on keyword hits)

#### 4. `gliner_config.json`
**Model:** `GLiNERConfig`

**Contains:**
- `model` - GLiNER model name (e.g., "urchade/gliner_multi-v2.1")
- `threshold` - Confidence threshold (0.5 default)
- `batch_size` - Batch size for processing
- `labels` - Label configuration:
  - `static[]` - Universal labels: "temporal expression", "legal action or event", "court", "geographic location", "monetary amount", "document reference"
  - `dynamic[]` - Actor-specific labels: "plaintiff (Jane Doe)", "defendant (ACME Inc.)", "medical substance (Depo-Provera)"
- `actors[]` - Full actor roster with GLiNER labels

**Generation Tools:**

| Element | Tool | Details |
|---------|------|---------|
| Static labels | **Config file** | Loaded from `config/event_extraction.toml` `[stage_2].static_labels` |
| Dynamic labels | **Template + roster** | Formatted as `{role} ({name})` for each named actor/product |
| Label deduplication | **Set tracking** | Ensures no duplicate labels (handles LLM-cleaned variants) |
| Actor GLiNER labels | **LLM or template** | LLM-phrased during validation, or auto-generated from role+name |

---

## Stage 2: GLiNER Entity Detection

**Purpose:** Detect all entity mentions (people, locations, dates, amounts, products) using zero-shot NER

### Output Files

#### 1. `entities.json`
**Model:** `EntitiesArtifact`

**Contains:**
- `case_id` - Case identifier
- `extraction_timestamp` - When entities were extracted
- `gliner_config` - Model and threshold used
- `entities[]` - All detected entities:
  - `entity_id` - Unique ID (ent_001, ent_002, ...)
  - `text` - Matched text span
  - `label` - GLiNER label (static or dynamic)
  - `score` - Confidence score (0-1)
  - `doc_id` - Source document
  - `char_start`, `char_end` - Character offsets in canonical text
  - `linked_actor` - Canonical actor name if linked (or null)
  - `context` - ±2 sentences around entity
  - `detection_method` - "gliner" or "gazetteer"
- `entity_counts` - Count by label

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| Entity detection | **GLiNER model** | Zero-shot NER using `urchade/gliner_multi-v2.1` |
| Text segmentation | **NLTK** | Sentence tokenization with overlapping context (±2 sentences) |
| Batch processing | **GLiNER** | Processes 8 chunks at a time (GPU-accelerated if available) |
| Span realignment | **Character offset mapping** | Maps GLiNER's segment-relative offsets to canonical text positions |
| Actor linking | **Fuzzy string matching** | `token_set_ratio >= 85` (RapidFuzz) to match entities to known actors |
| Gazetteer recall pass | **Regex** | Exact string search for actor canonical names + aliases (backstop for GLiNER misses) |
| Context extraction | **NLTK** | Extracts ±2 sentences around entity for human review |

---

## Stage 3: Document Summary

**Purpose:** Generate 1-3 sentence summaries of each document's core purpose

### Output Files

#### 1. `summaries.json`
**Model:** `SummariesArtifact`

**Contains:**
- `case_id` - Case identifier
- `extraction_timestamp` - When summaries were generated
- `documents[]` - Per-document summaries:
  - `doc_id` - Document identifier
  - `file_name` - PDF filename
  - `summary` - 1-3 sentence summary
  - `model` - LLM model used (e.g., "qwen3:30b-a3b")

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| Document summary | **LLM** | Ollama or NuExtract reads first 8000 chars + identified title (`summarize_document_with_llm()`) |
| Text loading | **File I/O** | Reads canonical document text from `<case_id>/<doc_id>.txt` |
| Text truncation | **String slicing** | Limits to 8000 chars to fit LLM context window |

---

## Stage 4: Date Clustering

**Purpose:** Parse dates, group by paragraph, and associate with relevant actors

### Output Files

#### 1. `dates.json`
**Model:** `DatesArtifact`

**Contains:**
- `case_id` - Case identifier
- `extraction_timestamp` - When dates were clustered
- `clusters[]` - Date clusters:
  - `cluster_id` - Unique ID (cluster_001, cluster_002, ...)
  - `doc_id` - Source document
  - `char_start`, `char_end` - Paragraph span in canonical text
  - `citation` - Paragraph text with dates bolded
  - `dates[]` - Dates in this cluster:
    - `date_id` - Unique ID
    - `text` - Raw date string (e.g., "January 15, 2024")
    - `parsed_date` - ISO 8601 datetime
    - `date_type` - event_date, filing_date, creation_date, etc.
    - `source` - document_body, cm_ecf_header, confirmation, etc.
    - `char_start`, `char_end` - Offsets in canonical text
  - `candidate_actors[]` - Canonical actor names found in same paragraph

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| Date parsing | **Pandas** | `pd.to_datetime()` with loose/coerce parsing (`parse_date_loosely()`) |
| Paragraph segmentation | **Regex** | Splits on double newlines: `\n\n+` |
| Date-to-paragraph mapping | **Character offset logic** | Finds which paragraph contains each date's char_start offset |
| Actor co-occurrence | **Cross-reference** | Finds entities from `entities.json` whose char offsets overlap this paragraph |
| Canonical actor resolution | **Entity linking** | Uses `linked_actor` field from entities.json |
| Citation generation | **String formatting** | Bolds date text with `**date**` markdown in paragraph context |

---

## Stage 5: Event Synthesis

**Purpose:** Convert date clusters into Events (what happened, when, who, outcome)

### Output Files

#### 1. `events.json`
**Model:** `EventTimeline`

**Contains:**
- `case_id` - Case identifier
- `extraction_timestamp` - When events were synthesized
- `events[]` - Timeline events:
  - `event_id` - Unique ID (event_001, event_002, ...)
  - `cluster_id` - Source date cluster
  - `event_type` - Type of event (only if LLM mode: "filed", "motion", "hearing", "verdict", etc.)
  - `description` - What happened (direct quote or LLM-synthesized)
  - `outcome` - Result if stated (only if LLM mode: "granted", "denied", "settled", etc.)
  - `actors[]` - Canonical names of involved parties
  - `dates[]` - Date text strings
  - `date_parsed` - Primary date as ISO 8601
  - `source_doc_id` - Source document
  - `char_start`, `char_end` - Location in canonical text
  - `confidence` - Reliability score

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| **Mode 1: Deterministic (default)** | | |
| `description` | **Text slicing** | Direct quote: sentence with date ± 1 context sentence |
| `actors` | **Direct passthrough** | Full `candidate_actors` list from date cluster (no curation) |
| `event_type` | **Not set** | Null (no LLM classification) |
| `outcome` | **Not set** | Null (no LLM extraction) |
| | | |
| **Mode 2: LLM (if `use_llm=true`)** | | |
| `event_type` | **LLM** | Reads cluster citation, classifies event type (`synthesize_events_with_llm()`) |
| `description` | **LLM** | Synthesizes what happened in natural language |
| `outcome` | **LLM** | Extracts stated outcome (granted, denied, settled, pending, etc.) |
| `actors` | **LLM** | Curates down to actually involved parties (constrained to `candidate_actors` via JSON schema) |
| | | |
| Span validation | **String search** | `resolve_span()` verifies date text exists in document body (not just header/stamp) |
| Batch processing (LLM mode) | **LLM** | Processes 6 clusters per call to reduce latency (`synthesize_events_batch_with_llm()`) |

#### 2. `stamp_dates.json`
**Model:** `StampDatesArtifact`

**Contains:**
- `case_id` - Case identifier
- `extraction_timestamp` - When separated
- `dates[]` - Header/stamp dates not in body text:
  - `cluster_id` - Original cluster ID
  - `doc_id` - Source document
  - `date_id` - Date identifier
  - `text` - Raw date string
  - `parsed_date` - ISO 8601 datetime
  - `date_type` - creation_date, filing_date, etc.
  - `source` - docling_header, pdf_metadata, etc.

**Extraction Tools:**

| Element | Tool | Details |
|---------|------|---------|
| Stamp detection | **String search failure** | If `resolve_span()` can't find date text in body, it's a stamp |
| Separation | **Filter** | Dates that fail body text validation go to stamp_dates.json, others to events.json |

---

## Data Flow Summary

```
┌─────────────────────────────────────────────────────────┐
│ Stage 1: Metadata Extraction                             │
│ • PostgreSQL: case metadata                              │
│ • pypdfium2: PDF properties                             │
│ • Docling: document structure                           │
│ • Regex: dates, CM/ECF headers, confirmations, captions │
│ • LLM: actor validation, product ID, title ID           │
├─────────────────────────────────────────────────────────┤
│ → files_scan.json, actors.json, products.json,         │
│   gliner_config.json                                    │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ Stage 2: GLiNER Entity Detection                        │
│ • GLiNER: zero-shot NER                                 │
│ • NLTK: sentence segmentation                           │
│ • RapidFuzz: actor linking                              │
│ • Regex: gazetteer fallback                             │
├─────────────────────────────────────────────────────────┤
│ → entities.json                                         │
└─────────────────────────────────────────────────────────┘
                         ↓
         ┌───────────────┴───────────────┐
         ↓                               ↓
┌────────────────────┐      ┌────────────────────────┐
│ Stage 3: Summary   │      │ Stage 4: Date Cluster  │
│ • LLM: 1-3 sent.  │      │ • Pandas: parse dates  │
│   purpose summary  │      │ • Regex: paragraphs    │
├────────────────────┤      │ • Cross-ref: actors    │
│ → summaries.json   │      ├────────────────────────┤
└────────────────────┘      │ → dates.json           │
         │                  └────────────────────────┘
         └───────────────┬───────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│ Stage 5: Event Synthesis                                │
│ • Mode 1 (default): Direct quote + full actor list     │
│ • Mode 2 (LLM): Synthesize type/outcome/actors         │
│ • String search: Separate stamps from events           │
├─────────────────────────────────────────────────────────┤
│ → events.json, stamp_dates.json                        │
└─────────────────────────────────────────────────────────┘
```

---

## Tool Summary by Type

### Regex Patterns
- **Date extraction:** MM/DD/YYYY, YYYY-MM-DD, "Month DD, YYYY"
- **CM/ECF header:** `Case ([^\s]+) Document (\d+) Filed ([\d/]+)`
- **NYSCEF header:** `FILED: .+?COUNTY CLERK (\d{1,2}/\d{1,2}/\d{4})`
- **Document signature:** `NYSCEF DOC. NO. (\d+)`
- **Document references:** `Doc(?:ument)?\s*(?:No\.|#)?\s*(\d+)`
- **Confirmation timestamp:** `received an electronic filing on (\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M)`
- **Confirmation judge:** `Assigned Judge:\s*(.+)`
- **Confirmation filer:** `^([A-Z][A-Za-z.,'\-\s]*?)\s*\|\s*(email)\s*\|\s*(phone)`
- **Confirmation clerk:** `^(?:Hon\.\s+)?([A-Z][A-Za-z.'\-\s]+?),\s*.+?County Clerk`
- **Caption role markers:** `^(plaintiff|defendant|petitioner|respondent)s?`
- **Caption separator:** `^-?\s*(against|vs?\.)\s*-?`
- **Litigation caption:** `In Re (.+?) (?:Litigation|Products? Liability|MDL)`

### LLM Tasks (Ollama or NuExtract)
- **Actor validation:** Sanity-check roster, correct roles, drop junk
- **Product identification:** Read case context, identify accused product
- **Document title:** Choose title from candidates + Docling hint + page 1 text
- **Document summary:** Synthesize 1-3 sentence purpose (Stage 3)
- **Event synthesis:** Extract event type, outcome, curated actors (Stage 5, optional)

### Libraries
- **pypdfium2:** PDF metadata extraction
- **Docling:** Document structure parsing (titles, headers, paragraphs)
- **GLiNER:** Zero-shot named entity recognition
- **NLTK:** Sentence tokenization for context extraction
- **RapidFuzz:** Fuzzy string matching for actor linking
- **Pandas:** Date parsing (`pd.to_datetime()`)
- **PostgreSQL:** Case metadata queries

---

## Configuration

All extraction behavior is controlled by `config/event_extraction.toml`:

```toml
[stage_1]
extract_from_database = true
extract_from_pdfs = true
extract_from_docling = true
extract_from_confirmations = true
identify_document_titles = true
validate_actors_with_llm = true
extract_products = true
llm_backend = "ollama"  # or "nuextract"
llm_model = "qwen3:30b-a3b"
llm_base_url = "http://localhost:11434"
date_patterns = [
    "\\d{1,2}/\\d{1,2}/\\d{4}",
    "\\d{4}-\\d{2}-\\d{2}",
    "(?:January|February|...|December)\\s+\\d{1,2},\\s+\\d{4}"
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

[stage_3]
summarize_documents = true
llm_backend = "ollama"
llm_model = "qwen3:30b-a3b"
llm_base_url = "http://localhost:11434"
max_chars = 8000

[stage_4]
# (No config - uses Stage 1 outputs)

[stage_5]
synthesize_events = true
use_llm = false  # Default: deterministic mode
# If use_llm = true:
llm_backend = "ollama"
llm_model = "qwen3:30b-a3b"
llm_base_url = "http://localhost:11434"
batch_size = 6
```

---

## File Locations

```
lawsuit-parser/
├── config/
│   ├── event_extraction.toml    ← Pipeline configuration
│   ├── llm_prompts.toml          ← LLM prompt templates
│   └── database.toml             ← PostgreSQL connection
│
├── data/
│   ├── cases/<case_id>/
│   │   ├── <case_id>.json        ← Case export from database
│   │   ├── documents/*.pdf       ← Source PDFs
│   │   ├── documents/*.txt       ← Canonical text
│   │   ├── confirmations/*.pdf   ← E-filing notices
│   │   └── docling/documents/    ← Docling outputs
│   │       ├── *.docling.json    ← Full structure
│   │       └── *.md              ← Markdown
│   │
│   └── extraction/<case_id>/events/
│       ├── files_scan.json       ← Stage 1: Document metadata
│       ├── actors.json           ← Stage 1: Actor roster
│       ├── products.json         ← Stage 1: Accused products
│       ├── gliner_config.json    ← Stage 1: GLiNER setup
│       ├── entities.json         ← Stage 2: Detected entities
│       ├── summaries.json        ← Stage 3: Document summaries
│       ├── dates.json            ← Stage 4: Date clusters
│       ├── events.json           ← Stage 5: Events timeline
│       └── stamp_dates.json      ← Stage 5: Header dates
│
└── lawsuit_parser/event_extraction/
    ├── stages/
    │   ├── stage_1_metadata.py   ← Implementation
    │   ├── stage_2_gliner.py
    │   ├── stage_3_summary.py
    │   ├── stage_4_dates.py
    │   └── stage_5_events.py
    ├── models.py                 ← Pydantic schemas
    ├── utils.py                  ← Regex/parsing functions
    └── llm_validation.py         ← LLM integration
```

---

## See Also

- [Event Extraction Usage Guide](event_extraction_usage.md) - How to run the pipeline
- [Pipeline Design](event_extraction_pipeline_design.md) - Architecture and extensibility
- [Implementation Summary](event_extraction_implementation.md) - Technical details