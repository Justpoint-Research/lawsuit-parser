#!/usr/bin/env python
"""
Build a stratified case sample for the product-liability/personal-injury vs.
other binary classifier.

Queries the live scrapping DB (courts_final.ny_cases_after_search joined to
courts_final.ny_docket_documents - requires the Cloud SQL Proxy, `make
run-proxy`) for every case's case_type/caption/document-availability, labels
each case, and picks a stratified sample: label 1 = product liability /
personal injury torts + caption-level class-action matches; label 0 = a
uniform-ish sample across every other case_type. Cross-references
data/cases/ny_classification to flag which selected cases already have PDFs
downloaded (no new GCS download needed) vs. which are brand new (will need a
real export_classification_sample.py run).

Deliberately does NOT read data/cases/ny_after_search: that's a point-in-time
snapshot and goes stale - verified 2026-09-13 that ~12% of cases a snapshot
called "nothing downloadable" already had real documents live (the crawler
backfills document_bucket_link well after a case is first listed). Always
querying live avoids building a sample - or an empty-cases list - around
data that's already wrong.

This script only reads/reports - it does not download or write case files
(it does write --sample-file/--empty-cases-file and may delete local
directories that turn out to have nothing downloadable - see below).

Re-runnable: if --sample-file already exists, its IDs are kept as-is (never
dropped or reshuffled) and only the shortfall to reach --target-pos/
--target-neg is filled with newly sampled IDs. So growing the labelled pool
is just: bump --target-pos/--target-neg and re-run.

New candidates are drawn only from cases with at least one document that
currently HAS a bucket link, so newly-added sample slots aren't wasted on
cases with nothing to download. Every case lacking a downloadable document
right now (whether newly seen or already sitting in an existing
--sample-file) is written to --empty-cases-file for follow-up.
"""

import argparse
import json
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text

from lawsuit_parser.utils.case_exporter import SCRAPPING_DB_PORT
from lawsuit_parser.utils.db import load_db_config

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


def has_bucket_link(value):
    """True if a document_bucket_link value is a real GCS path.

    SQL NULL arrives from the DB export as either JSON null (-> None) or, for
    rows that passed through a pandas DataFrame at export time, the literal
    float NaN - both are falsy here, along with the empty string.
    """
    return isinstance(value, str) and bool(value)


def make_engine():
    p = load_db_config()
    return create_engine(
        f"postgresql+psycopg://{p['user']}:{p['password']}"
        f"@{p['host']}:{SCRAPPING_DB_PORT}/{p.get('database', 'postgres')}"
    )


def load_cases():
    """Load case_type/caption/document-availability for every NY case,
    straight from the live DB - see module docstring for why this never
    reads a local snapshot."""
    engine = make_engine()
    query = text("""
        SELECT c.id AS case_id, c.case_type, c.caption,
               COUNT(d.id) AS num_docs,
               COUNT(d.document_bucket_link)
                   FILTER (WHERE d.document_bucket_link IS NOT NULL) AS num_downloadable
        FROM courts_final.ny_cases_after_search c
        LEFT JOIN courts_final.ny_docket_documents d ON d.docket_id = c.docket_id
        GROUP BY c.id, c.case_type, c.caption
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(query).fetchall()
    except Exception as e:
        sys.exit(
            f"Could not reach the scrapping DB on port {SCRAPPING_DB_PORT}: {e}\n"
            f"Start the Cloud SQL Proxy first: make run-proxy"
        )
    return [
        {
            "case_id": r.case_id,
            "case_type": clean(r.case_type),
            "caption": r.caption or "",
            "num_docs": r.num_docs,
            "has_downloadable_doc": r.num_downloadable > 0,
        }
        for r in rows
    ]


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


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-pos", type=int, default=350,
                         help="Total desired positive (label 1) case count (default: 350)")
    parser.add_argument("--target-neg", type=int, default=800,
                         help="Total desired negative (label 0) case count (default: 800)")
    parser.add_argument("--sample-file", type=Path,
                         default=Path("data/classification_sample_ids.json"),
                         help="Existing sample to top up (kept as-is) and where the result is written")
    parser.add_argument("--seed", type=int, default=42,
                         help="Random seed for shuffling newly-added candidates (default: 42)")
    parser.add_argument("--empty-cases-file", type=Path,
                         default=Path("data/classification_empty_cases.txt"),
                         help="Where to list case IDs with zero downloadable documents "
                              "(one per line) for later validation")
    return parser.parse_args()


def load_existing_ids(sample_file):
    if not sample_file.exists():
        return set(), set()
    with open(sample_file) as f:
        data = json.load(f)
    return set(data.get("positive_ids", [])), set(data.get("negative_ids", []))


def top_up_pool(full_pool, candidate_pool, existing_ids, target, already_downloaded_fn):
    """Keep every case already in existing_ids (looked up in full_pool, so a
    case kept from a prior run is never dropped even if it later turns out
    to have nothing downloadable), then randomly add more from candidate_pool
    (never already selected) until target is reached."""
    kept = [c for c in full_pool if c["case_id"] in existing_ids]
    candidates = [c for c in candidate_pool if c["case_id"] not in existing_ids]
    random.shuffle(candidates)
    candidates.sort(key=lambda c: not already_downloaded_fn(c["case_id"]))
    n_more = max(0, target - len(kept))
    return kept + candidates[:n_more]


def main():
    args = parse_args()
    random.seed(args.seed)

    existing_pos_ids, existing_neg_ids = load_existing_ids(args.sample_file)
    if existing_pos_ids or existing_neg_ids:
        print(f"Found existing sample at {args.sample_file}: "
              f"{len(existing_pos_ids)} positive / {len(existing_neg_ids)} negative IDs kept as-is")

    cases = load_cases()
    print(f"Loaded {len(cases)} cases from the live DB")

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

    # Cases whose metadata lists documents but NONE of them have a GCS
    # bucket_link (~43.6% of all documents DB-wide, verified 2026-09-13) -
    # dead weight for a sample: they'll never yield a downloadable PDF.
    empty_positives = [c for c in positives if not c["has_downloadable_doc"]]
    empty_negatives = [c for c in negatives if not c["has_downloadable_doc"]]
    print(f"\nCases with documents but nothing downloadable (excluded from new picks): "
          f"{len(empty_positives)} positive / {len(empty_negatives)} negative")

    positives_dl = [c for c in positives if c["has_downloadable_doc"]]
    negatives_dl = [c for c in negatives if c["has_downloadable_doc"]]

    print("\nPositive pool breakdown by case_type:")
    for ct, n in Counter(c["case_type"] for c in positives).most_common(30):
        print(f"  {n:>6}  {ct}")

    # --- Sample positives: keep existing IDs, top up to --target-pos.
    # New picks come only from positives_dl so a fresh slot never lands on a
    # case with nothing to download; existing IDs are kept regardless. ---
    TARGET_POS = args.target_pos
    pos_sample = top_up_pool(positives, positives_dl, existing_pos_ids, TARGET_POS, already_downloaded)

    print(f"\nPositive sample: {len(pos_sample)} "
          f"({sum(1 for c in pos_sample if already_downloaded(c['case_id']))} already downloaded, "
          f"{sum(1 for c in pos_sample if not already_downloaded(c['case_id']))} need fresh download)")

    # --- Sample negatives: keep existing IDs, top up to --target-neg,
    # spreading the newly-added portion across case_type buckets. New picks
    # come only from negatives_dl (has_downloadable_doc); existing IDs are
    # kept regardless. ---
    TARGET_NEG = args.target_neg
    existing_neg_cases = [c for c in negatives if c["case_id"] in existing_neg_ids]
    neg_sample = list(existing_neg_cases)

    by_type = defaultdict(list)
    for c in negatives_dl:
        if c["case_id"] not in existing_neg_ids:
            by_type[c["case_type"]].append(c)
    types = sorted(by_type.keys())
    random.shuffle(types)

    remaining_target = max(0, TARGET_NEG - len(neg_sample))
    remaining_types = list(types)
    added = []
    while len(added) < remaining_target and remaining_types:
        still_have_room = []
        share = max(1, (remaining_target - len(added)) // len(remaining_types))
        for t in remaining_types:
            pool = by_type[t]
            random.shuffle(pool)
            # prefer already-downloaded within this type
            pool.sort(key=lambda c: not already_downloaded(c["case_id"]))
            take = pool[:share]
            added.extend(take)
            by_type[t] = pool[share:]
            if by_type[t]:
                still_have_room.append(t)
            if len(added) >= remaining_target:
                break
        remaining_types = still_have_room

    neg_sample.extend(added[:remaining_target])
    print(f"\nNegative sample: {len(neg_sample)} across {len(set(c['case_type'] for c in neg_sample))} case_type buckets "
          f"({sum(1 for c in neg_sample if already_downloaded(c['case_id']))} already downloaded, "
          f"{sum(1 for c in neg_sample if not already_downloaded(c['case_id']))} need fresh download)")

    print("\nNegative sample breakdown by case_type (top 30):")
    for ct, n in Counter(c["case_type"] for c in neg_sample).most_common(30):
        print(f"  {n:>4}  {ct}")

    total_sample = pos_sample + neg_sample
    need_download = [c for c in total_sample if not already_downloaded(c["case_id"])]
    n_new_pos = len(pos_sample) - len(existing_pos_ids & {c["case_id"] for c in pos_sample})
    n_new_neg = len(neg_sample) - len(existing_neg_ids & {c["case_id"] for c in neg_sample})
    print(f"\n=== TOTAL SAMPLE: {len(total_sample)} ({len(pos_sample)} pos / {len(neg_sample)} neg) ===")
    print(f"Newly added this run: {n_new_pos} pos / {n_new_neg} neg")
    print(f"Already have PDFs on disk: {len(total_sample) - len(need_download)}")
    print(f"Need a fresh export_case.py download: {len(need_download)}")

    out = {
        "positive_ids": [c["case_id"] for c in pos_sample],
        "negative_ids": [c["case_id"] for c in neg_sample],
    }
    with open(args.sample_file, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote candidate ID lists to {args.sample_file}")

    # --- Empty-case bookkeeping: every case with declared documents but none
    # downloadable (whether it's in this sample or not), for later
    # validation against the crawler / DB. ---
    empty_case_ids = sorted({c["case_id"] for c in empty_positives + empty_negatives})
    with open(args.empty_cases_file, "w") as f:
        f.write("\n".join(str(cid) for cid in empty_case_ids) + "\n")
    print(f"Wrote {len(empty_case_ids)} empty case IDs to {args.empty_cases_file}")

    # These local dirs (under ny_classification only, never ny_after_search)
    # hold nothing but a copy of the metadata JSON - already_downloaded()
    # is false for all of them by construction, so this is a safety
    # double-check, not the deciding condition.
    removed_dirs = 0
    for case_id in empty_case_ids:
        case_dir = CLASSIFICATION_DIR / f"case_{case_id}"
        if case_dir.exists() and not already_downloaded(case_id):
            shutil.rmtree(case_dir)
            removed_dirs += 1
    print(f"Removed {removed_dirs} empty case directories from {CLASSIFICATION_DIR}")


if __name__ == "__main__":
    main()
