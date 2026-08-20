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
    header: str | None = None
    cm_ecf: CMECFMetadata | None = None


class DocumentMetadata(BaseModel):
    """Complete metadata for a single document."""

    doc_id: str
    file_name: str
    document_number: str | None = None
    document_title: str | None = None
    filing_date: str | None = None
    filed_by: str | None = None
    pdf_metadata: PDFMetadata | None = None
    docling_metadata: DoclingMetadata | None = None
    extracted_dates: list[ExtractedDate] = Field(default_factory=list)


class PartyDiscovered(BaseModel):
    """A party discovered during metadata extraction."""

    name: str
    role: str
    source: str = Field(description="Source of discovery (e.g., 'database', 'docling_header', 'pdf_metadata')")
    aliases: list[str] = Field(default_factory=list)


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
    Docling files, plus all dates extracted from any source.
    """

    case_id: str
    scan_timestamp: datetime = Field(default_factory=datetime.now)
    database_metadata: DatabaseMetadata | None = None
    documents: list[DocumentMetadata] = Field(default_factory=list)
    parties_discovered: list[PartyDiscovered] = Field(default_factory=list)
    all_dates: list[ExtractedDate] = Field(default_factory=list, description="All dates from all sources")


class Actor(BaseModel):
    """An actor (party) in the case with GLiNER label configuration."""

    canonical_name: str
    role: str
    aliases: list[str] = Field(default_factory=list)
    gliner_label: str = Field(description="Label to use for GLiNER detection")


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
    """An entity detected by GLiNER."""

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
    model_config: ModelConfig
    entities: list[Entity] = Field(default_factory=list)
    entity_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Count of entities by label"
    )


# ============================================================================
# Future: Event Timeline Models (placeholder)
# ============================================================================

class Event(BaseModel):
    """A legal event extracted from the case."""

    event_id: str
    event_type: str
    description: str
    actors: list[str] = Field(default_factory=list, description="Actors involved in the event")
    temporal_expression: str | None = None
    date_parsed: str | None = None
    source_doc_id: str
    char_start: int
    char_end: int
    confidence: float = Field(ge=0.0, le=1.0)


class EventTimeline(BaseModel):
    """Complete timeline of events for a case."""

    case_id: str
    events: list[Event] = Field(default_factory=list)
    timeline_generated: datetime = Field(default_factory=datetime.now)
