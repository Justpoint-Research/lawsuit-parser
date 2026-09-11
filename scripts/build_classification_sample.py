#!/usr/bin/env python
"""
Build a stratified case sample for the product-liability/personal-injury vs.
other binary classifier.

Reads every metadata-only JSON already exported to data/cases/ny_after_search
(no DB/network access needed), labels each case, and picks a stratified
sample: label 1 = product liability / personal injury torts + caption-level
class-action matches; label 0 = a uniform-ish sample across every other
case_type. Cross-references data/cases/ny_classification to flag which
selected cases already have PDFs downloaded (no new GCS download needed)
vs. which are brand new (will need a real export_case.py run).

This script only reads/reports - it does not download or write anything.
"""

import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

random.seed(42)

AFTER_SEARCH_DIR = Path("data/cases/ny_after_search")
CLASSIFICATION_DIR = Path("data/cases/ny_classification")

TAG_RE = re.compile(r"<[^>]+>")

def clean(value):
    if value is None:
        return None
    return TAG_RE.sub("", str(value)).strip()

# case_type values (after HTML-stripping) that count as label 1 on their own.
POSITIVE_CASE_TYPES = {
    "Torts - Product Liability",
    "Torts - Product Liability - Mass Tort - Zantac",
    "Mass Tort - Bextra Celebrex",
    "Mass Tort - Neurontin",
    "Mass Tort - Chantix",
    "Mass Tort - Human Tissue Litigation",
    "Mass Tort - RENU",
    "Mass Tort - Steampipe Explosion",
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


def load_cases():
    cases = []
    for path in sorted(AFTER_SEARCH_DIR.glob("case_*/case_*.json")):
        with open(path) as f:
            data = json.load(f)
        info = data.get("case_info", {})
        case_id = info.get("id")
        case_type = clean(info.get("case_type"))
        caption = info.get("caption") or ""
        num_docs = len(data.get("documents", []))
        cases.append({
            "case_id": case_id,
            "case_type": case_type,
            "caption": caption,
            "num_docs": num_docs,
        })
    return cases


def already_downloaded(case_id):
    """True if this case already has at least one PDF under ny_classification."""
    case_dir = CLASSIFICATION_DIR / f"case_{case_id}"
    if not case_dir.exists():
        return False
    for sub in ("documents", "confirmations"):
        d = case_dir / sub
        if d.exists() and any(d.glob("*.pdf")):
            return True
    return False


def main():
    cases = load_cases()
    print(f"Loaded {len(cases)} cases from {AFTER_SEARCH_DIR}")

    with_docs = [c for c in cases if c["num_docs"] > 0]
    print(f"Cases with >=1 document: {len(with_docs)}")

    for c in with_docs:
        c["is_class_action"] = bool(CLASS_ACTION_RE.search(c["caption"]))
        c["label"] = 1 if (c["case_type"] in POSITIVE_CASE_TYPES or c["is_class_action"]) else 0

    positives = [c for c in with_docs if c["label"] == 1]
    negatives = [c for c in with_docs if c["label"] == 0]
    class_action_only = [c for c in positives if c["is_class_action"] and c["case_type"] not in POSITIVE_CASE_TYPES]

    print(f"\nPositive pool (label 1): {len(positives)}")
    print(f"  of which matched purely by class-action caption keyword: {len(class_action_only)}")
    print(f"Negative pool (label 0): {len(negatives)}")

    print("\nPositive pool breakdown by case_type:")
    for ct, n in Counter(c["case_type"] for c in positives).most_common(30):
        print(f"  {n:>6}  {ct}")

    # --- Sample positives: 350 target, prefer already-downloaded ---
    TARGET_POS = 350
    pos_have = [c for c in positives if already_downloaded(c["case_id"])]
    pos_need = [c for c in positives if not already_downloaded(c["case_id"])]
    random.shuffle(pos_have)
    random.shuffle(pos_need)
    pos_sample = pos_have[:TARGET_POS]
    if len(pos_sample) < TARGET_POS:
        pos_sample += pos_need[: TARGET_POS - len(pos_sample)]

    print(f"\nPositive sample: {len(pos_sample)} "
          f"({sum(1 for c in pos_sample if already_downloaded(c['case_id']))} already downloaded, "
          f"{sum(1 for c in pos_sample if not already_downloaded(c['case_id']))} need fresh download)")

    # --- Sample negatives: 800 total, spread across case_type buckets ---
    TARGET_NEG = 800
    by_type = defaultdict(list)
    for c in negatives:
        by_type[c["case_type"]].append(c)
    types = sorted(by_type.keys())
    random.shuffle(types)
    per_type_cap = max(1, TARGET_NEG // len(types))

    neg_sample = []
    remaining_types = list(types)
    while len(neg_sample) < TARGET_NEG and remaining_types:
        still_have_room = []
        share = max(1, (TARGET_NEG - len(neg_sample)) // len(remaining_types))
        for t in remaining_types:
            pool = by_type[t]
            random.shuffle(pool)
            # prefer already-downloaded within this type
            pool.sort(key=lambda c: not already_downloaded(c["case_id"]))
            take = pool[:share]
            neg_sample.extend(take)
            by_type[t] = pool[share:]
            if by_type[t]:
                still_have_room.append(t)
            if len(neg_sample) >= TARGET_NEG:
                break
        remaining_types = still_have_room

    neg_sample = neg_sample[:TARGET_NEG]
    print(f"\nNegative sample: {len(neg_sample)} across {len(set(c['case_type'] for c in neg_sample))} case_type buckets "
          f"({sum(1 for c in neg_sample if already_downloaded(c['case_id']))} already downloaded, "
          f"{sum(1 for c in neg_sample if not already_downloaded(c['case_id']))} need fresh download)")

    print("\nNegative sample breakdown by case_type (top 30):")
    for ct, n in Counter(c["case_type"] for c in neg_sample).most_common(30):
        print(f"  {n:>4}  {ct}")

    total_sample = pos_sample + neg_sample
    need_download = [c for c in total_sample if not already_downloaded(c["case_id"])]
    print(f"\n=== TOTAL SAMPLE: {len(total_sample)} ({len(pos_sample)} pos / {len(neg_sample)} neg) ===")
    print(f"Already have PDFs on disk: {len(total_sample) - len(need_download)}")
    print(f"Need a fresh export_case.py download: {len(need_download)}")

    out = {
        "positive_ids": [c["case_id"] for c in pos_sample],
        "negative_ids": [c["case_id"] for c in neg_sample],
    }
    out_path = Path("data/classification_sample_ids.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote candidate ID lists to {out_path}")


if __name__ == "__main__":
    main()
