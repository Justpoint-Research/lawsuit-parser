"""Event Browser - Browse extracted event-candidate segments with actor filtering.

Same input as scripts/browse_events.py (the stage 0-4 extraction artifacts
under data/cases/<case_id>/stages/), presented as an interactive Streamlit
app instead of a terminal table. The main addition over the CLI script is
tag-style filtering: pick one or more actors (plaintiff, defendant, or any
other party/org GLiNER found) and the event list narrows to segments
mentioning them.

Full structured proto-events (predicate + typed actor/date/location edges)
require GLiNER-Relex, which isn't implemented yet - see
lawsuit_parser/extraction/protoevents.py. What's browsable here is
priority_segments: paragraphs flagged as event-bearing because they contain
a temporal expression, or a legal-action span plus a party mention.

NOTE: the event table is rendered as plain monospace text (st.code), not
st.dataframe/st.table. In this environment those Arrow-backed widgets
segfault the whole server process (libarrow.so) the moment the script reruns
after having rendered one - confirmed via the real running server, not just
a test harness quirk. See render_ascii_table in lawsuit_parser/extraction/browse.py.

Usage:
    streamlit run apps/event_browser.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from lawsuit_parser.extraction.browse import (
    EVENT_TABLE_COLUMNS,
    build_event_rows,
    collect_actor_tags,
    compute_case_summary,
    event_rows_to_table_dicts,
    list_browsable_cases,
    load_case_artifacts,
    render_ascii_table,
)

st.set_page_config(
    page_title="Event Browser",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(show_spinner=False)
def _load_summary_and_rows(case_id: str, data_root: str):
    """Cached so switching filters doesn't re-read stage artifacts from disk.
    Cache key is (case_id, data_root); re-run stages and rerun the app to
    pick up fresh extraction output for a case."""
    artifacts = load_case_artifacts(case_id, Path(data_root))
    summary = compute_case_summary(artifacts)
    rows = build_event_rows(artifacts)
    return summary, rows


def render_summary(summary) -> None:
    st.subheader(summary.caption or summary.case_id)

    cols = st.columns(4)
    cols[0].metric("Documents", summary.num_documents)
    cols[1].metric("Priority segments", summary.num_priority_segments)
    cols[2].metric("Temporal expressions", summary.temporal_count)
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

    with st.expander(f"Entity spans ({sum(summary.label_counts.values())} total)"):
        for label, count in sorted(summary.label_counts.items(), key=lambda kv: -kv[1]):
            st.write(f"- **{label}**: {count}")
        st.caption(
            f"Registry parties: {summary.num_registry_parties} total "
            f"({summary.num_mentions} mentions, {summary.num_unresolved} unresolved). "
            f"Proto-events: {summary.num_proto_events} "
            "(requires GLiNER-Relex, currently disabled)."
        )


def render_events(rows, selected_actors: list[str], match_all: bool) -> None:
    if selected_actors:
        wanted = set(selected_actors)
        if match_all:
            filtered = [r for r in rows if wanted <= set(r.actors)]
        else:
            filtered = [r for r in rows if wanted & set(r.actors)]
    else:
        filtered = rows

    if not rows:
        st.info("No priority segments (event candidates) found for this case.")
        return

    st.write(f"**{len(filtered)} of {len(rows)} events shown**")

    if not filtered:
        st.info("No events match the selected actor filter.")
        return

    st.code(
        render_ascii_table(EVENT_TABLE_COLUMNS, event_rows_to_table_dicts(filtered)),
        language=None,
    )

    st.markdown("#### Event detail")
    for row in filtered:
        timestamp_str = "; ".join(row.timestamps) if row.timestamps else "no date"
        header = f"#{row.seq} — {timestamp_str} — {row.doc_id} (page {row.page})"
        with st.expander(header):
            if row.actors:
                st.markdown("**Actors:** " + ", ".join(f"`{a}`" for a in row.actors))
            st.write(row.text.strip())


def main():
    st.title("\U0001F4C5 Event Browser")

    with st.sidebar:
        st.header("Case")
        data_root = st.text_input("Data root", value="data/cases")
        cases = list_browsable_cases(data_root)

        if not cases:
            st.error(
                f"No fully-extracted cases found under `{data_root}`. "
                "Run `uv run scripts/extract.py --case-id <id> --stages 0-4` first."
            )
            st.stop()

        case_id = st.selectbox("Case ID", cases)

    try:
        summary, rows = _load_summary_and_rows(case_id, data_root)
    except FileNotFoundError as e:
        st.error(f"Missing stage artifact for {case_id}: {e}")
        st.stop()

    render_summary(summary)
    st.divider()

    st.markdown("### Filter by actor")
    tags = collect_actor_tags(rows)
    selected_actors = st.multiselect(
        "Show only events mentioning these actors (tag-style filter)",
        options=tags,
        default=[],
    )
    match_all = False
    if len(selected_actors) > 1:
        match_all = st.checkbox(
            "Require ALL selected actors in the same event (default: ANY)",
            value=False,
        )

    st.divider()
    render_events(rows, selected_actors, match_all)


if __name__ == "__main__":
    main()
