"""Interactive case graph (documents, actors, products, events) for the
Streamlit event browser (apps/event_browser.py).

Builds a networkx.DiGraph from the same CaseArtifacts event_browser.py
already loads (see browse.py.load_case_artifacts), plus Stage 4/5 output
(dates.json/events.json) when present, and renders it as a self-contained
pyvis HTML fragment - never st.dataframe/st.table's Arrow path (see
browse.py's module docstring), pyvis's vis.js output doesn't touch Arrow
at all.

Node types: document, actor (colored by role), product, event (optional -
see include_events; a case can have 200+ events, so it's off by default).

Edge types:
- document --mentions--> actor/product: from entities.json's linked_actor,
  the same per-document linkage the document table already shows. Deduped
  to one edge per (document, actor) pair regardless of how many times that
  actor was mentioned in the document.
- document --cites--> document: Stage 1's resolved referenced_documents
  (see DocumentReference.doc_id) - only edges with a resolved doc_id are
  drawn; an unresolved citation means the cited document number isn't in
  this case's own scanned document set (common - e.g. an original
  complaint that was superseded and never itself downloaded).
- event --from--> document: Event.source_doc_id.
- event --involves--> actor: Event.actors. In quote mode ([stage_5].use_llm
  = false, the current default - see stage_5_events.py), this is every
  candidate actor near the date, not curated to who the event is actually
  about - so it reads noisier than the LLM-curated mode would.
"""

import networkx as nx
from pyvis.network import Network

from .browse import CaseArtifacts, _entities_by_doc
from .models import DocumentMetadata, Event

ROLE_COLORS = {
    "plaintiff": "#2ca02c",
    "defendant": "#d62728",
    "judge": "#9467bd",
    "court_clerk": "#8c564b",
    "counsel": "#1f77b4",
    "attorney": "#1f77b4",
    "witness": "#ff7f0e",
}
DEFAULT_ACTOR_COLOR = "#7f7f7f"
PRODUCT_COLOR = "#17becf"
DOCUMENT_COLOR = "#c7c7c7"
EVENT_COLOR = "#e7ba52"

EVENT_NODE_PREFIX = "event:"


def _document_tooltip(doc: DocumentMetadata) -> str:
    parts = [doc.doc_id]
    if doc.document_title:
        parts.append(doc.document_title)
    if doc.filing_date:
        parts.append(f"Filed: {doc.filing_date}")
    if doc.filed_by:
        parts.append(f"Filed by: {doc.filed_by}")
    return "\n".join(parts)


def _event_tooltip(event: Event) -> str:
    parts = []
    if event.event_type:
        parts.append(event.event_type)
    if event.dates:
        parts.append(", ".join(event.dates))
    parts.append(event.description[:400])
    if event.outcome:
        parts.append(f"Outcome: {event.outcome}")
    return "\n".join(parts)


def build_case_graph(artifacts: CaseArtifacts, include_events: bool = False) -> nx.DiGraph:
    """Build the full case graph. Event nodes (from artifacts.events, Stage
    5's output) are only included if `include_events` is True AND the case
    has actually run Stage 5 - the document/actor/product/citation layer
    alone is always available once Stage 1/2 have run.
    """
    g = nx.DiGraph()

    role_by_actor = {a.canonical_name: a.role for a in artifacts.actors.actors}
    product_names = {p.canonical_name for p in artifacts.products.actors}
    entities_by_doc = _entities_by_doc(artifacts.entities)

    for doc in artifacts.files_scan.documents:
        g.add_node(
            doc.doc_id,
            kind="document",
            label=doc.document_title or doc.doc_id,
            title=_document_tooltip(doc),
            color=DOCUMENT_COLOR,
            shape="box",
        )

    for actor in artifacts.actors.actors:
        g.add_node(
            actor.canonical_name,
            kind="actor",
            label=actor.canonical_name,
            title=f"{actor.role} ({'named' if actor.is_named else 'generic'}, source={actor.source})",
            color=ROLE_COLORS.get(actor.role, DEFAULT_ACTOR_COLOR),
            shape="dot",
        )

    for product in artifacts.products.actors:
        title = product.role
        if product.attributed_to:
            title += f" | attributed to: {', '.join(product.attributed_to)}"
        g.add_node(
            product.canonical_name,
            kind="product",
            label=product.canonical_name,
            title=title,
            color=PRODUCT_COLOR,
            shape="diamond",
        )

    for doc_id, doc_entities in entities_by_doc.items():
        if doc_id not in g:
            continue
        for entity in doc_entities:
            actor_name = entity.linked_actor
            if actor_name and (actor_name in role_by_actor or actor_name in product_names) and actor_name in g:
                g.add_edge(doc_id, actor_name, kind="mentions")

    for doc in artifacts.files_scan.documents:
        for ref in doc.referenced_documents:
            if ref.doc_id and ref.doc_id != doc.doc_id and ref.doc_id in g:
                g.add_edge(doc.doc_id, ref.doc_id, kind="cites")

    if include_events and artifacts.events is not None:
        for event in artifacts.events.events:
            node_id = f"{EVENT_NODE_PREFIX}{event.event_id}"
            g.add_node(
                node_id,
                kind="event",
                label=event.event_type or (event.dates[0] if event.dates else event.event_id),
                title=_event_tooltip(event),
                color=EVENT_COLOR,
                shape="star",
            )
            if event.source_doc_id in g:
                g.add_edge(node_id, event.source_doc_id, kind="from")
            for actor_name in event.actors:
                if actor_name in g:
                    g.add_edge(node_id, actor_name, kind="involves")

    return g


def filter_to_actors(g: nx.DiGraph, actor_names: set[str]) -> nx.DiGraph:
    """Subgraph limited to the given actors plus anything directly
    connected to at least one of them (their documents/events/products) -
    used by the "Filter by actor" control already in event_browser.py to
    scope the graph view the same way it already scopes the document
    table."""
    keep = set(actor_names)
    for u, v in g.edges():
        if u in actor_names or v in actor_names:
            keep.add(u)
            keep.add(v)
    return g.subgraph(keep).copy()


def render_pyvis_html(g: nx.DiGraph, height: str = "750px") -> str:
    """Render `g` as a self-contained HTML document (vis.js inlined, no
    external requests) suitable for st.components.v1.html. Never
    st.dataframe/st.table - see this module's and browse.py's docstrings."""
    net = Network(height=height, width="100%", directed=True, cdn_resources="in_line", notebook=False)
    net.from_nx(g)
    net.show_buttons(filter_=["physics"])
    return net.generate_html(notebook=False)
