"""Pydantic data models for event extraction pipeline."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ============================================================================
# Stage 1: Metadata Extraction Models
# ============================================================================

class ExtractedDate(BaseModel):
    """A date extracted from a document."""

    text: str = Field(description="Raw date text as it appears in the source")
    source: str = Field(description="Source of the date (e.g., 'cm_ecf_header', 'document_body', 'pdf_metadata')")
    type: str = Field(description="Type of date (e.g., 'filing_date', 'event_date', 'creation_date')")
    doc_id: str | None = Field(default=None, description="Document ID if from a specific document")
    char_start: int | None = Field(default=None, description="Character offset in canonical text")
    char_end: int | None = Field(default=None, description="Character offset in canonical text")


class CMECFMetadata(BaseModel):
    """CM/ECF header metadata extracted from document headers."""

    case_number: str | None = None
    document_number: str | None = None
    filing_date: str | None = None
    page_info: str | None = None


class PDFMetadata(BaseModel):
    """Metadata extracted from PDF file properties."""

    created: datetime | None = None
    modified: datetime | None = None
    pages: int | None = None
    author: str | None = None
    title: str | None = None


class DoclingMetadata(BaseModel):
    """Metadata extracted from Docling parsed files."""

    title: str | None = None
    title_candidates: list[str] = Field(
        default_factory=list,
        description="Heuristic candidate title lines from page 1 (see "
                     "utils.find_title_candidates), used to arbitrate the final "
                     "document title via an LLM - see "
                     "llm_validation.identify_document_title_with_llm",
    )
    header: str | None = None
    cm_ecf: CMECFMetadata | None = None
    document_signature: str | None = Field(
        default=None,
        description="This document's own filing/document number as stamped by its e-filing "
                     "system (e.g. '11' from 'NYSCEF DOC. NO. 11') - not tied to any one "
                     "state/system, see utils.extract_document_signature",
    )


class ConfirmationMetadata(BaseModel):
    """Metadata extracted from a document's e-filing confirmation notice.

    Confirmations live alongside documents under a case's confirmations/
    directory, same file name, different content: an acknowledgement of who
    filed the document, with whom, and when - not the document itself. See
    lawsuit_parser.parsers.batch and BaseStage.get_confirmations_dir.
    """

    assigned_judge: str | None = None
    filer_name: str | None = None
    filer_email: str | None = None
    filer_phone: str | None = None
    notice_timestamp: str | None = None


class DocumentReference(BaseModel):
    """A citation of one document's filing/document number found in
    another document's text - e.g. "see Doc. No. 7" found in doc_id
    doc_012. Not tied to any one e-filing system's citation wording.

    See BaseStage.get_documents_dir / Stage1Metadata for how doc_number is
    resolved to doc_id: every document's own number is collected first (its
    "signature", see DoclingMetadata.document_signature), then every other
    document's citations are matched against that map.
    """

    doc_number: str = Field(description="The cited document number, as it appears (e.g. '7')")
    doc_id: str | None = Field(
        default=None,
        description="Resolved doc_id of the referenced document, if doc_number matches a "
                     "document in this case's own document set; None if it couldn't be resolved "
                     "(e.g. it cites a document outside this case's scanned set)",
    )
    char_start: int | None = Field(default=None, description="Character offset of the citation in canonical text")
    char_end: int | None = Field(default=None, description="Character offset of the citation in canonical text")


class DocumentMetadata(BaseModel):
    """Complete metadata for a single document."""

    doc_id: str
    file_name: str
    document_number: str | None = Field(
        default=None,
        description="This document's own filing/document number (its 'signature') - from a "
                     "federal CM/ECF header or a state e-filing system's own document-number "
                     "stamp - used to resolve other documents' citations of it (see referenced_by)",
    )
    document_title: str | None = None
    filing_date: str | None = None
    filed_by: str | None = None
    pdf_metadata: PDFMetadata | None = None
    docling_metadata: DoclingMetadata | None = None
    confirmation_metadata: ConfirmationMetadata | None = None
    extracted_dates: list[ExtractedDate] = Field(default_factory=list)
    referenced_documents: list[DocumentReference] = Field(
        default_factory=list,
        description="Other documents this one cites by document number, found in its own text",
    )
    referenced_by: list[str] = Field(
        default_factory=list,
        description="doc_ids of documents in this case whose text cites this document's number",
    )


class DatabaseMetadata(BaseModel):
    """Metadata extracted from the database."""

    case_number: str | None = None
    court: str | None = None
    plaintiff: list[str] = Field(default_factory=list)
    defendant: list[str] = Field(default_factory=list)
    case_filed_date: str | None = None
    status: str | None = None


class FilesScan(BaseModel):
    """Complete output of Stage 1 metadata extraction.

    This artifact contains all metadata gathered from database, PDFs, and
    Docling files, plus all dates extracted from any source. Actor/party
    identification lives in the separate actors.json artifact (see Actor,
    ActorsArtifact below).
    """

    case_id: str
    scan_timestamp: datetime = Field(default_factory=datetime.now)
    database_metadata: DatabaseMetadata | None = None
    documents: list[DocumentMetadata] = Field(default_factory=list)
    all_dates: list[ExtractedDate] = Field(default_factory=list, description="All dates from all sources")


class Actor(BaseModel):
    """A named or role-based entity relevant to the case: a party or role
    (plaintiff, defendant, judge, court clerk, counsel, witness) OR an
    accused product (drug, medical device, cosmetic product, chemical
    substance - see products.json / Stage1Metadata's product-identification
    step). Both are "actors" in the sense used here: something GLiNER should
    have a dedicated detection label for.

    Named entries (is_named=True) carry a specific name/product found in
    the database, document text, or an LLM read of the case's pleadings.
    Unnamed entries are generic role placeholders (e.g. "Witness") added so
    GLiNER still has a label to search for even when no specific individual
    is known in advance.
    """

    canonical_name: str = Field(description="Name, or a generic role designation when unnamed (e.g. 'Witness')")
    role: str = Field(
        description="Party role (plaintiff, defendant, judge, court_clerk, counsel, witness, "
                     "attorney) or product type (drug, medical_device, cosmetic_product, "
                     "chemical_substance, other_product)"
    )
    is_named: bool = Field(default=True, description="False for generic role placeholders with no known individual")
    source: str = Field(description="Where discovered: 'database', 'caption', 'confirmation', 'llm', or 'generic'")
    aliases: list[str] = Field(default_factory=list)
    doc_ids: list[str] = Field(default_factory=list, description="Documents this actor was found in")
    attributed_to: list[str] = Field(
        default_factory=list,
        description="For an accused product: canonical name(s) of the defendant(s) it's "
                     "attributed to (its manufacturer/seller/distributor). Empty for party/role actors.",
    )
    gliner_label: str | None = Field(
        default=None,
        description="Label used for GLiNER detection - unset in actors.json/products.json, "
                     "assigned when building gliner_config.json",
    )


class ActorsArtifact(BaseModel):
    """Stage 1 output: every actor identified in the case from the
    database and PDF/confirmation text, with a clear name/designation and
    role. This is the roster gliner_config.json's dynamic labels are built
    from (see Stage1Metadata._generate_gliner_config).

    Used for two separate artifacts sharing this same shape: actors.json
    (parties/roles) and products.json (accused products - see
    Stage1Metadata's product-identification step).
    """

    case_id: str
    extraction_timestamp: datetime = Field(default_factory=datetime.now)
    actors: list[Actor] = Field(default_factory=list)


class GLiNERLabels(BaseModel):
    """Label configuration for GLiNER."""

    static: list[str] = Field(description="Static labels for general entity types")
    dynamic: list[str] = Field(description="Dynamic labels for specific actors")


class GLiNERConfig(BaseModel):
    """Configuration for GLiNER entity detection (Stage 2).

    This artifact defines the GLiNER model, labels, and actors to detect
    based on the metadata discovered in Stage 1.
    """

    model: str = Field(default="urchade/gliner_multi-v2.1")
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    batch_size: int = Field(default=8, gt=0)
    labels: GLiNERLabels
    actors: list[Actor] = Field(default_factory=list)


# ============================================================================
# Stage 2: Entity Detection Models
# ============================================================================

class Entity(BaseModel):
    """An entity detected either by GLiNER or by the gazetteer pass."""

    entity_id: str
    text: str
    label: str
    score: float = Field(ge=0.0, le=1.0)
    doc_id: str
    char_start: int
    char_end: int
    linked_actor: str | None = Field(
        default=None,
        description="Canonical name of actor if this entity was linked to a known actor"
    )
    context: str | None = Field(
        default=None,
        description="Surrounding text for context (optional)"
    )
    detection_method: str = Field(
        default="gliner",
        description="'gliner' (model detection) or 'gazetteer' (exact regex match on a known "
                     "actor's name/alias, added as a recall backstop for spans GLiNER's "
                     "threshold missed - see Stage2GLiNER._gazetteer_entities)",
    )


class ModelConfig(BaseModel):
    """GLiNER model configuration used for extraction."""

    model: str
    threshold: float


class EntitiesArtifact(BaseModel):
    """Complete output of Stage 2 entity detection.

    This artifact contains all entities detected by GLiNER across all
    documents in the case.
    """

    case_id: str
    extraction_timestamp: datetime = Field(default_factory=datetime.now)
    gliner_config: ModelConfig
    entities: list[Entity] = Field(default_factory=list)
    entity_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Count of entities by label"
    )


# ============================================================================
# Stage 3: Document Summary Models
# ============================================================================

class DocumentSummary(BaseModel):
    """A short, LLM-generated summary of one document's core purpose - why
    it exists / what it accomplishes, not a description of its contents."""

    doc_id: str
    file_name: str
    summary: str | None = Field(
        default=None,
        description="1-3 sentence summary of the document's core purpose, or "
                     "None if undeterminable or the LLM backend was unreachable",
    )
    model: str | None = Field(default=None, description="LLM model tag used to generate the summary")


class SummariesArtifact(BaseModel):
    """Stage 3 output: a short summary for every document in the case."""

    case_id: str
    extraction_timestamp: datetime = Field(default_factory=datetime.now)
    documents: list[DocumentSummary] = Field(default_factory=list)


# ============================================================================
# Stage 4: Date Clustering Models
# ============================================================================

class DateEntry(BaseModel):
    """One ExtractedDate (see Stage 1), carried into Stage 4 with a real
    parsed value alongside its unparsed surface text."""

    date_id: str
    text: str = Field(description="Raw date text, unchanged from Stage 1's ExtractedDate.text")
    parsed_date: datetime | None = Field(
        default=None,
        description="Best-effort real parse of `text` (see utils.parse_date_loosely) - "
                     "None if the string didn't parse as a date at all",
    )
    date_type: str = Field(description="Carried over from ExtractedDate.type (filing_date/event_date/...)")
    source: str = Field(description="Carried over from ExtractedDate.source")
    doc_id: str
    char_start: int | None = None
    char_end: int | None = None


class DateCluster(BaseModel):
    """Every DateEntry found in one document paragraph (see
    utils.split_paragraphs), plus the actors/products already linked to
    entities GLiNER found in that same paragraph - the unit Stage 5 turns
    into one or more Event records.

    Dates with no char offset (e.g. a confirmation notice's timestamp,
    never regex-matched against document text - see Stage1Metadata) can't
    be placed in a paragraph; each becomes its own single-date cluster with
    no citation/candidate_actors.
    """

    cluster_id: str
    doc_id: str
    char_start: int | None = None
    char_end: int | None = None
    citation: str | None = Field(
        default=None,
        description="The paragraph's text, with each contained date substring wrapped in ** "
                     "markers - same convention as Entity.context",
    )
    dates: list[DateEntry] = Field(default_factory=list)
    candidate_actors: list[str] = Field(
        default_factory=list,
        description="Canonical linked_actor names of entities.json entries found in this same "
                     "paragraph (people, defendants, products - never a static/unlinked label). "
                     "Stage 5 constrains its LLM's 'who was involved' answer to this list.",
    )


class DatesArtifact(BaseModel):
    """Stage 4 output: every case date, parsed and grouped into
    same-paragraph clusters with their candidate actors."""

    case_id: str
    extraction_timestamp: datetime = Field(default_factory=datetime.now)
    clusters: list[DateCluster] = Field(default_factory=list)


# ============================================================================
# Stage 5: Event Timeline Models
# ============================================================================

class Event(BaseModel):
    """One dated occurrence from Stage 4's dates.json, in graph-ready form
    (actors are canonical names entities.json already resolved, never
    invented). Populated one of two ways, per [stage_5].use_llm:
    - False (default, deterministic - no LLM call): `description` is a
      direct quote (the sentence containing the date, plus one sentence of
      context on each side), `event_type`/`outcome` are left unset, and
      `actors` is the source DateCluster's full candidate_actors (no
      per-event curation).
    - True: an LLM reads the cluster's citation and fills in `event_type`/
      `description`/`outcome`, and curates `actors` down to who was
      actually involved - constrained to candidate_actors via a
      JSON-schema enum so it can never name someone GLiNER didn't already
      resolve. Much slower (one LLM call, or one call per batch - see
      [stage_5].batch_size - per distinct cluster)."""

    event_id: str
    cluster_id: str = Field(description="The DateCluster (dates.json) this event was synthesized from")
    event_type: str | None = Field(default=None, description="Only set when [stage_5].use_llm is true")
    description: str
    outcome: str | None = Field(default=None, description="What the event resulted in, if the text states one")
    actors: list[str] = Field(
        default_factory=list,
        description="Canonical linked_actor names involved - each one is a name from the "
                     "source DateCluster's candidate_actors, never LLM-invented",
    )
    dates: list[str] = Field(default_factory=list, description="Raw date text(s) (DateEntry.text) this event covers")
    date_parsed: datetime | None = Field(default=None, description="Earliest parsed date among `dates`, if any parsed")
    source_doc_id: str
    char_start: int | None = None
    char_end: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class EventTimeline(BaseModel):
    """Stage 5 output: complete timeline of events for a case."""

    case_id: str
    extraction_timestamp: datetime = Field(default_factory=datetime.now)
    events: list[Event] = Field(default_factory=list)


class StampDate(BaseModel):
    """A Stage 4 DateEntry whose text couldn't be located anywhere in its
    document's canonical body text - in practice, almost always a page
    header/footer/e-filing stamp date (Stage 1's docling_header/
    docling_first_page sources - see
    stage_1_metadata._extract_from_docling), which is deliberately scanned
    only once, from page 1, rather than re-processed on every page it
    repeats on, and so has no counterpart anywhere in the canonical body
    text. With nothing to quote or synthesize an Event from, Stage 5 sets
    it aside here instead of putting it in events.json."""

    cluster_id: str
    doc_id: str
    date_id: str
    text: str
    parsed_date: datetime | None = None
    date_type: str
    source: str


class StampDatesArtifact(BaseModel):
    """Stage 5 output: dates set aside as stamp-only (see StampDate)."""

    case_id: str
    extraction_timestamp: datetime = Field(default_factory=datetime.now)
    dates: list[StampDate] = Field(default_factory=list)
