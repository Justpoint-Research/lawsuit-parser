# Lawsuit Event Extraction — Stages 0–4 Implementation Spec

Instructions for Claude Code. Implement stages 0 through 4 of the event extraction
pipeline inside the existing `lawsuit-parser` repository.

---

## 0. Context

The repo already parses lawsuit PDFs to text via Docling. This work adds the front
half of an event extraction pipeline that will eventually produce a chronological,
provenance-backed timeline of a lawsuit.

**Existing structure (do not restructure):**

```
lawsuit_parser/
  parsers/          # pdf_parser.py, batch.py — Docling parsing, already works
  postprocessors/   # base.py, passthrough.py
  utils/            # db.py, gcs.py, case_exporter.py
apps/               # Streamlit case browser
scripts/            # CLI entrypoints
tests/              # pytest
```

**What you are adding:** a new `lawsuit_parser/extraction/` package plus one CLI script.

### Full pipeline for context (implement only 0–4)

| Stage | Purpose | Engine |
|---|---|---|
| **0** | Canonical text + segmentation | deterministic |
| **1** | Document metadata, DCT, seed parties | NuExtract3 |
| **2** | Party registry + coreference + mention index | NuExtract3 + Maverick |
| **3** | Exhaustive span sweep (recall floor) | GLiNER |
| **4** | Proto-events (predicate + typed edges) | GLiNER-Relex |
| 5 | Event mention extraction | NuExtract3 + XGrammar |
| 6 | Date arithmetic + validation | deterministic |
| 7 | Relative dates, ordering, merge | Gemma 4 |
| 8 | Topological sort → timeline | deterministic |

**Stages 5–8 are out of scope. Do not implement them, do not stub them beyond the
schema fields already specified below.**

### Simplifying assumptions for this phase

- Documents are **English**.
- Text quality is good — **no OCR handling, no image/VLM path, no scan fallback**.
- Small corpora (single case, ~5–40 documents). Optimise for debuggability, not throughput.
- **No orchestration framework.** Plain Python, sequential stages, JSON artifacts on disk.
- **No database writes.** Postgres integration comes later; keep the storage layer
  behind a thin interface so it can be swapped.

---

## 1. Hard rules

These are correctness constraints, not style preferences. Violating any of them
silently corrupts downstream stages.

1. **One canonical text per document.** Produced once in stage 0. Every offset in the
   entire system indexes into it. No stage may re-tokenize, re-strip, re-normalize, or
   otherwise derive a second version of the text.
2. **Every extracted span carries `(doc_id, char_start, char_end)`** and the substring
   at those offsets must equal the recorded surface form. Assert this. If a model
   returns a string you cannot locate unambiguously, **drop it and count the drop** —
   never guess an offset.
3. **All model output is parsed into Pydantic models before leaving the stage
   function.** No raw dicts cross a stage boundary.
4. **No calendar arithmetic anywhere in stages 0–4.** Dates are captured as surface
   strings only. Resolution is stage 6's job.
5. **Stages are pure functions of their inputs plus artifacts on disk.** Running
   stage N twice must produce identical output.
6. **Fail loudly on schema violations, drop quietly on low-confidence extractions.**
   A malformed model response is a bug; a span the model didn't find is data.
7. **Do not use `localStorage`-style hidden state, global mutable singletons, or
   module-level model loading.** Models load explicitly via a context manager.

---

## 2. Environment

Python 3.12+, `uv`. Add dependencies:

```toml
[project.dependencies]
pydantic = ">=2.0"
gliner = "*"
maverick-coref = "*"
openai = "*"          # OpenAI-compatible client for the vLLM endpoint
rapidfuzz = "*"       # alias / name normalization
tomli-w = "*"
```

`vllm` is **not** a project dependency — it runs as a separate server process. Document
the launch command in the README; do not shell out to start it from Python.

**GLiNER-Relex:** verify current availability before adding it. It is a recent research
release and may not have a stable package. If it is not cleanly installable, implement
stage 4 behind the interface described in §7 with the `disabled` implementation as
default, and make the whole stage skippable. Do not block stages 0–3 on it.

### Model serving

NuExtract3 via vLLM, started manually:

```bash
vllm serve numind/NuExtract3 \
  --trust-remote-code \
  --chat-template-content-format openai \
  --max-model-len 32768 \
  --port 8000
```

**Verify the NuExtract3 invocation format against the current model card on Hugging
Face before writing the client.** The 2.x line passed the extraction template via
`extra_body={"chat_template_kwargs": {"template": ..., "examples": [...]}}`. Confirm
whether 3.x uses the same mechanism, and whether it is compatible with vLLM's
`guided_json`. If the native template mechanism and `guided_json` conflict, prefer the
**native template** plus strict Pydantic validation on the response, and record the
decision in a comment. Temperature must be `0`.

GLiNER and Maverick run in-process.

---

## 3. Configuration

`config/extraction.toml`, loaded into a Pydantic `Settings` model. Every tunable lives
here — no magic numbers in code.

```toml
[paths]
data_root = "data/cases"

[nuextract]
base_url = "http://localhost:8000/v1"
model = "numind/NuExtract3"
temperature = 0.0
max_retries = 3
timeout_s = 120

[gliner]
model = "urchade/gliner_multi-v2.1"   # verify best current checkpoint
threshold = 0.5
batch_size = 8
labels = [
  "temporal expression",
  "party or organization",
  "court",
  "geographic location",
  "monetary amount",
  "legal action or event",
  "document reference",
  "case citation",
]

[maverick]
model = "sapienzanlp/maverick-mes-ontonotes"   # verify current checkpoint
enabled = true

[relex]
enabled = false
relations = ["has_date", "actor", "affected", "at_location", "instrument"]

[registry]
fuzzy_match_threshold = 88
```

The `gliner.labels` list is a **tunable hyperparameter**, not a constant. Label wording
materially changes GLiNER output quality. Make it trivially editable and make stage 3
record which label set produced a given artifact.

---

## 4. Storage layer

`lawsuit_parser/extraction/store.py`

Artifacts on disk, one JSON file per stage per case:

```
data/cases/<case_id>/
  documents/<doc_id>.txt        # canonical text, stage 0
  stages/
    00_segments.json
    01_metadata.json
    02_registry.json
    03_spans.json
    04_protoevents.json
  run.json                      # run metadata: timestamps, model ids, config hash
```

```python
class ArtifactStore:
    def __init__(self, case_id: str, root: Path): ...
    def write_stage(self, stage: str, model: BaseModel) -> None: ...
    def read_stage(self, stage: str, model_cls: type[T]) -> T: ...
    def has_stage(self, stage: str) -> bool: ...
    def write_canonical_text(self, doc_id: str, text: str) -> None: ...
    def read_canonical_text(self, doc_id: str) -> str: ...
```

Write JSON with `indent=2` and sorted keys — these files get read by humans constantly
during tuning, and they should diff cleanly between runs.

`run.json` records, per stage: timestamp, config hash, model identifiers and revisions,
and the stage's own counters (see §9). This is the poor man's provenance layer and it
is what makes tuning tractable.

---

## 5. Schemas

`lawsuit_parser/extraction/schemas.py` — Pydantic v2, all frozen where practical.

```python
class Span(BaseModel):
    doc_id: str
    char_start: int
    char_end: int
    text: str          # MUST equal canonical_text[char_start:char_end]

class Segment(BaseModel):
    seg_id: str
    doc_id: str
    page: int | None
    para_label: str | None       # "¶ 42", "II.B", None
    section_type: Literal["caption", "body", "heading", "signature",
                          "certificate_of_service", "exhibit", "other"]
    char_start: int
    char_end: int

class SegmentsArtifact(BaseModel):
    case_id: str
    segments: list[Segment]
    documents: list[DocumentRef]

# ---- stage 1

class PartySeed(BaseModel):
    name: str
    role: Literal["plaintiff", "defendant", "intervenor", "third_party",
                  "amicus", "counsel", "court", "other"]
    short_name: str | None

class DocumentMetadata(BaseModel):
    doc_id: str
    court: str | None
    case_number: str | None
    document_title: str | None
    document_number: str | None
    filing_date_raw: str | None      # SURFACE STRING ONLY — no parsing
    signature_date_raw: str | None
    dct_raw: str | None              # chosen anchor: filing date, else signature date
    filed_by: str | None
    parties: list[PartySeed]
    source_spans: dict[str, Span]    # field name -> provenance

class MetadataArtifact(BaseModel):
    case_id: str
    documents: list[DocumentMetadata]

# ---- stage 2

class Party(BaseModel):
    party_id: str                    # "p_001"
    canonical_name: str
    party_type: Literal["individual", "organization", "government", "court", "unknown"]
    roles: list[str]
    aliases: list[str]

class PartyMention(BaseModel):
    span: Span
    party_id: str
    source: Literal["caption", "alias_definition", "exact_match", "fuzzy_match",
                    "role_anaphora", "coref"]
    confidence: float

class RegistryArtifact(BaseModel):
    case_id: str
    parties: list[Party]
    mentions: list[PartyMention]     # THE mention index
    unresolved: list[Span]           # entity-like spans that failed to link

# ---- stage 3

class GlinerSpan(BaseModel):
    span: Span
    seg_id: str
    label: str
    score: float

class SpansArtifact(BaseModel):
    case_id: str
    label_set: list[str]             # exactly what was prompted
    model_id: str
    threshold: float
    spans: list[GlinerSpan]
    realignment_failures: int

# ---- stage 4

class ProtoEventEdge(BaseModel):
    relation: Literal["has_date", "actor", "affected", "at_location", "instrument"]
    target: Span
    score: float

class ProtoEvent(BaseModel):
    proto_id: str
    seg_id: str
    predicate: Span
    edges: list[ProtoEventEdge]

class ProtoEventsArtifact(BaseModel):
    case_id: str
    enabled: bool
    proto_events: list[ProtoEvent]
    priority_segments: list[str]     # seg_ids, emitted even when relex disabled
```

Note `filing_date_raw` / `dct_raw` — **surface strings, never parsed dates**. If you
find yourself importing `datetime` in stages 0–4, stop.

---

## 6. Stage specifications

### Stage 0 — `segments.py`

```python
def build_segments(case_id: str, docling_outputs: list[DoclingDoc],
                   store: ArtifactStore) -> SegmentsArtifact
```

1. Produce canonical text per document from Docling output. Fix the whitespace policy
   **once** and document it in a module docstring — e.g. normalize `\r\n` → `\n`,
   collapse runs of 3+ blank lines, strip trailing whitespace per line, preserve
   everything else. Do not "clean" further; every character removed shifts offsets.
2. Write canonical text to disk. All later stages read it from there, never from Docling.
3. Parse the CM/ECF page header if present, by regex:
   `Case <case_no>  Document <n>  Filed <date>  Page <p> of <total>`
   This yields case number, document number, filing date, and a page→offset map with
   near-perfect accuracy and zero model calls. Treat absence as normal (state courts
   vary); log the hit rate.
4. Segment on Docling structure: caption block, numbered paragraphs, headings,
   signature block, certificate of service. Numbered paragraphs are the primary unit —
   detect leading `N.` / `¶ N` patterns. Where no numbering exists, fall back to
   Docling's paragraph boundaries.
5. Assign `section_type`. Caption = everything before the first numbered paragraph or
   the document title, whichever comes first.

**Acceptance:** for every segment, `canonical_text[char_start:char_end]` is non-empty
and segments are non-overlapping and ordered. Segments need not cover 100% of the text.

### Stage 1 — `metadata.py`

```python
def extract_metadata(case_id: str, segments: SegmentsArtifact,
                     client: NuExtractClient, store: ArtifactStore) -> MetadataArtifact
```

One NuExtract3 call per document. **Input is the caption block plus the signature
block only** — not the full document. This keeps the call small, cheap, and accurate.

Template (all fields verbatim so nothing can be invented):

```json
{
  "court": "verbatim-string",
  "case_number": "verbatim-string",
  "document_title": "verbatim-string",
  "document_number": "verbatim-string",
  "filing_date": "verbatim-string",
  "signature_date": "verbatim-string",
  "filed_by": "verbatim-string",
  "parties": [{
    "name": "verbatim-string",
    "role": "verbatim-string",
    "short_name": "verbatim-string"
  }]
}
```

Post-processing:
- Map every returned string back to a `Span` by exact search within the input block.
  Ambiguous or absent → set field to `None` and increment a counter.
- Normalize `role` to the `PartySeed.role` enum with a small mapping table; unmapped →
  `"other"` plus a warning log.
- `dct_raw` = filing date if present, else signature date, else `None`. Prefer the
  CM/ECF header date from stage 0 over the model's answer when both exist, and log
  disagreements — those are worth looking at.

**Acceptance:** every non-null field has a valid span; the parties list is non-empty
for at least the initiating pleading.

### Stage 2 — `registry.py`

The highest-leverage stage. A perfectly dated timeline with scrambled parties is
worthless. Order matters:

```python
def build_registry(case_id: str, segments, metadata,
                   client, coref, store) -> RegistryArtifact
```

1. **Seed** the registry from all captions across all documents (stage 1 output).
   Dedupe by normalized name. Assign `p_001`, `p_002`, … deterministically by first
   appearance in document order.

2. **Alias harvest** — one NuExtract3 call over the "Parties" section of the initiating
   pleading. Target `d/b/a`, `f/k/a`, `a/k/a`, and `(hereinafter "ACME")` definitions.
   These parenthetical short-name definitions are the highest-precision alias source in
   the entire corpus; treat a match here as authoritative over fuzzy matching.

   ```json
   {"aliases": [{"full_name": "verbatim-string",
                 "alias": "verbatim-string",
                 "alias_type": "verbatim-string"}]}
   ```

3. **Normalization** — suffix folding for corporate names: `Inc.`/`Incorporated`,
   `Corp.`/`Corporation`, `Co.`, `LLC`, `L.L.C.`, `Ltd.`, `LP`, `N.A.`. Case-fold,
   strip punctuation, collapse whitespace. Merge registry entries whose normalized
   forms match exactly. Use `rapidfuzz` above `fuzzy_match_threshold` for near-matches,
   and **log every fuzzy merge** — this is where silent corruption happens.

4. **Coreference** — Maverick per document over canonical text. Produces mention chains.

5. **Linking** — for each chain, pick the representative (longest mention containing a
   proper noun) and link to the registry by, in order: exact normalized match, alias
   match, fuzzy match above threshold. Unlinked chains → `unresolved`.

6. **Role anaphora override — do this last and let it win.** Maverick is trained on
   newswire; in legal prose it will sometimes chase a nearer antecedent for role terms
   and be confidently wrong. Deterministically resolve these from the caption, and
   overwrite any conflicting coref assignment:

   - `Plaintiff`, `Plaintiffs`, `Defendant`, `Defendants`, `Movant`, `Respondent`,
     `Petitioner`, `Appellant`, `Appellee` → the caption-assigned party for that role
     in that document.
   - `the Court` → the court entity.
   - Role term followed by a name (`Defendant ACME Corp.`) → resolve by the name, and
     use it to confirm the role assignment.

   Where a role maps to multiple parties (three defendants, bare "Defendants"), link to
   **all** of them and mark `confidence` accordingly. Do not pick one.

   Let Maverick keep pronouns and descriptive NPs ("the company", "her employer").

**Acceptance:** every `PartyMention.span` is valid; every `party_id` exists in
`parties`; role-anaphora mentions have `source="role_anaphora"`; the count of
`unresolved` spans is reported.

### Stage 3 — `spans.py`

```python
def sweep_spans(case_id: str, segments, gliner, store) -> SpansArtifact
```

Run GLiNER over **100% of segments**. Not a sample, not just likely ones. This is
cheap and it is the recall denominator for the entire pipeline — you cannot audit what
you never looked for.

- Batch segments per `gliner.batch_size`.
- **Realign every returned string to exact char offsets** against canonical text. GLiNER
  returns entity strings, and offsets relative to its own input; do not trust them
  blindly. Search within the segment's char range. Multiple matches within a segment and
  no way to disambiguate → drop and increment `realignment_failures`.
- Record `label_set`, `model_id`, and `threshold` in the artifact so two runs with
  different label wording are distinguishable after the fact.

**Acceptance:** `realignment_failures / total_returned < 0.02`. If it is higher, the
realignment logic is wrong — fix it rather than raising the tolerance.

### Stage 4 — `protoevents.py`

```python
def build_proto_events(case_id: str, segments, spans, relex, store) -> ProtoEventsArtifact
```

**Priority segment selection (always runs, even when Relex is disabled):**

A segment is priority if it contains a `temporal expression` span, OR contains both a
`legal action or event` span and a party mention from stage 2. Emit the list regardless
— stage 5 needs it either way, and it is useful on its own.

**If `relex.enabled`:** run GLiNER-Relex over priority segments to produce proto-events
— one predicate with typed edges to its date, actors, and location. The purpose is
disambiguation before generation: a paragraph with three dates and two parties is
genuinely ambiguous to a slot-filling model, and a proto-event turns it into a
well-posed question.

**If disabled:** emit `proto_events: []` and `enabled: false`. Stage 5 will fall back
to whole-segment extraction. This must be a supported, tested path, not a degraded one.

Same realignment discipline as stage 3.

**Acceptance:** every edge target span is valid; every `seg_id` is in the priority list;
the pipeline completes end-to-end with `relex.enabled = false`.

---

## 7. Model client interfaces

`lawsuit_parser/extraction/models.py` — thin wrappers, explicit lifecycle, no globals.

```python
class NuExtractClient:
    """OpenAI-compatible client against the vLLM endpoint."""
    def extract(self, text: str, template: dict,
                examples: list[dict] | None = None) -> dict:
        """Returns parsed JSON. Retries on transient errors only.
        Raises ExtractionError on malformed output after retries."""

class GlinerRunner:
    def __enter__(self) -> "GlinerRunner": ...   # loads model
    def predict_batch(self, texts: list[str], labels: list[str],
                      threshold: float) -> list[list[RawSpan]]: ...

class CorefRunner:
    def __enter__(self) -> "CorefRunner": ...
    def predict(self, text: str) -> list[list[tuple[int, int]]]:
        """Char-offset mention chains."""

class RelexRunner(Protocol):
    def predict(self, text: str, relations: list[str]) -> list[RawProtoEvent]: ...

class DisabledRelexRunner:
    """Default. Returns []."""
```

**Retry policy:** retry connection errors, timeouts, and 5xx. **Do not retry schema
validation failures** — at temperature 0 the same input produces the same bad output,
and retrying only hides the error rate. Raise, log the offending response verbatim to
`data/cases/<case_id>/errors/`, and continue with that item dropped.

---

## 8. CLI

`scripts/extract.py`

```bash
uv run scripts/extract.py \
    --case-id 1-19-cv-01234 \
    --stages 0-4 \
    [--force] \
    [--config config/extraction.toml]
```

- `--stages` accepts `0-4`, `2`, `1,3`.
- Skip stages whose artifact already exists unless `--force`. This is the entire caching
  story for now and it is sufficient at this scale.
- Load each model **only if** a stage requiring it will actually run — do not pay
  Maverick's load time to run stage 3 alone.
- Print the stage report (§9) to stdout after each stage.

---

## 9. Instrumentation

Each stage returns counters, written to `run.json` and printed. These are the numbers
you will actually tune against:

| Stage | Counters |
|---|---|
| 0 | docs, segments, segments by `section_type`, CM/ECF header hit rate |
| 1 | docs processed, null fields per field name, span-mapping failures, header/model date disagreements |
| 2 | parties, aliases, mentions by `source`, fuzzy merges (**list them**), unresolved spans, role-anaphora overrides applied |
| 3 | spans by label, realignment failures, mean score by label |
| 4 | priority segments, proto-events, edges by relation |

Stage 3 span counts by label and stage 2 fuzzy merges are the two most diagnostic
numbers in the set. Make them impossible to miss in the output.

---

## 10. Tests

`tests/extraction/`. Do not require a GPU or a running vLLM server for the default suite.

**Fixtures:** two synthetic documents (a complaint with numbered paragraphs and a
caption; a short motion) plus one real anonymized document if available. Commit the
canonical text, not PDFs.

**Unit — no models:**
- Whitespace normalization is idempotent: `normalize(normalize(x)) == normalize(x)`.
- Segment offsets are valid, non-overlapping, ordered.
- CM/ECF header regex: hits, misses, malformed.
- Suffix folding and name normalization table.
- Role-anaphora resolution, including multi-defendant bare "Defendants".
- Span realignment: unique match, no match, ambiguous match.
- Artifact round-trip: `write_stage` → `read_stage` is lossless.

**Integration — mocked models:**
- `NuExtractClient` stubbed with recorded responses. Assert stage 1 and 2 produce valid
  artifacts, and that a malformed response raises rather than silently producing junk.
- Full stage 0→4 run with all models stubbed and `relex.enabled = false`.

**Marked `@pytest.mark.gpu`, excluded by default:**
- Real GLiNER over the fixture, asserting realignment failure rate is under threshold.
- Real Maverick over the fixture.

**Property test worth having:** for every span in every artifact,
`canonical_text[start:end] == span.text`. Run it over all artifacts as a final check in
the CLI. This single assertion catches most offset bugs.

---

## 11. Definition of done

- [ ] `uv run scripts/extract.py --case-id <id> --stages 0-4` completes on the fixture case.
- [ ] All five artifacts written, all validate against their Pydantic models on re-read.
- [ ] Global span validity assertion passes across every artifact.
- [ ] Pipeline completes with `relex.enabled = false`.
- [ ] Re-running without `--force` skips completed stages; with `--force` reproduces
      byte-identical artifacts.
- [ ] Stage report prints for every stage.
- [ ] Non-GPU test suite passes without a vLLM server running.
- [ ] README section: vLLM launch command, config walkthrough, how to tune
      `gliner.labels`, how to read the artifacts.

---

## 12. Open questions — surface, don't guess

Raise these rather than picking silently:

1. **NuExtract3 template mechanism.** Confirm against the current model card. If native
   templating and vLLM `guided_json` are incompatible, prefer native + Pydantic
   validation and say so in a comment.
2. **GLiNER checkpoint.** Verify the best current checkpoint for English legal text.
   The config default is a starting point.
3. **Maverick checkpoint and long-document behaviour.** It is newswire-trained. If it
   degrades badly on a 40-page complaint, report the failure mode — chunking with
   overlap is a plausible mitigation but confirm before building it.
4. **GLiNER-Relex packaging.** If it is not cleanly installable, ship stage 4 with
   `DisabledRelexRunner` and note what is needed.
5. **Whitespace policy.** Propose one, document it, and flag it explicitly — it is
   effectively irreversible once artifacts exist.
