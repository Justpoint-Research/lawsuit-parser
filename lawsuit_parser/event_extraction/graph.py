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
- actor --represents--> actor: from relations.json (Stage 6), showing
  lawyer-client representation relationships (e.g., counsel representing
  plaintiffs or defendants).
- date --associated_with--> document: filing dates and extracted dates linked
  to the documents where they appear.
- date --occurs_in--> event: dates associated with specific events.
"""

import networkx as nx
from datetime import datetime
from pyvis.network import Network

from .browse import CaseArtifacts, _entities_by_doc
from .models import DocumentMetadata, Event

ROLE_COLORS = {
    "plaintiff": "#28a745",      # Green for plaintiffs
    "defendant": "#dc3545",      # Red for defendants
    "judge": "#000000",          # Black for judges
    "court_clerk": "#6c757d",    # Gray for court clerks
    "counsel": "#007bff",        # Blue for attorneys/counsel
    "attorney": "#007bff",       # Blue for attorneys
    "witness": "#ffc107",        # Yellow for witnesses
}
DEFAULT_ACTOR_COLOR = "#6c757d"  # Gray for others
DOCUMENT_COLOR = "#d3d3d3"       # Light gray for documents
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
    if event.summary:
        parts.append(f"Summary: {event.summary[:300]}")
    parts.append(f"Quote: {event.quote[:300]}")
    if event.outcome:
        parts.append(f"Outcome: {event.outcome}")
    return "\n".join(parts)


def _parse_date_for_display(date_text: str, parsed_date: str | None) -> tuple[str, str | None]:
    """Parse date for display, returning (display_label, parsed_datetime).

    Args:
        date_text: Raw date text
        parsed_date: ISO format parsed date string or None

    Returns:
        (label for node, parsed datetime string or None)
    """
    # Try to parse the parsed_date if available
    if parsed_date:
        try:
            dt = datetime.fromisoformat(parsed_date.replace('Z', '+00:00'))
            return dt.strftime("%Y-%m-%d"), parsed_date
        except:
            pass

    # Fall back to just using the text
    return date_text, parsed_date


def _get_date_color(parsed_date: str | None, year_min: int | None, year_max: int | None) -> str:
    """Get color for a date node based on its year (gradient from early to recent).

    Args:
        parsed_date: ISO format parsed date string or None
        year_min: Minimum year in the case (for color scaling)
        year_max: Maximum year in the case (for color scaling)

    Returns:
        Hex color string
    """
    if not parsed_date or not year_min or not year_max:
        return DATE_COLOR

    try:
        dt = datetime.fromisoformat(parsed_date.replace('Z', '+00:00'))
        year = dt.year

        # If only one year, use default color
        if year_max == year_min:
            return DATE_COLOR

        # Gradient from darker pink (older) to lighter pink (newer)
        ratio = (year - year_min) / (year_max - year_min)
        # Start: #C41E3A (darker pink), End: #FFB6C1 (lighter pink)
        r = int(196 + (255 - 196) * ratio)
        g = int(30 + (182 - 30) * ratio)
        b = int(58 + (193 - 58) * ratio)
        return f"#{r:02x}{g:02x}{b:02x}"
    except:
        return DATE_COLOR


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
            size=25,  # Make documents larger and more prominent
            font={"size": 14},
        )

    # Helper function to determine if an actor is a legal entity (organization) vs individual
    def is_legal_entity(actor):
        """Check if actor is a company/organization (square) vs individual person (dot)."""
        # Check if it's a defendant organization (usually companies)
        if actor.role == 'defendant':
            # Common indicators of corporate entities
            corp_indicators = ['inc', 'llc', 'corp', 'ltd', 'l.l.c', 'company', 'corporation',
                             'pharmaceutical', 'laboratories', 'usa', 'america']
            name_lower = actor.canonical_name.lower()
            if any(ind in name_lower for ind in corp_indicators):
                return True
        return False

    for actor in artifacts.actors.actors:
        # Determine shape: square for legal entities, dot for individuals
        shape = "square" if is_legal_entity(actor) else "dot"
        size = 20 if is_legal_entity(actor) else 15

        g.add_node(
            actor.canonical_name,
            kind="actor",
            label=actor.canonical_name,
            title=f"{actor.role} ({'named' if actor.is_named else 'generic'}, source={actor.source})",
            color=ROLE_COLORS.get(actor.role, DEFAULT_ACTOR_COLOR),
            shape=shape,
            size=size,
        )

    # Add document-to-actor edges (who is present in which document)
    for doc_id, doc_entities in entities_by_doc.items():
        if doc_id not in g:
            continue
        for entity in doc_entities:
            actor_name = entity.linked_actor
            if actor_name and actor_name in role_by_actor and actor_name in g:
                # Document mentions actor
                g.add_edge(
                    doc_id,
                    actor_name,
                    kind="present_in",
                    color="#6c757d",  # Gray
                    width=1.5,
                    title="Is present in document"
                )

    # Events are not shown by default - the graph focuses on actors and documents
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
                # Event from document - purple
                g.add_edge(
                    node_id,
                    event.source_doc_id,
                    kind="from",
                    color="#9467bd",
                    width=1.5,
                    title="Event from document"
                )
            for actor_name in event.actors:
                if actor_name in g:
                    # Event involves actor - purple, dashed
                    g.add_edge(
                        node_id,
                        actor_name,
                        kind="involves",
                        color="#9467bd",
                        width=1.5,
                        dashes=[3, 3],
                        title="Event involves actor"
                    )

            # Connect dates to this event
            for date_text in event.dates:
                date_node_id = f"{DATE_NODE_PREFIX}{date_text}"
                if date_node_id in g:
                    g.add_edge(
                        date_node_id,
                        node_id,
                        kind="occurs_in",
                        color="#FF69B4",  # Hot pink
                        width=1.5,
                        dashes=[3, 2],
                        title="Date occurs in event"
                    )

    # Stage 6: Add representation relationship edges (lawyer --> client)
    if artifacts.relations is not None:
        for relation in artifacts.relations.relations:
            source = relation.source_entity
            target = relation.target_entity
            # Only add edge if both nodes exist in the graph
            if source in g and target in g:
                # Add representation edge (lawyer --> client)
                g.add_edge(
                    source,
                    target,
                    kind="represents",
                    relation_type=relation.relation_type,
                    confidence=relation.confidence,
                    title=f"Represents (confidence: {relation.confidence:.2f})",
                    color="#ffc107",  # Yellow/amber for representation relationships
                    width=2,
                )

    # Drop nodes with no edges at all (e.g. an LLM-discovered actor Stage 2
    # never actually linked to any document, or a document Docling found no
    # text for) - a node with zero relationships adds visual clutter, not
    # information, to a graph whose whole point is showing who/what is
    # connected to what.
    g.remove_nodes_from(list(nx.isolates(g)))

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
    # Default spacing/font left labels overlapping into unreadable text
    # wherever a document has many one-off actor connections (confirmed on
    # mdl-1954: an 18-attorney "APPEARANCES" block all radiating off one
    # document rendered as a solid smear of overlapping names at pyvis's
    # defaults). avoidOverlap pushes nodes apart based on their own size
    # instead of treating them as points; a stronger negative
    # gravitationalConstant and longer springLength spread the whole graph
    # out further; a white stroke behind each label keeps it legible over
    # crossing edges and nearby nodes. More stabilization iterations so it
    # settles into that layout before physics "cools", instead of still
    # visibly drifting/overlapping when the page finishes loading.
    net.set_options("""
    var options = {
      "nodes": {
        "font": { "size": 16, "strokeWidth": 3, "strokeColor": "#ffffff" }
      },
      "edges": {
        "smooth": {
          "enabled": true,
          "type": "continuous"
        },
        "arrows": { "to": { "enabled": true, "scaleFactor": 0.5 } }
      },
      "physics": {
        "enabled": true,
        "stabilization": {
          "enabled": true,
          "iterations": 1000,
          "updateInterval": 25
        },
        "barnesHut": {
          "gravitationalConstant": -15000,
          "centralGravity": 0.2,
          "springLength": 200,
          "springConstant": 0.02,
          "avoidOverlap": 0.8
        }
      },
      "interaction": {
        "dragNodes": true,
        "dragView": true,
        "zoomView": true
      },
      "layout": {
        "improvedLayout": true
      }
    }
    """)

    # Add event listener to disable physics after stabilization
    # This stops the constant movement and makes the graph static
    html = net.generate_html(notebook=False)

    # Inject script to disable physics after initial stabilization
    physics_disable_script = """
    <script type="text/javascript">
      // Wait for the network to be created
      setTimeout(function() {
        if (typeof network !== 'undefined') {
          // Disable physics after stabilization completes
          network.on('stabilizationIterationsDone', function() {
            network.setOptions({ physics: { enabled: false } });
          });

          // Fallback: disable physics after 5 seconds regardless
          setTimeout(function() {
            network.setOptions({ physics: { enabled: false } });
          }, 5000);
        }
      }, 100);
    </script>
    """

    # Insert the script before the closing body tag
    html = html.replace('</body>', physics_disable_script + '</body>')

    return html
    return net.generate_html(notebook=False)
