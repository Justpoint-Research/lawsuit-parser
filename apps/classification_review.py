"""Classification review app.

Reads the prepared training-data CSV in ``data/classification/`` and, for each
case, shows which model voted for which label plus the per-model reasoning in a
tab view.

Run with::

    make classification-review
    # or
    uv run streamlit run apps/classification_review.py

Note: this app deliberately avoids pandas. The Arrow/pyarrow stack segfaults the
Streamlit server on rerun in this environment (same issue noted in apps/README.md
for the event browser), and any pandas use across a widget rerun triggers it. The
dataset is tiny, so the stdlib ``csv`` module is plenty.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import streamlit as st

CLASSIFICATION_DIR = Path(__file__).resolve().parents[1] / "data" / "classification"
DATA_PATH = CLASSIFICATION_DIR / "training_data.csv"
# The exact per-case text handed to the BERT classifier (input_field="summary").
SUMMARY_PATH = CLASSIFICATION_DIR / "reconciled_training_data.json"
# Generation metadata for those summaries (token counts, truncation, limits).
SUMMARY_META_PATH = CLASSIFICATION_DIR / "case_summaries.json"

MODELS = ["ollama", "gemini", "anthropic"]
MODEL_ICONS = {
    "ollama": ":material/pets:",
    "gemini": ":material/auto_awesome:",
    "anthropic": ":material/psychology:",
}


@st.cache_data(show_spinner=False)
def load_rows(path: str, mtime: float) -> list[dict[str, str]]:
    """Load the training-data CSV as a list of dict rows. ``mtime`` busts the cache."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@st.cache_data(show_spinner=False)
def load_summaries(path: str, mtime: float) -> dict[str, str]:
    """case_id -> the summary string passed to the BERT classifier."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {r["case_id"]: (r.get("summary") or "") for r in data.get("results", [])}


@st.cache_data(show_spinner=False)
def load_summary_meta(path: str, mtime: float) -> tuple[dict[str, dict], dict]:
    """(case_id -> generation stats, run-level metadata) for the summaries."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("summaries", {}), data.get("metadata", {})


def split_labels(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def all_labels(rows: list[dict[str, str]]) -> list[str]:
    seen: set[str] = set()
    for row in rows:
        for model in MODELS:
            seen.update(split_labels(row.get(f"{model}_labels")))
    return sorted(seen)


def to_float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_reasoning(text: str | None) -> list[tuple[str, str]]:
    """Split ``[label] ... | [label] ...`` reasoning into (label, text) pairs."""
    text = (text or "").strip()
    if not text or text == "nan":
        return []
    parts = re.split(r"\s*\|?\s*(?=\[[a-z_]+\]\s)", text)
    out: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"\[([a-z_]+)\]\s*(.*)", part, flags=re.DOTALL)
        out.append((m.group(1), m.group(2).strip()) if m else ("", part))
    return out


st.set_page_config(page_title="Classification review", page_icon=":material/how_to_vote:", layout="wide")

if not DATA_PATH.exists():
    st.error(f"Training data not found at `{DATA_PATH}`.")
    st.stop()

rows = load_rows(str(DATA_PATH), DATA_PATH.stat().st_mtime)
labels = all_labels(rows)

summaries: dict[str, str] = {}
summary_meta: dict[str, dict] = {}
summary_run_meta: dict = {}
if SUMMARY_PATH.exists():
    summaries = load_summaries(str(SUMMARY_PATH), SUMMARY_PATH.stat().st_mtime)
if SUMMARY_META_PATH.exists():
    summary_meta, summary_run_meta = load_summary_meta(
        str(SUMMARY_META_PATH), SUMMARY_META_PATH.stat().st_mtime
    )


def agrees(row: dict[str, str]) -> bool:
    return str(row.get("all_agree", "")).strip().lower() == "true"


st.title("Classification review")
st.caption(f"{len(rows)} cases · data/classification/training_data.csv")

# --- Dataset overview -------------------------------------------------------
n_agree = sum(agrees(r) for r in rows)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Cases", len(rows))
c2.metric("All models agree", n_agree)
c3.metric("Some disagreement", len(rows) - n_agree)
c4.metric("Labels", len(labels))

with st.expander("Label frequency (by model)"):
    freq_lines = [
        "| Label | " + " | ".join(m.capitalize() for m in MODELS) + " |",
        "|" + "---|" * (len(MODELS) + 1),
    ]
    for label in labels:
        counts = [
            str(sum(label in split_labels(r.get(f"{model}_labels")) for r in rows))
            for model in MODELS
        ]
        freq_lines.append(f"| `{label}` | " + " | ".join(counts) + " |")
    st.markdown("\n".join(freq_lines))

st.divider()

# --- Case picker ----------------------------------------------------------
only_disagree = st.sidebar.toggle("Only cases with disagreement", value=False)
view = [r for r in rows if not agrees(r)] if only_disagree else rows
if not view:
    st.warning("No cases match the current filter.")
    st.stop()

caption_by_id = {r["case_id"]: r["caption"] for r in view}
case_id = st.sidebar.selectbox(
    "Case",
    [r["case_id"] for r in view],
    format_func=lambda cid: f"{cid} — {caption_by_id.get(cid, '')}",
)
case = next(r for r in rows if r["case_id"] == case_id)

# --- Case header ---------------------------------------------------------
st.subheader(case["caption"])
h1, h2, h3 = st.columns(3)
h1.markdown(f"**Court**\n\n{case['court']}")
h2.markdown(f"**Case type**\n\n{case['case_type']}")
h3.markdown(f"**Status**\n\n{case['case_status']}  \nReceived {case['case_received_date']}")

with st.expander(f"{case['n_documents']} document(s)"):
    for name in str(case["document_names"]).split("|"):
        st.markdown(f"- {name.strip()}")

st.divider()

# --- Summary handed to the BERT classifier -----------------------------
bert_model = summary_run_meta.get("bert_model", "legal-bert-base-uncased")
max_tokens = summary_run_meta.get("max_input_tokens")
st.markdown("### Summary passed to the BERT classifier")

# Enlarge the summary body text.
st.markdown(
    """
    <style>
    .st-key-bert_summary [data-testid="stMarkdownContainer"] p {
        font-size: 1.35rem;
        line-height: 1.75;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.caption(
    f"`input_field=summary` → {bert_model}"
    + (f" · truncated to {max_tokens} tokens" if max_tokens else "")
)

summary_text = summaries.get(case_id, "")
meta = summary_meta.get(case_id, {})
if not summary_text:
    st.info(
        "No summary found for this case in "
        f"`{SUMMARY_PATH.name}`."
        if SUMMARY_PATH.exists()
        else f"`{SUMMARY_PATH.name}` not found."
    )
else:
    if meta:
        tags = []
        if meta.get("token_count") is not None:
            tags.append(f"{meta['token_count']} tokens")
        if meta.get("input_chars"):
            tags.append(f"distilled from {int(meta['input_chars']):,} source chars")
        if meta.get("truncated"):
            tags.append("⚠️ truncated")
        if meta.get("reasked"):
            tags.append("re-asked (too long on first pass)")
        if tags:
            st.caption(" · ".join(tags))
    with st.container(border=True, key="bert_summary"):
        st.write(summary_text)

st.divider()

# --- Vote summary ------------------------------------------------------
st.markdown("### Who voted for what")

majority = split_labels(case["consensus_majority"])
table = [
    "| Label | " + " | ".join(m.capitalize() for m in MODELS) + " | Majority |",
    "|" + "---|" * (len(MODELS) + 2),
]
for label in labels:
    cells = []
    for model in MODELS:
        voted = label in split_labels(case[f"{model}_labels"])
        conf = to_float(case.get(f"{label}__{model}"))
        cells.append((f"✅ {conf:.2f}" if conf is not None else "✅") if voted else "—")
    table.append(
        f"| `{label}` | " + " | ".join(cells) + f" | {'✅' if label in majority else '—'} |"
    )
st.markdown("\n".join(table))

st.markdown(
    f"**Consensus (majority):** {', '.join(f'`{l}`' for l in majority) or '—'}  \n"
    f"**Consensus (union):** "
    f"{', '.join(f'`{l}`' for l in split_labels(case['consensus_union'])) or '—'}  \n"
    f"**All models agree:** {'yes' if agrees(case) else 'no'}"
)

st.divider()

# --- Reasoning tabs ---------------------------------------------------
st.markdown("### Model reasoning")

# Bump the reasoning text size inside the tab panels only.
st.markdown(
    """
    <style>
    div[data-testid="stTabs"] [role="tabpanel"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stTabs"] [role="tabpanel"] [data-testid="stMarkdownContainer"] li {
        font-size: 1.35rem;
        line-height: 1.75;
    }
    div[data-testid="stTabs"] [role="tabpanel"] [data-testid="stMarkdownContainer"] h4 {
        font-size: 1.55rem;
    }
    div[data-testid="stTabs"] button[role="tab"] p { font-size: 1.15rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

tabs = st.tabs([f"{MODEL_ICONS[m]} {m.capitalize()}" for m in MODELS])
for tab, model in zip(tabs, MODELS):
    with tab:
        voted = split_labels(case[f"{model}_labels"])
        st.markdown("**Voted for:** " + (", ".join(f"`{l}`" for l in voted) or "_nothing_"))
        pairs = parse_reasoning(case.get(f"{model}_reasoning"))
        if not pairs:
            st.info("No reasoning recorded for this model.")
            continue
        for label, body in pairs:
            if label:
                st.markdown(f"#### `{label}`")
            st.markdown(body)
