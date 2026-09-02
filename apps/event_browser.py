"""Event Browser - Browse event_extraction pipeline output with actor filtering.

Same input as scripts/browse_events.py (the Stage 1-5 artifacts under
data/extraction/<case_id>/events/, written by `make extract-events`),
presented as an interactive Streamlit app instead of a terminal table. The
main addition over the CLI script is tag-style filtering: pick one or more
actors and the document list narrows to documents mentioning them.

The document table browses at document granularity: one row per scanned
document, showing the dates Stage 1 found in it, the actors Stage 2 linked
to it, and the short summary Stage 3 generated for it. Below it, a "Case
graph" section (see lawsuit_parser.event_extraction.graph) renders an
interactive node-link view of the same case - documents, actors, products,
document-to-document citations, and (optionally) Stage 5's events - via
pyvis/vis.js embedded through st.iframe.

NOTE: the document table is rendered as a plain HTML <table> via
st.markdown(..., unsafe_allow_html=True), never st.dataframe/st.table. In
this environment those Arrow-backed widgets segfault the whole server
process - confirmed directly (a bare st.dataframe call crashes with
SIGSEGV via streamlit.testing.v1.AppTest). See render_html_table in
lawsuit_parser/event_extraction/browse.py. The graph view sidesteps this
entirely - pyvis's HTML/JS output never touches Arrow.

Usage:
    streamlit run apps/event_browser.py
"""

import sys
from pathlib import Path

# Note: transformers __path__ access warnings (triggered by GLiNER dependency)
# are suppressed via TRANSFORMERS_VERBOSITY=error environment variable in Makefile
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from lawsuit_parser.event_extraction.browse import (
    EVENT_TABLE_COLUMNS,
    build_document_rows,
    collect_actor_tags,
    compute_case_summary,
    document_rows_to_table_dicts,
    format_name_list,
    list_browsable_cases,
    load_case_artifacts,
    render_html_table,
)
from lawsuit_parser.event_extraction.graph import build_case_graph, filter_to_actors, render_pyvis_html

st.set_page_config(
    page_title="Event Browser",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _artifacts_signature(case_id: str, output_root: str) -> float:
    """Latest mtime across a case's events/*.json artifacts - included in
    the cache key below so re-running extraction (which overwrites those
    files) invalidates the cache on the next script rerun, without needing
    a full Streamlit process restart. A plain st.cache_data key of just
    (case_id, data_root, output_root) never changes across an extraction
    re-run, so it kept serving stale actors/entities from before a fix
    (confirmed: a case re-extracted mid-session still showed pre-fix
    plaintiff/defendant/judge data until the server process was restarted)."""
    events_dir = Path(output_root) / case_id / "events"
    if not events_dir.exists():
        return 0.0
    return max((p.stat().st_mtime for p in events_dir.glob("*.json")), default=0.0)


@st.cache_data(show_spinner=False)
def _load_artifacts(case_id: str, data_root: str, output_root: str, signature: float):
    """Cached so switching filters doesn't re-read artifacts from disk.
    `signature` (see _artifacts_signature) makes the cache key change
    whenever the underlying files do, so a script rerun after re-running
    extraction picks up fresh output automatically."""
    return load_case_artifacts(case_id, Path(data_root), Path(output_root))


@st.cache_data(show_spinner=False)
def _render_graph_html(
    case_id: str, data_root: str, output_root: str, signature: float, include_events: bool, actor_filter: tuple[str, ...]
) -> str:
    """Cached on every input that changes the rendered graph, so toggling
    something unrelated (e.g. the document-list actor filter, which is a
    separate control) doesn't re-run pyvis's layout/HTML generation.
    `signature` - see _load_artifacts - so this doesn't keep serving a
    stale rendered graph after re-extraction either."""
    artifacts = _load_artifacts(case_id, data_root, output_root, signature)
    g = build_case_graph(artifacts, include_events=include_events)
    if actor_filter:
        g = filter_to_actors(g, set(actor_filter))
    return render_pyvis_html(g)


def render_summary(summary) -> None:
    st.subheader(summary.caption or summary.case_id)

    cols = st.columns(4)
    cols[0].metric("Documents", summary.num_documents)
    cols[1].metric("Entities", summary.num_entities)
    cols[2].metric("Dates found", summary.temporal_count)
    year_range_str = (
        f"{summary.year_range[0]}–{summary.year_range[1]}"
        if summary.year_range and summary.year_range[0] != summary.year_range[1]
        else (str(summary.year_range[0]) if summary.year_range else "–")
    )
    cols[3].metric("Time span", year_range_str)

    info_cols = st.columns(3)
    with info_cols[0]:
        st.markdown("**Plaintiff(s)**")
        st.markdown(format_name_list(summary.plaintiffs))
    with info_cols[1]:
        st.markdown("**Defendant(s)**")
        st.markdown(format_name_list(summary.defendants))
    with info_cols[2]:
        st.markdown("**Court**")
        st.markdown(format_name_list(summary.court))

    if summary.other_roles:
        with st.expander(f"Other parties ({sum(len(names) for names in summary.other_roles.values())} total)"):
            for role, names in summary.other_roles.items():
                st.markdown(f"**{role}**")
                st.markdown(format_name_list(names))

    if summary.products:
        st.caption(f"Accused products: {', '.join(summary.products)}")

    with st.expander(f"Entities by label ({summary.num_entities} total)"):
        for label, count in sorted(summary.label_counts.items(), key=lambda kv: -kv[1]):
            st.write(f"- **{label}**: {count}")
        st.caption(
            f"Parties/roles: {summary.num_parties} total. "
            f"{summary.num_linked} of {summary.num_entities} entities linked to a known actor."
        )
        if summary.gliner_model:
            st.caption(f"GLiNER model: {summary.gliner_model} (threshold={summary.gliner_threshold})")


def render_documents(rows, selected_actors: list[str], match_all: bool) -> None:
    if selected_actors:
        wanted = set(selected_actors)
        if match_all:
            filtered = [r for r in rows if wanted <= set(r.actors)]
        else:
            filtered = [r for r in rows if wanted & set(r.actors)]
    else:
        filtered = rows

    if not rows:
        st.info("No documents found for this case.")
        return

    st.write(f"**{len(filtered)} of {len(rows)} documents shown**")

    if not filtered:
        st.info("No documents match the selected actor filter.")
        return

    st.markdown(
        render_html_table(EVENT_TABLE_COLUMNS, document_rows_to_table_dicts(filtered)),
        unsafe_allow_html=True,
    )

    st.markdown("#### Document detail")
    for row in filtered:
        header = f"#{row.seq} — {row.doc_id} — {row.filing_date or 'no filing date'}"
        with st.expander(header):
            if row.document_title:
                st.write(row.document_title)
            if row.summary:
                st.markdown(f"**Summary:** {row.summary}")
            if row.actors:
                st.markdown("**Actors:** " + ", ".join(f"`{a}`" for a in row.actors))
            if row.dates:
                st.markdown(
                    "**Dates found:** " + ", ".join(f"{text} ({dtype})" for dtype, text in row.dates)
                )
            if row.entities:
                st.markdown("**Entities:**")
                for label, text, score in row.entities:
                    st.write(f"- [{label}] {text!r} (score={score:.2f})")


def render_graph(case_id: str, data_root: str, output_root: str, has_events: bool, selected_actors: list[str]) -> None:
    st.markdown("### Case graph")
    st.caption(
        "Documents (boxes), actors (dots, colored by role), and products (diamonds), linked by "
        "who's mentioned in which document and which document cites which. Drag nodes, "
        "scroll to zoom, hover for details."
    )

    include_events = False
    if has_events:
        include_events = st.checkbox(
            "Include events (Stage 5) as nodes - adds one node per event, linked to its "
            "source document and involved actors. Can get dense on a large case.",
            value=False,
        )
    else:
        st.caption("No events.json for this case yet (Stage 5 hasn't run) - showing documents/actors/products only.")

    if selected_actors:
        st.caption(f"Graph scoped to {len(selected_actors)} selected actor(s) via the filter above.")

    html = _render_graph_html(
        case_id, data_root, output_root, _artifacts_signature(case_id, output_root),
        include_events, tuple(sorted(selected_actors)),
    )
    st.iframe(html, height=780)


def main():
    st.title("\U0001F4C5 Event Browser")

    with st.sidebar:
        st.header("Case")
        data_root = st.text_input("Data root (source case data)", value="data/cases")
        output_root = st.text_input("Output root (extraction artifacts)", value="data/extraction")
        cases = list_browsable_cases(output_root)

        if not cases:
            st.error(
                f"No extracted cases found under `{output_root}`. "
                "Run `make extract-events` first."
            )
            st.stop()

        case_id = st.selectbox("Case ID", cases)

    try:
        artifacts = _load_artifacts(case_id, data_root, output_root, _artifacts_signature(case_id, output_root))
    except FileNotFoundError as e:
        st.error(f"Missing extraction artifact for {case_id}: {e}")
        st.stop()
    summary = compute_case_summary(artifacts)
    rows = build_document_rows(artifacts)

    render_summary(summary)
    st.divider()

    st.markdown("### Filter by actor")
    tags = collect_actor_tags(rows)
    selected_actors = st.multiselect(
        "Show only documents mentioning these actors (tag-style filter)",
        options=tags,
        default=[],
    )
    match_all = False
    if len(selected_actors) > 1:
        match_all = st.checkbox(
            "Require ALL selected actors in the same document (default: ANY)",
            value=False,
        )

    st.divider()
    render_documents(rows, selected_actors, match_all)

    st.divider()
    render_graph(case_id, data_root, output_root, has_events=artifacts.events is not None, selected_actors=selected_actors)


if __name__ == "__main__":
    main()
