# How actors.json is Generated

Complete explanation of the actor roster generation process in Stage 1.

## Overview

`actors.json` contains every party and role discovered in a case, including plaintiffs, defendants, judges, attorneys, witnesses, and court clerks. The file is generated through a **multi-source extraction → deduplication → validation → labeling** pipeline.

## Real Examples

### Example 1: case_95 (Full Roster)
```json
{
  "case_id": "case_95",
  "actors": [
    {
      "canonical_name": "Brendan T. Lantry",
      "role": "judge",
      "is_named": true,
      "source": "confirmation",
      "aliases": ["Brendan T. Lantry", "Lantry", "BTL"],
      "doc_ids": ["doc_000", "doc_001", ..., "doc_045"],
      "gliner_label": "judge (Brendan T. Lantry)"
    },
    {
      "canonical_name": "BONNIE DARLING",
      "role": "plaintiff",
      "is_named": true,
      "source": "caption",
      "aliases": ["BONNIE DARLING", "DARLING", "BD"],
      "doc_ids": ["doc_001", "doc_002", ..., "doc_011"],
      "gliner_label": "plaintiff (BONNIE DARLING)"
    },
    {
      "canonical_name": "L'ORÉAL USA, INC",
      "role": "defendant",
      "is_named": true,
      "source": "caption",
      "aliases": ["L'ORÉAL USA, INC", "L'ORÉAL USA,", "LUI"],
      "doc_ids": ["doc_001", "doc_002", ..., "doc_044"],
      "gliner_label": "defendant (L'ORÉAL USA, INC)"
    }
  ]
}
```

### Example 2: mdl-1954 (Generic Placeholders Only)
```json
{
  "case_id": "mdl-1954",
  "actors": [
    {
      "canonical_name": "Witness",
      "role": "witness",
      "is_named": false,
      "source": "generic",
      "aliases": [],
      "doc_ids": [],
      "gliner_label": null
    },
    {
      "canonical_name": "Judge",
      "role": "judge",
      "is_named": false,
      "source": "generic",
      "aliases": [],
      "doc_ids": [],
      "gliner_label": null
    }
  ]
}
```

**Why the difference?** mdl-1954 had no readable captions and no confirmation notices, so only generic placeholders were added.

---

## Generation Process

### Phase 1: Multi-Source Extraction

The roster is built by scanning **3 different sources** in this order (a
per-case PostgreSQL lookup used to be a fourth, first source, but was
removed - it only ever covered `case_<numeric>` ids scraped from that DB,
never MDL dockets, and even then had no reliable plaintiff/defendant
columns to draw on):

#### Source 1: Document Captions

**Location:** `lawsuit_parser/event_extraction/stages/stage_1_metadata.py:273-277`, `utils.py:224-288`

**Process:**

For each document's first page (from Docling):

1. **Find role markers:**
   ```regex
   ^(plaintiff|defendant|petitioner|respondent)s?$
   ```
   Example: "Plaintiff," or "Defendants."

2. **Find separator:**
   ```regex
   ^-?\s*(against|vs?\.)\s*-?
   ```
   Example: "-against-" or "v."

3. **Extract plaintiff names:**
   - Lines between last boilerplate and first role marker
   - Filter out: court headers, addresses (ZIP codes), docket stamps

4. **Extract defendant names:**
   - Lines between separator and second role marker

5. **Split co-parties:**
   - Semicolon-separated: "John Doe; Jane Smith"
   - "and"-joined: "John Doe and Jane Smith"
   - Strip "et al."

**Tool:** Regex + heuristic parsing (`parse_caption_block()`)

**Caption Structure Recognized:**
```
SUPREME COURT OF THE STATE OF NEW YORK
COUNTY OF NEW YORK
----------------------------------------X

BONNIE DARLING,
                                    Plaintiff,        ← Role marker

         -against-                                    ← Separator

L'ORÉAL USA, INC.,
GOLDWELL,
KAO USA, INC.,
                                    Defendants.       ← Role marker

----------------------------------------X
```

**Yields:**
- `source="caption"` plaintiffs
- `source="caption"` defendants

**Example from case_95:**
```python
# Input lines from Docling first page:
[
  "SUPREME COURT OF THE STATE OF NEW YORK",
  "COUNTY OF NEW YORK",
  "BONNIE DARLING,",
  "Plaintiff,",
  "-against-",
  "L'ORÉAL USA, INC.,",
  "GOLDWELL,",
  "Defendants."
]

# Output:
plaintiffs = ["BONNIE DARLING"]
defendants = ["L'ORÉAL USA, INC.", "GOLDWELL"]
```

---

#### Source 2: E-Filing Confirmation Notices

**Location:** `lawsuit_parser/event_extraction/stages/stage_1_metadata.py:303-330`, `utils.py:114-162`

**Process:**

For each document's matching confirmation PDF (same filename under `confirmations/`):

1. **Extract judge:**
   ```regex
   Assigned Judge:\s*(.+)
   ```

2. **Extract filer (counsel):**
   ```regex
   ^([A-Z][A-Za-z.,'\-\s]*?)\s*\|\s*(email@domain.com)\s*\|\s*(phone)
   ```

3. **Extract court clerk:**
   ```regex
   ^(?:Hon\.\s+)?([A-Z][A-Za-z.'\-\s]+?),\s*.+?County Clerk
   ```

4. **Extract timestamp:**
   ```regex
   received an electronic filing on\s+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)
   ```

**Tool:** Regex patterns (`extract_confirmation_details()`)

**Confirmation Notice Example:**
```
SUPREME COURT OF THE STATE OF NEW YORK
NEW YORK COUNTY CLERK'S OFFICE

You received an electronic filing on 03/03/2026 10:30 AM

Assigned Judge: Brendan T. Lantry

JONATHAN MICHAEL SEDGH | jsedgh@law.com | (212) 555-1234

Hon. Milton A. Tingling, New York County Clerk
```

**Yields:**
- `source="confirmation"` judge
- `source="confirmation"` counsel (filer)
- `source="confirmation"` court_clerk

**Example from case_95:**
```python
{
  "assigned_judge": "Brendan T. Lantry",    # → role=judge
  "filer_name": "JONATHAN MICHAEL SEDGH",   # → role=counsel
  "court_clerk": "Milton A. Tingling"       # → role=court_clerk
}
```

---

#### Source 3: LLM Validation (Optional)

**Location:** `lawsuit_parser/event_extraction/stages/stage_1_metadata.py:422-433`, `llm_validation.py:194-230`

**Process:**

After collecting actors from sources 1-3:

1. **Build prompt** with:
   - Candidate roster (name, role, source, doc count)
   - Case caption (if available)
   - Court name (if available)

2. **LLM reads and validates:**
   - Corrects mis-assigned roles
   - Drops junk entries (addresses, boilerplate, OCR noise)
   - Rephrases names for GLiNER labels
   - Marks each actor `keep=true/false`

3. **Apply results:**
   - Keep actors where `keep=true`
   - Update roles from LLM corrections
   - Update canonical names if LLM cleaned them
   - Set `gliner_label` from LLM response

**Tool:** LLM (Ollama or NuExtract)

**Backends:**
- **Ollama** (default): `qwen3:30b-a3b` at `http://localhost:11434`
- **NuExtract**: `numind/NuExtract3` at `http://localhost:8000/v1`

**Example Prompt:**
```
You are a legal document analyzer. Review this roster of parties discovered in a lawsuit.

CASE CONTEXT:
- Caption: "Bonnie Darling v. L'Oréal USA, Inc."
- Court: "Supreme Court of the State of New York"

CANDIDATES:
1. name='BONNIE DARLING' current_role=plaintiff is_named=True source=caption seen_in=11 document(s)
2. name='L'ORÉAL USA, INC' current_role=defendant is_named=True source=caption seen_in=19 document(s)
3. name='123 Main Street' current_role=plaintiff is_named=True source=caption seen_in=1 document(s)

For each candidate, respond with:
- canonical_name: cleaned name
- role: corrected role (plaintiff, defendant, judge, counsel, witness, court_clerk, other)
- gliner_label: how to phrase for entity recognition
- keep: true/false
```

**Example Response:**
```json
{
  "actors": [
    {
      "canonical_name": "BONNIE DARLING",
      "role": "plaintiff",
      "gliner_label": "plaintiff (BONNIE DARLING)",
      "keep": true
    },
    {
      "canonical_name": "L'ORÉAL USA, INC",
      "role": "defendant",
      "gliner_label": "defendant (L'ORÉAL USA, INC)",
      "keep": true
    },
    {
      "canonical_name": "123 Main Street",
      "role": "other",
      "gliner_label": "address",
      "keep": false
    }
  ]
}
```

**Fallback:** If LLM is unreachable or returns invalid JSON, the unvalidated roster is used.

**Config to disable:**
```toml
[stage_1]
validate_actors_with_llm = false
```

---

### Phase 2: Deduplication

**Location:** `lawsuit_parser/event_extraction/stages/stage_1_metadata.py:556-593`

**Algorithm:** `_add_actor()`

For each discovered actor:

1. **Normalize name:**
   ```python
   # Strip Inc., LLC, et al., commas, periods
   "L'ORÉAL USA, INC." → "L'ORÉAL USA"
   ```

2. **Check for existing match:**
   - Same normalized name
   - Same role
   - If found: merge `doc_ids`, don't duplicate

3. **If new:**
   - Create `Actor` object
   - Generate aliases
   - Append to roster

**Example:**
```python
# First encounter (doc_001):
_add_actor(actors, "L'ORÉAL USA, INC.", "defendant", "caption", "doc_001")
# Creates: Actor(canonical_name="L'ORÉAL USA, INC.", doc_ids=["doc_001"])

# Second encounter (doc_002):
_add_actor(actors, "L'ORÉAL USA, INC", "defendant", "caption", "doc_002")
# Normalizes to same name → merges: doc_ids=["doc_001", "doc_002"]

# Different name:
_add_actor(actors, "L'OREAL USA, INC", "defendant", "caption", "doc_003")
# OCR variant → creates separate entry (unless LLM validation merges them)
```

---

### Phase 3: Alias Generation

**Location:** `lawsuit_parser/event_extraction/utils.py:379-435`

**Algorithm:** `find_party_aliases()`

For each actor, generate common alternate forms:

#### For People:
```python
"Brendan T. Lantry" →
  ["Brendan T. Lantry",  # Full name
   "Lantry",             # Last name only
   "BTL"]                # Initials
```

#### For Organizations:
```python
"L'ORÉAL USA, INC" →
  ["L'ORÉAL USA, INC",   # Full name
   "L'ORÉAL USA,",       # Without Inc
   "LUI"]                # Acronym

"KOHLBERG KRAVIS ROBERT & CO. a/k/a KKR & CO., INC" →
  ["KOHLBERG KRAVIS ROBERT & CO. a/k/a KKR & CO., INC",
   "KOHLBERG KRAVIS ROBERT & CO. a/k/a KKR & CO.,",
   "KKRCAKCI"]
```

**Purpose:** Aliases are used by:
- GLiNER gazetteer pass (Stage 2) for exact string matching
- Entity linking to resolve "Lantry" → canonical "Brendan T. Lantry"

---

### Phase 4: Generic Placeholders

**Location:** `lawsuit_parser/event_extraction/stages/stage_1_metadata.py:483-492`

**Process:**

After extraction and validation, add **generic placeholders** for any role with no named individual:

```python
GENERIC_ACTOR_ROLES = {
    "witness": "Witness",
    "attorney": "Attorney",
    "judge": "Judge",
    "court_clerk": "Court Clerk",
}

# If no judge was found:
actors.append(Actor(
    canonical_name="Judge",
    role="judge",
    is_named=False,
    source="generic",
    aliases=[],
    doc_ids=[],
    gliner_label=None
))
```

**Purpose:** GLiNER needs a label for every role, even if no specific person was identified. Generic labels like "judge" (without a name) still allow Stage 2 to find judge mentions in text.

**Example:**
- case_95: Found judge "Brendan T. Lantry" → no generic "Judge" added
- mdl-1954: No judge found → generic "Judge" placeholder added

---

### Phase 5: GLiNER Label Assignment

**Location:** `lawsuit_parser/event_extraction/stages/stage_1_metadata.py:933-984`

**Process:**

Each actor gets a `gliner_label` field for Stage 2:

#### Named Actors:
```python
# Template: "{role} ({canonical_name})"
"plaintiff (BONNIE DARLING)"
"defendant (L'ORÉAL USA, INC)"
"judge (Brendan T. Lantry)"
"counsel (JONATHAN MICHAEL SEDGH)"
```

#### Generic Placeholders:
```python
# Just the role, no parentheses
"witness"
"attorney"
"judge"
"court_clerk"
```

**Note:** If LLM validation was enabled, `gliner_label` comes from the LLM response instead of this template.

---

## Field Breakdown

### `canonical_name`
- **Source:** As discovered, or cleaned by LLM
- **Examples:** "BONNIE DARLING", "L'ORÉAL USA, INC", "Witness"

### `role`
- **Allowed values:** plaintiff, defendant, judge, court_clerk, counsel, witness, attorney, other
- **Source:** Extracted from context (caption role marker, confirmation field) or corrected by LLM

### `is_named`
- **true:** Actual person/entity (e.g., "Brendan T. Lantry")
- **false:** Generic placeholder (e.g., "Judge")

### `source`
- **caption:** From document caption block
- **confirmation:** From e-filing confirmation notice
- **llm:** Added/corrected by LLM (rare)
- **generic:** Placeholder for unfilled role

### `aliases`
- **Generated by:** `find_party_aliases()` heuristic
- **Examples:**
  - People: full name, last name, initials
  - Orgs: full name, name without Inc/LLC, acronym

### `doc_ids`
- **List of:** Documents where this actor appears (doc_000, doc_001, ...)
- **Purpose:** Track which documents to search for entity mentions in Stage 2
- **Merged:** When same actor found in multiple docs

### `gliner_label`
- **Format:** `"{role} ({name})"` or just `"{role}"` for generics
- **Purpose:** Exact label passed to GLiNER in Stage 2
- **null:** Only for generic placeholders (they still get used, just with role as label)

### `attributed_to`
- **Always empty for actors** (only used for products.json)

---

## Configuration

**File:** `config/event_extraction.toml`

```toml
[stage_1]
# Which sources to scan
extract_from_pdfs = true           # PDF metadata
extract_from_docling = true        # Caption parsing
extract_from_confirmations = true  # E-filing notices

# LLM validation
validate_actors_with_llm = true    # Sanity-check roster
llm_backend = "ollama"             # or "nuextract"
llm_model = "qwen3:30b-a3b"
llm_base_url = "http://localhost:11434"
```

---

## Code Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ Stage1Metadata.run()                                        │
└─────────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 1. Database Extraction (if enabled)                         │
│    - Query public.court_cases                               │
│    - Parse caption → plaintiffs, defendants                 │
│    - _add_actor() for each                                  │
└─────────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. Scan All Documents (for each PDF):                       │
│    a. Extract Docling metadata                              │
│    b. Parse caption block (parse_caption_block)             │
│       - Find role markers, separator                        │
│       - Extract plaintiff/defendant names                   │
│       - _add_actor() for each                               │
│    c. Load matching confirmation notice                     │
│       - Extract judge, filer, clerk (regex)                 │
│       - _add_actor() for each                               │
└─────────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. LLM Validation (if enabled)                              │
│    - Build prompt with candidate roster                     │
│    - Send to Ollama/NuExtract                               │
│    - Apply results: update roles, labels, drop junk         │
└─────────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. Add Generic Placeholders                                 │
│    - For each unfilled role (witness, attorney, judge, etc.)│
│    - Add Actor(is_named=false, source="generic")            │
└─────────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│ 5. Save actors.json                                         │
│    - ActorsArtifact(case_id, actors, timestamp)             │
└─────────────────────────────────────────────────────────────┘
```

---

## Deduplication Details

### `_add_actor()` Algorithm

```python
def _add_actor(actors, name, role, source, doc_id=None):
    # 1. Normalize name for matching
    normalized = normalize_party_name(name)
    # "L'ORÉAL USA, INC." → "L'ORÉAL USA"

    # 2. Check if already exists (same normalized name + role)
    for actor in actors:
        if (actor.role == role and
            normalize_party_name(actor.canonical_name) == normalized):
            # MERGE: just add doc_id
            if doc_id and doc_id not in actor.doc_ids:
                actor.doc_ids.append(doc_id)
            return

    # 3. NEW: create entry
    actors.append(Actor(
        canonical_name=name,
        role=role,
        is_named=True,
        source=source,
        aliases=find_party_aliases(name),
        doc_ids=[doc_id] if doc_id else []
    ))
```

### `normalize_party_name()` Algorithm

**Location:** `lawsuit_parser/event_extraction/utils.py:388-404`

```python
def normalize_party_name(party_name):
    # 1. Lowercase
    party_name = party_name.lower()

    # 2. Strip business suffixes
    # Inc., Incorporated, Corp., LLC, Ltd., Company, Co.
    party_name = re.sub(
        r"\s+(inc\.?|incorporated|corp\.?|corporation|llc|ltd\.?|limited|company|co\.?)$",
        "",
        party_name,
        flags=re.IGNORECASE
    )

    # 3. Strip punctuation
    party_name = party_name.replace(",", "").replace(".", "")

    # 4. Strip "et al."
    party_name = re.sub(r"\s*,?\s*et\.?\s*al\.?$", "", party_name)

    # 5. Normalize whitespace
    party_name = " ".join(party_name.split())

    return party_name.strip()
```

**Examples:**
```python
normalize_party_name("L'ORÉAL USA, INC.") → "l'oréal usa"
normalize_party_name("KAO USA, Inc") → "kao usa"
normalize_party_name("Bonnie Darling, et al.") → "bonnie darling"
```

---

## Common Issues & Debugging

### Issue 1: Only Generic Placeholders (like mdl-1954)

**Symptoms:**
- All actors have `is_named=false`, `source="generic"`
- No actual party names

**Causes:**
1. No readable caption blocks (poor OCR, non-standard format)
2. No confirmation notices available
3. LLM validation dropped all candidates (too aggressive filtering)

**Debug:**
- Check files_scan.json → documents[].docling_metadata.title
- Look for caption text in raw Docling JSON
- Disable LLM validation: `validate_actors_with_llm = false`

### Issue 2: Duplicate Actors (OCR Variants)

**Symptoms:**
```json
{"canonical_name": "L'ORÉAL USA, INC", ...},
{"canonical_name": "L'OREAL USA, INC", ...}
```

**Causes:**
- OCR inconsistencies across documents
- Normalization not aggressive enough

**Solutions:**
- Enable LLM validation (merges variants)
- Improve `normalize_party_name()` logic

### Issue 3: Wrong Roles

**Symptoms:**
- Address line marked as plaintiff
- Document title marked as defendant

**Causes:**
- Caption parser failing to filter boilerplate
- No LLM validation to correct

**Solutions:**
- Enable LLM validation: `validate_actors_with_llm = true`
- Improve boilerplate filtering in `parse_caption_block()`

### Issue 4: Missing Judge/Clerk

**Symptoms:**
- Only generic "Judge" placeholder, no named judge

**Causes:**
- No confirmation notices
- Confirmation regex pattern mismatch

**Debug:**
- Check if confirmations/ directory exists and has PDFs
- Check confirmation JSON for "Assigned Judge:" text
- Verify regex pattern matches actual format

---

## Performance Notes

- **Database query:** ~100-500ms (if available)
- **Caption parsing:** ~50-200ms per document (regex-based, fast)
- **Confirmation parsing:** ~50-100ms per document (regex-based, fast)
- **LLM validation:** ~3-10 seconds (network round-trip to Ollama/NuExtract)
- **Total Stage 1:** ~10-30 seconds for 50-document case (without LLM), ~20-60 seconds (with LLM)

**Optimization:** Disable LLM validation for faster processing, but expect lower quality roster.

---

## Related Files

- **Stage implementation:** `lawsuit_parser/event_extraction/stages/stage_1_metadata.py`
- **Utilities:** `lawsuit_parser/event_extraction/utils.py`
- **LLM validation:** `lawsuit_parser/event_extraction/llm_validation.py`
- **Prompts:** `lawsuit_parser/event_extraction/prompts.py`
- **Prompt text:** `config/llm_prompts.toml`
- **Config:** `config/event_extraction.toml`
- **Data models:** `lawsuit_parser/event_extraction/models.py`

---

## See Also

- [Pipeline Outputs Reference](pipeline_outputs.md) - What each stage produces
- [Event Extraction Usage Guide](event_extraction_usage.md) - How to run the pipeline
- [Pipeline Design](event_extraction_pipeline_design.md) - Architecture overview