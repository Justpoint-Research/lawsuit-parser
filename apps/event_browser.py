"""Event Browser - Browse event_extraction pipeline output with actor filtering.

Same input as scripts/browse_events.py (the Stage 1/2/3 artifacts under
data/extraction/<case_id>/events/, written by `make extract-events`),
presented as an interactive Streamlit app instead of a terminal table. The
main addition over the CLI script is tag-style filtering: pick one or more
actors and the document list narrows to documents mentioning them.

A real event/timeline concept (Stage 4+) isn't built yet, so this browses at
document granularity: one row per scanned document, showing the dates Stage 1
found in it, the actors Stage 2 linked to it, and the short summary Stage 3
generated for it.

NOTE: the table is rendered as a plain HTML <table> via
st.markdown(..., unsafe_allow_html=True), never st.dataframe/st.table. In
this environment those Arrow-backed widgets segfault the whole server
process - confirmed directly (a bare st.dataframe call crashes with
SIGSEGV via streamlit.testing.v1.AppTest). See render_html_table in
lawsuit_parser/event_extraction/browse.py.

Usage:
    streamlit run apps/event_browser.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from lawsuit_parser.event_extraction.browse import (
    EVENT_TABLE_COLUMNS,
    build_document_rows,
    collect_actor_tags,
    compute_case_summary,
    document_rows_to_table_dicts,
    list_browsable_cases,
    load_case_artifacts,
    render_html_table,
)

st.set_page_config(
    page_title="Event Browser",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(show_spinner=False)
def _load_summary_and_rows(case_id: str, data_root: str, output_root: str):
    """Cached so switching filters doesn't re-read artifacts from disk.
    Cache key is (case_id, data_root, output_root); re-run extraction and
    rerun the app to pick up fresh output for a case."""
    artifacts = load_case_artifacts(case_id, Path(data_root), Path(output_root))
    summary = compute_case_summary(artifacts)
    rows = build_document_rows(artifacts)
    return summary, rows


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
        st.write(", ".join(summary.plaintiffs) if summary.plaintiffs else "–")
    with info_cols[1]:
        st.markdown("**Defendant(s)**")
        st.write(", ".join(summary.defendants) if summary.defendants else "–")
    with info_cols[2]:
        st.markdown("**Court**")
        st.write(summary.court or "–")

    if summary.other_roles:
        other_str = "; ".join(f"{role}: {', '.join(names)}" for role, names in summary.other_roles.items())
        st.caption(f"Other parties: {other_str}")

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
        summary, rows = _load_summary_and_rows(case_id, data_root, output_root)
    except FileNotFoundError as e:
        st.error(f"Missing extraction artifact for {case_id}: {e}")
        st.stop()

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


if __name__ == "__main__":
    main()
