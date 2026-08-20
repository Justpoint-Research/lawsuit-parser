"""Shared read-side computations for browsing extracted event-candidate segments.

Both scripts/browse_events.py (CLI) and apps/event_browser.py (Streamlit) load
the same stage 0-4 artifacts and need the same case summary and per-segment
"event row" (timestamp/actors/text) derived from them. This module is the one
place that logic lives, so the two views can't drift apart.

None of this runs a model - it's pure post-hoc aggregation over what stages
0-4 already wrote to disk.
"""

import re
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import (
    SegmentsArtifact,
    MetadataArtifact,
    RegistryArtifact,
    SpansArtifact,
    ProtoEventsArtifact,
)
from .store import ArtifactStore
from .spans import parse_case_caption

# Loose year match, tolerating a trailing "s" for decades ("1920s", "mid-1960s")
# - used only to give a rough min/max on top of raw temporal-expression
# strings, which are never parsed into real dates elsewhere in the pipeline
# (see metadata.py).
YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})s?\b")


@dataclass
class CaseSummary:
    case_id: str
    caption: str | None
    court: str | None
    num_documents: int
    plaintiffs: list[str]
    defendants: list[str]
    other_roles: dict[str, list[str]]
    num_registry_parties: int
    num_mentions: int
    num_unresolved: int
    temporal_count: int
    year_range: tuple[int, int] | None
    label_counts: dict[str, int]
    num_priority_segments: int
    num_proto_events: int


@dataclass
class EventRow:
    """One priority segment, with everything needed to display or filter it."""
    seq: int
    seg_id: str
    doc_id: str
    page: int | None
    section_type: str
    timestamps: list[str]
    actors: list[str]
    text: str
    entities: list[tuple[str, str, float]] = field(default_factory=list)  # (label, text, score)


class CaseArtifacts:
    """The stage 0-4 artifacts for one case, loaded once and reused."""

    def __init__(
        self,
        case_id: str,
        store: ArtifactStore,
        segments: SegmentsArtifact,
        metadata: MetadataArtifact,
        registry: RegistryArtifact,
        spans: SpansArtifact,
        protoevents: ProtoEventsArtifact,
    ):
        self.case_id = case_id
        self.store = store
        self.segments = segments
        self.metadata = metadata
        self.registry = registry
        self.spans = spans
        self.protoevents = protoevents


def load_case_artifacts(case_id: str, data_root: Path) -> CaseArtifacts:
    """Load every stage 0-4 artifact for a case. Raises FileNotFoundError if
    a stage hasn't been run yet (see scripts/extract.py)."""
    store = ArtifactStore(case_id, Path(data_root))
    return CaseArtifacts(
        case_id=case_id,
        store=store,
        segments=store.read_stage("00_segments", SegmentsArtifact),
        metadata=store.read_stage("01_metadata", MetadataArtifact),
        registry=store.read_stage("02_registry", RegistryArtifact),
        spans=store.read_stage("03_spans", SpansArtifact),
        protoevents=store.read_stage("04_protoevents", ProtoEventsArtifact),
    )


def has_required_stages(case_id: str, data_root: Path) -> bool:
    """Whether a case has all the stage artifacts browse_events/event_browser need."""
    stages_dir = Path(data_root) / case_id / "stages"
    required = ["00_segments", "01_metadata", "02_registry", "03_spans", "04_protoevents"]
    return all((stages_dir / f"{stage}.json").exists() for stage in required)


def has_documents(case_id: str, data_root: Path) -> bool:
    """Whether a case has any docling.json documents."""
    case_dir = Path(data_root) / case_id

    # Current structure: docling outputs mirrored under docling/documents/
    # (parse_all_pdfs.py output structure, see lawsuit_parser.parsers.batch.get_docling_dir)
    docling_documents_dir = case_dir / "docling" / "documents"
    if docling_documents_dir.exists() and list(docling_documents_dir.glob("*.docling.json")):
        return True

    # Older parse_all_pdfs.py output structure: docling.json alongside the PDF
    documents_dir = case_dir / "documents"
    if documents_dir.exists() and list(documents_dir.glob("*.docling.json")):
        return True

    # Check flat structure (legacy)
    return bool(list(case_dir.glob("*.docling.json")))


def list_browsable_cases(data_root: Path) -> list[str]:
    """Case ids under data_root that have every stage artifact browsing needs and at least one document."""
    data_root = Path(data_root)
    if not data_root.exists():
        return []
    return sorted(
        p.name for p in data_root.iterdir()
        if p.is_dir() and has_documents(p.name, data_root) and has_required_stages(p.name, data_root)
    )


def compute_case_summary(artifacts: CaseArtifacts) -> CaseSummary:
    """Case-level aggregation: parties by role, document/entity counts, a
    best-effort year range from temporal expressions, and event counts."""
    caption = artifacts.store.read_case_caption()

    doc_ids = {d.doc_id for d in artifacts.segments.documents}

    parties_by_role: dict[str, list[str]] = {}
    for party in artifacts.registry.parties:
        for role in party.roles:
            parties_by_role.setdefault(role, []).append(party.canonical_name)

    # Registry parties come from stage 1/2 (NuExtract + coref) and can be
    # empty if those stages found nothing. The DB caption is always available
    # up front, so use it to fill in plaintiff/defendant when the registry
    # didn't resolve them.
    caption_parties = parse_case_caption(caption) if caption else None
    if caption_parties:
        cap_plaintiff, cap_defendant = caption_parties
        for role, name in (("plaintiff", cap_plaintiff), ("defendant", cap_defendant)):
            existing = parties_by_role.setdefault(role, [])
            if name not in existing:
                existing.insert(0, name)

    label_counts = dict(Counter(s.label for s in artifacts.spans.spans))

    temporal_texts = [s.span.text for s in artifacts.spans.spans if s.label == "temporal expression"]
    years = sorted({int(m.group(1)) for t in temporal_texts for m in YEAR_RE.finditer(t)})
    year_range = (years[0], years[-1]) if years else None

    court = next((m.court for m in artifacts.metadata.documents if m.court), None)

    other_roles = {
        role: sorted(set(names)) for role, names in parties_by_role.items()
        if role not in ("plaintiff", "defendant")
    }

    return CaseSummary(
        case_id=artifacts.case_id,
        caption=caption,
        court=court,
        num_documents=len(doc_ids),
        plaintiffs=sorted(set(parties_by_role.get("plaintiff", []))),
        defendants=sorted(set(parties_by_role.get("defendant", []))),
        other_roles=other_roles,
        num_registry_parties=len(artifacts.registry.parties),
        num_mentions=len(artifacts.registry.mentions),
        num_unresolved=len(artifacts.registry.unresolved),
        temporal_count=len(temporal_texts),
        year_range=year_range,
        label_counts=label_counts,
        num_priority_segments=len(artifacts.protoevents.priority_segments),
        num_proto_events=len(artifacts.protoevents.proto_events),
    )


def _build_spans_by_seg(spans: SpansArtifact) -> dict[str, list[tuple[str, str, float]]]:
    spans_by_seg: dict[str, list[tuple[str, str, float]]] = {}
    for gs in spans.spans:
        spans_by_seg.setdefault(gs.seg_id, []).append((gs.label, gs.span.text, gs.score))
    return spans_by_seg


def _build_mentions_by_seg(segments: SegmentsArtifact, registry: RegistryArtifact) -> dict[str, set[str]]:
    party_names = {p.party_id: p.canonical_name for p in registry.parties}
    mentions_by_seg: dict[str, set[str]] = {}
    for m in registry.mentions:
        for seg in segments.segments:
            if (
                seg.doc_id == m.span.doc_id
                and seg.char_start <= m.span.char_start
                and seg.char_end >= m.span.char_end
            ):
                name = party_names.get(m.party_id, m.party_id)
                mentions_by_seg.setdefault(seg.seg_id, set()).add(name)
                break
    return mentions_by_seg


def _extract_timestamps(seg_id: str, spans_by_seg: dict) -> list[str]:
    times = [text for label, text, _score in spans_by_seg.get(seg_id, []) if label == "temporal expression"]
    return list(dict.fromkeys(times))  # dedupe, keep first-seen order


def _extract_actors(seg_id: str, mentions_by_seg: dict, spans_by_seg: dict) -> list[str]:
    """Actors mentioned in a segment: resolved registry party names first
    (most reliable), then role tags from the case-specific plaintiff/defendant
    GLiNER labels, then any other raw "party or organization" span text."""
    actors: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            actors.append(name)

    for name in sorted(mentions_by_seg.get(seg_id, ())):
        add(name)

    for label, text, _score in spans_by_seg.get(seg_id, []):
        if label.startswith("plaintiff ("):
            add("Plaintiff")
        elif label.startswith("defendant ("):
            add("Defendant")
        elif label == "party or organization":
            add(text)

    return actors


def build_event_rows(artifacts: CaseArtifacts) -> list[EventRow]:
    """One EventRow per priority segment (stage 4's event candidates)."""
    segments_by_id = {s.seg_id: s for s in artifacts.segments.segments}
    spans_by_seg = _build_spans_by_seg(artifacts.spans)
    mentions_by_seg = _build_mentions_by_seg(artifacts.segments, artifacts.registry)

    rows = []
    for i, seg_id in enumerate(artifacts.protoevents.priority_segments, start=1):
        seg = segments_by_id.get(seg_id)
        if seg is None:
            continue
        text = artifacts.store.read_canonical_text(seg.doc_id)[seg.char_start : seg.char_end]

        rows.append(EventRow(
            seq=i,
            seg_id=seg_id,
            doc_id=seg.doc_id,
            page=seg.page,
            section_type=seg.section_type,
            timestamps=_extract_timestamps(seg_id, spans_by_seg),
            actors=_extract_actors(seg_id, mentions_by_seg, spans_by_seg),
            text=text,
            entities=sorted(spans_by_seg.get(seg_id, []), key=lambda e: e[0]),
        ))
    return rows


def collect_actor_tags(rows: list[EventRow]) -> list[str]:
    """Every distinct actor across all rows, for populating a tag filter."""
    return sorted({actor for row in rows for actor in row.actors})


# ---- Table formatting shared by the CLI script and the Streamlit app ----
#
# Deliberately plain ASCII/monospace, not a DataFrame/Arrow-backed table:
# this environment's pyarrow build segfaults (libarrow.so) the moment
# Streamlit re-runs a script that has ever called st.dataframe/st.table, so
# apps/event_browser.py renders this same text inside st.code() instead of
# using Streamlit's native table widgets.

EVENT_TABLE_COLUMNS = [
    ("Seq", "seq", 3),
    ("Timestamp", "timestamp", 14),
    ("Actors", "actors", 22),
    ("Summary", "summary", 28),
    ("Document", "document", 9),
    ("Page", "page", 4),
    ("Text (source excerpt)", "text", 45),
]


def _compact(text: str) -> str:
    return " ".join(text.split())


def clip_text(text: str, max_len: int) -> str:
    """Collapse whitespace and truncate to max_len chars on a word boundary.

    No summarization model is wired into the pipeline, so a "Summary" column
    is a shorter clip of the same source text, not generated text.
    """
    compact = _compact(text)
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rsplit(" ", 1)[0] + "..."


def format_actors(actors: list[str], max_actors: int = 4) -> str:
    if not actors:
        return "-"
    if len(actors) > max_actors:
        return ", ".join(actors[:max_actors]) + f" (+{len(actors) - max_actors} more)"
    return ", ".join(actors)


def event_rows_to_table_dicts(rows: list[EventRow]) -> list[dict]:
    return [
        {
            "seq": str(row.seq),
            "timestamp": "; ".join(row.timestamps) if row.timestamps else "-",
            "actors": format_actors(row.actors),
            "summary": clip_text(row.text, max_len=70),
            "document": row.doc_id,
            "page": str(row.page) if row.page is not None else "-",
            "text": clip_text(row.text, max_len=400),
        }
        for row in rows
    ]


def render_ascii_table(columns: list[tuple[str, str, int]], rows: list[dict]) -> str:
    """Render rows as a fixed-width ASCII table, wrapping long cell text
    across multiple lines within a row rather than truncating it away.
    Returns the table as one string (print it, or show it in st.code())."""
    widths = [w for _, _, w in columns]

    def rule(char: str = "-") -> str:
        return "+" + "+".join(char * (w + 2) for w in widths) + "+"

    def render_row(values: list[str]) -> list[str]:
        wrapped = [textwrap.wrap(str(v), width=w) or [""] for v, w in zip(values, widths)]
        height = max(len(w) for w in wrapped)
        lines = []
        for i in range(height):
            cells = [
                (wrapped[c][i] if i < len(wrapped[c]) else "").ljust(widths[c])
                for c in range(len(columns))
            ]
            lines.append("| " + " | ".join(cells) + " |")
        return lines

    lines = [rule("=")]
    lines.extend(render_row([header for header, _, _ in columns]))
    lines.append(rule("="))
    for row in rows:
        lines.extend(render_row([row.get(key, "") for _, key, _ in columns]))
        lines.append(rule("-"))

    return "\n".join(lines)
