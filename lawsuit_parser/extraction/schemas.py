"""Pydantic schemas for the event extraction pipeline.

All models are frozen where practical to ensure immutability.
Every extracted span carries (doc_id, char_start, char_end) and the substring
at those offsets must equal the recorded surface form.
"""

from typing import Literal
from pydantic import BaseModel, field_validator


class Span(BaseModel):
    """A text span with character offsets into canonical text.

    The text field MUST equal canonical_text[char_start:char_end].
    This invariant is validated in stage functions.
    """
    doc_id: str
    char_start: int
    char_end: int
    text: str

    model_config = {"frozen": True}


class DocumentRef(BaseModel):
    """Reference to a document in the case."""
    doc_id: str
    file_name: str | None = None

    model_config = {"frozen": True}


# ---- Stage 0: Segmentation ----

class Segment(BaseModel):
    """A structural segment of a document (paragraph, heading, signature block, etc.)."""
    seg_id: str
    doc_id: str
    page: int | None
    para_label: str | None  # "¶ 42", "II.B", None
    section_type: Literal[
        "caption", "body", "heading", "signature",
        "certificate_of_service", "exhibit", "other"
    ]
    char_start: int
    char_end: int

    model_config = {"frozen": True}


class SegmentsArtifact(BaseModel):
    """Output of stage 0: canonical text segmentation."""
    case_id: str
    segments: list[Segment]
    documents: list[DocumentRef]


# ---- Stage 1: Metadata Extraction ----

class PartySeed(BaseModel):
    """A party extracted from the document caption."""
    name: str
    role: Literal[
        "plaintiff", "defendant", "intervenor", "third_party",
        "amicus", "counsel", "court", "other"
    ]
    short_name: str | None = None

    model_config = {"frozen": True}


class DocumentMetadata(BaseModel):
    """Metadata extracted from a single document."""
    doc_id: str
    court: str | None = None
    case_number: str | None = None
    document_title: str | None = None
    document_number: str | None = None
    filing_date_raw: str | None = None  # SURFACE STRING ONLY — no parsing
    signature_date_raw: str | None = None
    dct_raw: str | None = None  # chosen anchor: filing date, else signature date
    filed_by: str | None = None
    parties: list[PartySeed]
    source_spans: dict[str, Span]  # field name -> provenance


class MetadataArtifact(BaseModel):
    """Output of stage 1: document metadata."""
    case_id: str
    documents: list[DocumentMetadata]


# ---- Stage 2: Party Registry and Coreference ----

class Party(BaseModel):
    """A canonical party in the case."""
    party_id: str  # "p_001"
    canonical_name: str
    party_type: Literal["individual", "organization", "government", "court", "unknown"]
    roles: list[str]
    aliases: list[str]


class PartyMention(BaseModel):
    """A mention of a party in the text, linked to the registry."""
    span: Span
    party_id: str
    source: Literal[
        "caption", "alias_definition", "exact_match", "fuzzy_match",
        "role_anaphora", "coref"
    ]
    confidence: float

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v


class RegistryArtifact(BaseModel):
    """Output of stage 2: party registry and mention index."""
    case_id: str
    parties: list[Party]
    mentions: list[PartyMention]  # THE mention index
    unresolved: list[Span]  # entity-like spans that failed to link


# ---- Stage 3: Span Sweep with GLiNER ----

class GlinerSpan(BaseModel):
    """A span extracted by GLiNER with its label and score."""
    span: Span
    seg_id: str
    label: str
    score: float

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")
        return v


class SpansArtifact(BaseModel):
    """Output of stage 3: exhaustive span sweep."""
    case_id: str
    label_set: list[str]  # exactly what was prompted
    model_id: str
    threshold: float
    spans: list[GlinerSpan]
    realignment_failures: int


# ---- Stage 4: Proto-events ----

class ProtoEventEdge(BaseModel):
    """A typed edge from a proto-event predicate to an argument."""
    relation: Literal["has_date", "actor", "affected", "at_location", "instrument"]
    target: Span
    score: float

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")
        return v

    model_config = {"frozen": True}


class ProtoEvent(BaseModel):
    """A proto-event: predicate + typed edges to arguments."""
    proto_id: str
    seg_id: str
    predicate: Span
    edges: list[ProtoEventEdge]


class ProtoEventsArtifact(BaseModel):
    """Output of stage 4: proto-events."""
    case_id: str
    enabled: bool
    proto_events: list[ProtoEvent]
    priority_segments: list[str]  # seg_ids, emitted even when relex disabled
