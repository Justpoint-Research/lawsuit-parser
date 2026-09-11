#!/usr/bin/env python
"""
Assemble the classifier sample (built by build_classification_sample.py,
downloaded/parsed by export_classification_sample.py) into the exact JSON
format scripts/train_bert_classifier.py expects (the same schema
scripts/reconcile_classifications.py produces for its --training-output):

    {
      "metadata": {"categories": [...]},
      "results": [
        {"case_id", "caption", "court", "case_type", "document_names",
         "text_excerpt", "labels": [{"category": ...}, ...]},
        ...
      ]
    }

Labels here are NOT LLM judgments (no zero_shot_classifier.py run over this
sample) - they're derived from the same case_type/caption heuristic used to
build the sample in the first place: this codebase's existing 3-category
taxonomy (config/event_extraction.toml [lawsuit_classification].categories)
is "product_liability", "personal_injury", "class_action", so a positively-
sampled case is tagged product_liability or personal_injury by its
case_type bucket, plus class_action from a caption keyword match; a
negatively-sampled case gets an empty label list.

Only includes cases that actually have Docling-extracted text on disk right
now - export_classification_sample.py's download+parse run is ongoing in
the background, so re-run this script later to pick up more cases as they
finish.

Usage:
    uv run python scripts/build_classification_training_data.py
    uv run python scripts/train_bert_classifier.py --data data/classification_training_data.json --evaluate
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.zero_shot_classifier import load_document_text, select_documents_for_classification  # noqa: E402

CASES_DIR = Path("data/cases/ny_classification")
DATA_ROOT = Path("data/cases")
OUTPUT_ROOT = Path("data/extraction")
SAMPLE_IDS_PATH = Path("data/classification_sample_ids.json")
OUTPUT_PATH = Path("data/classification_training_data.json")

# Matches config/event_extraction.toml [lawsuit_classification].categories.
CATEGORIES = ["product_liability", "personal_injury", "class_action"]

TAG_RE = re.compile(r"<[^>]+>")


def clean(value):
    if value is None:
        return None
    return TAG_RE.sub("", str(value)).strip()


PRODUCT_LIABILITY_CASE_TYPES = {
    "Torts - Product Liability",
    "Torts - Product Liability - Mass Tort - Zantac",
    "Mass Tort - Bextra Celebrex",
    "Mass Tort - Neurontin",
    "Mass Tort - Chantix",
    "Mass Tort - Human Tissue Litigation",
    "Mass Tort - RENU",
    "Mass Tort - Steampipe Explosion",
}

PERSONAL_INJURY_CASE_TYPES = {
    "Torts - Motor Vehicle",
    "Torts - Motor Vehicle - City",
    "Torts - Medical, Dental, or Podiatrist Malpractice",
    "Medical Malpractice",
    "Torts - Other Negligence",
    "Torts - Other Negligence - City",
    "Torts - Other",
    "Torts - Other - City",
    "Tort",
    "Torts - Asbestos",
    "Asbestos",
    "Torts - Environmental",
    "Torts - Child Victims Act",
    "Torts - Other Professional Malpractice",
    "Adult Survivors Act",
    "Covid - 19 Action Against Nursing Home",
}

CLASS_ACTION_RE = re.compile(
    r"\bclass action\b|\bsimilarly situated\b|\bputative class\b|\bon behalf of (?:himself|herself|themselves|all others)\b",
    re.IGNORECASE,
)

# BERT truncates at tokenize time anyway (see train_bert_classifier.py's
# bert_max_length); this just matches the LLM pipeline's own combined-text
# budget (config/event_extraction.toml max_text_chars) instead of inventing
# a different number.
MAX_TEXT_CHARS = 100_000


def labels_for_case(case_type: str, caption: str) -> list[dict]:
    labels = []
    if case_type in PRODUCT_LIABILITY_CASE_TYPES:
        labels.append({"category": "product_liability"})
    elif case_type in PERSONAL_INJURY_CASE_TYPES:
        labels.append({"category": "personal_injury"})
    if CLASS_ACTION_RE.search(caption or ""):
        labels.append({"category": "class_action"})
    return labels


def main():
    sample = json.load(open(SAMPLE_IDS_PATH))
    positive_ids = set(sample["positive_ids"])
    negative_ids = set(sample["negative_ids"])
    all_ids = sorted(positive_ids | negative_ids)

    results = []
    skipped_not_ready = 0
    skipped_no_metadata = 0

    for case_id in all_ids:
        case_dir = CASES_DIR / f"case_{case_id}"
        meta_path = case_dir / f"case_{case_id}.json"
        if not meta_path.exists():
            skipped_no_metadata += 1
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        info = meta.get("case_info", {})
        caption = info.get("caption") or ""
        case_type = clean(info.get("case_type"))

        doc_paths = select_documents_for_classification(case_dir)
        text_parts = []
        for doc_path in doc_paths:
            text = load_document_text(doc_path, MAX_TEXT_CHARS, DATA_ROOT, OUTPUT_ROOT)
            if text:
                text_parts.append(f"=== Document: {doc_path.name} ===\n{text}")
        text_excerpt = "\n\n".join(text_parts)[:MAX_TEXT_CHARS]

        if not text_excerpt.strip():
            skipped_not_ready += 1
            continue

        labels = labels_for_case(case_type, caption) if case_id in positive_ids else []

        results.append({
            "case_id": f"case_{case_id}",
            "caption": caption,
            "court": info.get("court"),
            "case_type": case_type,
            "document_names": [d.get("document_name") for d in meta.get("documents", []) if d.get("document_name")],
            "text_excerpt": text_excerpt,
            "labels": labels,
        })

    n_pos = sum(1 for r in results if r["labels"])
    n_neg = len(results) - n_pos
    print(f"Sample universe: {len(all_ids)} cases")
    print(f"Included (text extracted, ready to train on): {len(results)} ({n_pos} positive / {n_neg} negative)")
    print(f"Skipped - not extracted yet (export_classification_sample.py still running): {skipped_not_ready}")
    if skipped_no_metadata:
        print(f"Skipped - no case metadata JSON found: {skipped_no_metadata}")

    for cat in CATEGORIES:
        n = sum(1 for r in results for lbl in r["labels"] if lbl["category"] == cat)
        print(f"  {cat}: {n}")

    output = {
        "metadata": {
            "categories": CATEGORIES,
            "source": "build_classification_sample.py + export_classification_sample.py "
                      "(case_type/caption heuristic labels, not LLM-judged)",
        },
        "results": results,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {len(results)} training examples to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
