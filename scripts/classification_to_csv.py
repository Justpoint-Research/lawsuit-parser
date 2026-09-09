#!/usr/bin/env python3
"""Flatten zero_shot_classifier.py's JSON output into a spreadsheet-friendly CSV.

scripts/zero_shot_classifier.py writes data/classification/training_data.json
with each provider's (ollama/gemini/anthropic) labels nested per case under
"provider_results". That shape is awkward to read in a spreadsheet, so this
script rewrites it as one row per case with:

  * case metadata (caption, court, type, status, received date, documents)
  * a confidence column for every category x provider - blank when that
    provider did not assign the category, so agreement/disagreement is
    visible at a glance (adjacent columns are the same category across the
    three providers)
  * per-provider label lists and a majority/union consensus
  * per-provider reasoning (one [category]-prefixed entry per label)

Every field is quoted and multi-value cells are " | "-separated, so a stray
comma or newline in the text can never be read as a column break. Open the
result directly in Google Sheets (File > Import > Upload) or with `gspread`;
the UTF-8 BOM is written so Sheets/Excel detect the encoding.

Usage:
    # Convert the default training data file
    python scripts/classification_to_csv.py

    # Custom paths
    python scripts/classification_to_csv.py --input data/classification/training_data.json \
        --output /tmp/classifications.csv

    # Restrict to specific providers / drop the long reasoning columns
    python scripts/classification_to_csv.py --providers ollama anthropic --no-reasoning
"""

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

# Column order for providers when present; anything else is appended sorted.
CANONICAL_PROVIDER_ORDER = ("ollama", "gemini", "anthropic")

# Separator for multi-value cells (label lists, document names, reasoning).
# " | " rather than "," or ";" so a stray delimiter can't be mistaken for a
# column break, and every field is quoted on write (csv.QUOTE_ALL) as a belt.
MULTI_VALUE_SEP = " | "

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(value: Any) -> str:
    """Strip HTML tags (case_type/case_status carry <span> markup) and collapse
    whitespace so the value sits cleanly in one spreadsheet cell."""
    if value is None:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub("", str(value))).strip()


def order_providers(present: set[str]) -> list[str]:
    ordered = [p for p in CANONICAL_PROVIDER_ORDER if p in present]
    ordered += sorted(present - set(ordered))
    return ordered


def discover(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (providers, categories) to build columns for, in a stable order."""
    results = data.get("results", [])
    present_providers = {
        p for case in results for p in case.get("provider_results", {})
    }
    categories = data.get("metadata", {}).get("categories") or sorted({
        label["category"]
        for case in results
        for pr in case.get("provider_results", {}).values()
        for label in pr.get("labels", [])
    })
    return order_providers(present_providers), list(categories)


def case_row(
    case: dict[str, Any],
    providers: list[str],
    categories: list[str],
    include_reasoning: bool,
) -> dict[str, str]:
    provider_results = case.get("provider_results", {})
    present = [p for p in providers if p in provider_results]

    # category -> {provider: confidence}
    conf: dict[str, dict[str, float]] = {c: {} for c in categories}
    reasoning: dict[str, list[str]] = {p: [] for p in providers}
    for provider in present:
        for label in provider_results[provider].get("labels", []):
            cat = label["category"]
            conf.setdefault(cat, {})[provider] = label.get("confidence")
            if label.get("reasoning"):
                reasoning[provider].append(f"[{cat}] {_clean(label['reasoning'])}")

    votes = {c: [p for p in present if p in conf.get(c, {})] for c in categories}
    n = len(present)
    majority = [c for c in categories if len(votes[c]) > n / 2] if n else []
    union = [c for c in categories if votes[c]]
    label_sets = {
        p: {lbl["category"] for lbl in provider_results[p].get("labels", [])}
        for p in present
    }
    all_agree = n > 1 and len(set(map(frozenset, label_sets.values()))) == 1

    doc_names = case.get("document_names") or []
    row = {
        "case_id": case.get("case_id", ""),
        "caption": _clean(case.get("caption")),
        "court": _clean(case.get("court")),
        "case_type": _clean(case.get("case_type")),
        "case_status": _clean(case.get("case_status")),
        "case_received_date": _clean(case.get("case_received_date")),
        "n_documents": str(len(doc_names)),
        "document_names": MULTI_VALUE_SEP.join(doc_names),
        "providers": MULTI_VALUE_SEP.join(present),
    }
    for provider in providers:
        cats = sorted(label_sets.get(provider, set()))
        row[f"{provider}_labels"] = MULTI_VALUE_SEP.join(cats)
    for cat in categories:
        for provider in providers:
            c = conf.get(cat, {}).get(provider)
            row[f"{cat}__{provider}"] = "" if c is None else f"{c:.2f}"
    row["consensus_majority"] = MULTI_VALUE_SEP.join(majority)
    row["consensus_union"] = MULTI_VALUE_SEP.join(union)
    row["all_agree"] = "TRUE" if all_agree else "FALSE"
    if include_reasoning:
        for provider in providers:
            row[f"{provider}_reasoning"] = MULTI_VALUE_SEP.join(reasoning.get(provider, []))
    return row


def build_fieldnames(
    providers: list[str], categories: list[str], include_reasoning: bool
) -> list[str]:
    fields = [
        "case_id",
        "caption",
        "court",
        "case_type",
        "case_status",
        "case_received_date",
        "n_documents",
        "document_names",
        "providers",
    ]
    fields += [f"{p}_labels" for p in providers]
    for cat in categories:
        fields += [f"{cat}__{p}" for p in providers]
    fields += ["consensus_majority", "consensus_union", "all_agree"]
    if include_reasoning:
        fields += [f"{p}_reasoning" for p in providers]
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flatten multi-provider classifications into a CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=REPO_ROOT / "data" / "classification" / "training_data.json",
        help="Path to zero_shot_classifier.py's output file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="CSV path to write (default: --input with a .csv suffix)",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        help="Only include these providers (default: whichever are present)",
    )
    parser.add_argument(
        "--no-reasoning",
        action="store_true",
        help="Drop the per-provider reasoning columns (much smaller file)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if not args.input.exists():
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    output = args.output or args.input.with_suffix(".csv")

    with open(args.input) as f:
        data = json.load(f)

    results = data.get("results", [])
    if not results:
        print("Error: no results found in input file", file=sys.stderr)
        sys.exit(1)

    providers, categories = discover(data)
    if args.providers:
        unknown = set(args.providers) - set(providers)
        if unknown:
            logger.warning(f"Ignoring providers not present in input: {sorted(unknown)}")
        providers = order_providers(set(args.providers) & set(providers))
    if not providers:
        print("Error: no matching provider results in input file", file=sys.stderr)
        sys.exit(1)

    include_reasoning = not args.no_reasoning
    fieldnames = build_fieldnames(providers, categories, include_reasoning)
    rows = [
        case_row(case, providers, categories, include_reasoning)
        for case in sorted(results, key=lambda c: c.get("case_id", ""))
    ]

    output.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Google Sheets / Excel pick up the encoding; QUOTE_ALL wraps
    # every field (multi-value cells use " | ", not a CSV delimiter, but the
    # quoting removes any doubt about where a column ends).
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, extrasaction="ignore", quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        writer.writerows(rows)

    n_agree = sum(r["all_agree"] == "TRUE" for r in rows)
    print(f"Wrote {len(rows)} case row(s) x {len(fieldnames)} column(s) to {output}")
    print(f"Providers: {providers}  |  Categories: {categories}")
    print(f"All providers agree on the label set: {n_agree}/{len(rows)} case(s)")


if __name__ == "__main__":
    main()
