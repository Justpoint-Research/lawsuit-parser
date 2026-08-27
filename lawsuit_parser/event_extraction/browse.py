"""Shared read-side computations for browsing event_extraction pipeline output.

Both scripts/browse_events.py (CLI) and apps/event_browser.py (Streamlit) load
the same Stage 1/2/3 artifacts - data/extraction/<case_id>/events/*.json,
written by `make extract-events` (scripts/run_event_extraction.py) - and need
the same case summary and per-document "row" derived from them. This module
is the one place that logic lives, so the two views can't drift apart.

None of this runs a model - it's pure post-hoc aggregation over what Stages
1/2/3 already wrote to disk.

A real event/timeline concept (Stage 4+) isn't built yet. Until then, the
closest thing to "browsing events" is the raw dates Stage 1 pulled out of
each document (files_scan.json's all_dates), shown alongside the actors
Stage 2 found and the LLM-generated summary Stage 3 wrote for the same
document. So the grain here is one row per *document*, not per event -
that's the actual granularity Stages 1-3 operate at.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .models import ActorsArtifact, EntitiesArtifact, Entity, ExtractedDate, FilesScan, SummariesArtifact

# Loose year match, tolerating a trailing "s" for decades ("1920s", "mid-1960s")
# - used only to give a rough min/max on top of raw date strings, which are
# never parsed into real dates elsewhere in this pipeline (see ExtractedDate).
YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})s?\b")

CAPTION_SEPARATOR_RE = re.compile(
    r"\s+-\s+(?:v|vs|versus)\.?\s+-\s+|\s+(?:v|vs|versus)\.?\s+", re.IGNORECASE
)
ET_AL_RE = re.compile(r"\bet\s+al\.?$", re.IGNORECASE)
STRIP_CHARS = " .,-"


def _clean_party_name(name: str) -> str:
    name = name.strip(STRIP_CHARS)
    name = ET_AL_RE.sub("", name).strip(STRIP_CHARS)
    return name


def parse_case_caption(caption: str) -> tuple[str, str] | None:
    """Split a short docket caption like "Judith Phillips v. Pfizer Inc. et al"
    into (plaintiff_name, defendant_name). Returns None if it doesn't split
    into a recognizable "X v. Y" pair."""
    if not caption:
        return None

    parts = CAPTION_SEPARATOR_RE.split(caption, maxsplit=1)
    if len(parts) != 2:
        return None

    plaintiff = _clean_party_name(parts[0])
    defendant = _clean_party_name(parts[1])

    if not plaintiff or not defendant:
        return None

    return plaintiff, defendant


@dataclass
class CaseSummary:
    case_id: str
    caption: str | None
    court: str | None
    num_documents: int
    plaintiffs: list[str]
    defendants: list[str]
    other_roles: dict[str, list[str]]
    products: list[str]
    num_parties: int
    num_entities: int
    num_linked: int
    temporal_count: int
    year_range: tuple[int, int] | None
    label_counts: dict[str, int]
    gliner_model: str | None
    gliner_threshold: float | None


@dataclass
class DocumentRow:
    """One scanned document, with everything Stage 1/2 found in it."""
    seq: int
    doc_id: str
    file_name: str
    document_number: str | None
    document_title: str | None
    filing_date: str | None
    filed_by: str | None
    dates: list[tuple[str, str]]  # (type, text)
    actors: list[str]
    summary: str | None = None
    entities: list[tuple[str, str, float]] = field(default_factory=list)  # (label, text, score)


class CaseArtifacts:
    """The Stage 1/2/3 artifacts for one case, loaded once and reused."""

    def __init__(
        self,
        case_id: str,
        caption: str | None,
        files_scan: FilesScan,
        actors: ActorsArtifact,
        products: ActorsArtifact,
        entities: EntitiesArtifact,
        summaries: SummariesArtifact | None = None,
    ):
        self.case_id = case_id
        self.caption = caption
        self.files_scan = files_scan
        self.actors = actors
        self.products = products
        self.entities = entities
        # Stage 3 (document summaries) is newer than the other artifacts and
        # optional here - a case extracted before it existed still browses,
        # just without a Summary column.
        self.summaries = summaries


def _events_dir(case_id: str, output_root: Path) -> Path:
    return Path(output_root) / case_id / "events"


def _load_artifact(events_dir: Path, filename: str, model_cls):
    with open(events_dir / filename, "r", encoding="utf-8") as f:
        return model_cls.model_validate(json.load(f))


def read_case_caption(case_id: str, data_root: Path) -> str | None:
    """Read the short docket caption (e.g. "X v. Y") from the case's
    DB-exported JSON under data_root (data/cases), not output_root - this is
    case-level metadata from the scraping database, produced independently of
    the extraction pipeline."""
    case_json_path = Path(data_root) / case_id / f"{case_id}.json"
    if not case_json_path.exists():
        return None

    with open(case_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data.get("case_info", {}).get("caption")


def load_case_artifacts(case_id: str, data_root: Path, output_root: Path) -> CaseArtifacts:
    """Load every Stage 1/2 artifact for a case, plus Stage 3's summaries.json
    if present. Raises FileNotFoundError if Stage 1/2 haven't been run yet
    (see `make extract-events`) - summaries.json alone is optional, since
    Stage 3 postdates cases already extracted with just Stage 1/2."""
    events_dir = _events_dir(case_id, output_root)
    try:
        summaries = _load_artifact(events_dir, "summaries.json", SummariesArtifact)
    except FileNotFoundError:
        summaries = None
    return CaseArtifacts(
        case_id=case_id,
        caption=read_case_caption(case_id, data_root),
        files_scan=_load_artifact(events_dir, "files_scan.json", FilesScan),
        actors=_load_artifact(events_dir, "actors.json", ActorsArtifact),
        products=_load_artifact(events_dir, "products.json", ActorsArtifact),
        entities=_load_artifact(events_dir, "entities.json", EntitiesArtifact),
        summaries=summaries,
    )


def has_required_artifacts(case_id: str, output_root: Path) -> bool:
    """Whether a case has the Stage 1/2 artifacts browse_events/event_browser need."""
    events_dir = _events_dir(case_id, output_root)
    required = ["files_scan.json", "actors.json", "products.json", "entities.json"]
    return all((events_dir / name).exists() for name in required)


def list_browsable_cases(output_root: Path) -> list[str]:
    """Case ids under output_root that have every artifact browsing needs."""
    output_root = Path(output_root)
    if not output_root.exists():
        return []
    return sorted(
        p.name for p in output_root.iterdir()
        if p.is_dir() and has_required_artifacts(p.name, output_root)
    )


def compute_case_summary(artifacts: CaseArtifacts) -> CaseSummary:
    """Case-level aggregation: parties by role, document/entity counts, a
    best-effort year range from extracted dates."""
    parties_by_role: dict[str, list[str]] = {}
    for actor in artifacts.actors.actors:
        parties_by_role.setdefault(actor.role, []).append(actor.canonical_name)

    # The DB caption is always available up front (independent of Stage 1's
    # NER/LLM party discovery), so use it to fill in plaintiff/defendant when
    # Stage 1 didn't find them.
    caption_parties = parse_case_caption(artifacts.caption) if artifacts.caption else None
    if caption_parties:
        cap_plaintiff, cap_defendant = caption_parties
        for role, name in (("plaintiff", cap_plaintiff), ("defendant", cap_defendant)):
            existing = parties_by_role.setdefault(role, [])
            if name not in existing:
                existing.insert(0, name)

    other_roles = {
        role: sorted(set(names)) for role, names in parties_by_role.items()
        if role not in ("plaintiff", "defendant")
    }

    # Only "filing_date"-type entries (CM/ECF-style header stamps, e-filing
    # confirmation timestamps) anchor this case's own chronology. The much
    # larger "event_date" pool is every date-shaped string regex-matched
    # anywhere in a document's body text - it also catches things with
    # nothing to do with this case's timeline: citations to old case law
    # ("Apr. 2, 1998"), and outright bad OCR/PDF text (a served-on date of
    # "7/26/2076" in one case_95 affidavit). Including those swings the
    # displayed span by decades in either direction.
    filing_date_texts = [d.text for d in artifacts.files_scan.all_dates if d.type == "filing_date"]
    years = sorted({int(m.group(1)) for t in filing_date_texts for m in YEAR_RE.finditer(t)})
    year_range = (years[0], years[-1]) if years else None

    court = None
    if artifacts.files_scan.database_metadata:
        court = artifacts.files_scan.database_metadata.court

    num_linked = sum(1 for e in artifacts.entities.entities if e.linked_actor)

    return CaseSummary(
        case_id=artifacts.case_id,
        caption=artifacts.caption,
        court=court,
        num_documents=len(artifacts.files_scan.documents),
        plaintiffs=sorted(set(parties_by_role.get("plaintiff", []))),
        defendants=sorted(set(parties_by_role.get("defendant", []))),
        other_roles=other_roles,
        products=sorted({a.canonical_name for a in artifacts.products.actors}),
        num_parties=len(artifacts.actors.actors),
        num_entities=len(artifacts.entities.entities),
        num_linked=num_linked,
        temporal_count=len(artifacts.files_scan.all_dates),
        year_range=year_range,
        label_counts=dict(artifacts.entities.entity_counts),
        gliner_model=artifacts.entities.gliner_config.model if artifacts.entities.gliner_config else None,
        gliner_threshold=artifacts.entities.gliner_config.threshold if artifacts.entities.gliner_config else None,
    )


def _dates_by_doc(files_scan: FilesScan) -> dict[str, list[ExtractedDate]]:
    by_doc: dict[str, list[ExtractedDate]] = {}
    for d in files_scan.all_dates:
        if d.doc_id:
            by_doc.setdefault(d.doc_id, []).append(d)
    return by_doc


def _entities_by_doc(entities: EntitiesArtifact) -> dict[str, list[Entity]]:
    by_doc: dict[str, list[Entity]] = {}
    for e in entities.entities:
        by_doc.setdefault(e.doc_id, []).append(e)
    return by_doc


def _summary_by_doc(summaries: SummariesArtifact | None) -> dict[str, str]:
    if summaries is None:
        return {}
    return {d.doc_id: d.summary for d in summaries.documents if d.summary}


def build_document_rows(artifacts: CaseArtifacts) -> list[DocumentRow]:
    """One DocumentRow per scanned document, in the order files_scan.json
    lists them (doc_000, doc_001, ... - source-filename order, see
    scripts/parse_case_for_extraction.py; not a date sort)."""
    dates_by_doc = _dates_by_doc(artifacts.files_scan)
    entities_by_doc = _entities_by_doc(artifacts.entities)
    summary_by_doc = _summary_by_doc(artifacts.summaries)

    rows = []
    for i, doc in enumerate(artifacts.files_scan.documents, start=1):
        doc_entities = entities_by_doc.get(doc.doc_id, [])
        actors = sorted({e.linked_actor for e in doc_entities if e.linked_actor})

        rows.append(DocumentRow(
            seq=i,
            doc_id=doc.doc_id,
            file_name=doc.file_name,
            document_number=doc.document_number,
            document_title=doc.document_title,
            filing_date=doc.filing_date,
            filed_by=doc.filed_by,
            dates=[(d.type, d.text) for d in dates_by_doc.get(doc.doc_id, [])],
            actors=actors,
            summary=summary_by_doc.get(doc.doc_id),
            entities=sorted(
                ((e.label, e.text, e.score) for e in doc_entities),
                key=lambda e: e[0],
            ),
        ))
    return rows


def collect_actor_tags(rows: list[DocumentRow]) -> list[str]:
    """Every distinct actor across all rows, for populating a tag filter."""
    return sorted({actor for row in rows for actor in row.actors})


# ---- Table formatting shared by the CLI script and the Streamlit app ----
#
# Never st.dataframe/st.table: this environment's pyarrow build segfaults
# the whole server process (confirmed directly - a bare st.dataframe call
# crashes with SIGSEGV via streamlit.testing.v1.AppTest, no rerun even
# needed) the moment a script that renders one of those Arrow-backed
# widgets runs. render_ascii_table is for the CLI (scripts/browse_events.py);
# render_html_table is for apps/event_browser.py - a real <table> element
# (proper columns/header, not monospace-in-a-code-block) built as a plain
# HTML string and shown via st.markdown(..., unsafe_allow_html=True),
# which doesn't touch Arrow at all.

EVENT_TABLE_COLUMNS = [
    ("Seq", "seq", 3),
    ("Document", "document", 9),
    ("Filename", "filename", 30),
    ("Filed", "filed", 12),
    ("Dates found", "dates", 26),
    ("Actors", "actors", 26),
    ("Title", "title", 28),
    ("Summary", "summary", 45),
]


def _compact(text: str) -> str:
    return " ".join(text.split())


def clip_text(text: str, max_len: int) -> str:
    """Collapse whitespace and truncate to max_len chars on a word boundary."""
    compact = _compact(text)
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rsplit(" ", 1)[0] + "..."


def format_actors(actors: list[str]) -> str:
    if not actors:
        return "-"
    return ", ".join(actors)


def format_dates(dates: list[tuple[str, str]]) -> str:
    if not dates:
        return "-"
    return ", ".join(f"{text} ({dtype})" for dtype, text in dates)


def document_rows_to_table_dicts(rows: list[DocumentRow]) -> list[dict]:
    return [
        {
            "seq": str(row.seq),
            "document": row.doc_id,
            "filename": row.file_name,
            "filed": row.filing_date or "-",
            "dates": format_dates(row.dates),
            "actors": format_actors(row.actors),
            "title": clip_text(row.document_title, max_len=60) if row.document_title else "-",
            "summary": clip_text(row.summary, max_len=400) if row.summary else "-",
        }
        for row in rows
    ]


def render_ascii_table(columns: list[tuple[str, str, int]], rows: list[dict]) -> str:
    """Render rows as a fixed-width ASCII table, wrapping long cell text
    across multiple lines within a row rather than truncating it away.
    Returns the table as one string (print it, or show it in st.code())."""
    import textwrap

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


def render_html_table(columns: list[tuple[str, str, int]], rows: list[dict]) -> str:
    """Render rows as a real HTML <table> - proper columns and a header
    row, cell text wrapping naturally instead of being pre-wrapped into
    fixed-width lines. Column widths are approximate (in `ch` units, i.e.
    roughly one monospace character) rather than strict, so long unbroken
    strings (e.g. the hashed source filenames) still wrap instead of
    forcing a scrollbar.

    Returns one HTML string - render it with
    st.markdown(html, unsafe_allow_html=True). Never use st.dataframe/
    st.table here - see the module-level note above render_ascii_table."""
    import html as html_module

    def esc(value: str) -> str:
        return html_module.escape(str(value)).replace("\n", "<br>")

    colgroup = "".join(f'<col style="width:{w}ch">' for _, _, w in columns)
    header_cells = "".join(
        f'<th style="text-align:left; padding:6px 10px; border-bottom:2px solid rgba(128,128,128,0.4);">{esc(label)}</th>'
        for label, _, _ in columns
    )
    body_rows = []
    for row in rows:
        cells = "".join(
            f'<td style="text-align:left; vertical-align:top; padding:6px 10px; '
            f'border-bottom:1px solid rgba(128,128,128,0.25); word-wrap:break-word; '
            f'overflow-wrap:break-word;">{esc(row.get(key, ""))}</td>'
            for _, key, _ in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")

    return (
        '<table style="width:100%; border-collapse:collapse; font-size:0.9em;">'
        f"<colgroup>{colgroup}</colgroup>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )
