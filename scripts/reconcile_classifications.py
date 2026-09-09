#!/usr/bin/env python3
"""Reconcile multi-provider zero-shot classifications into training labels.

scripts/zero_shot_classifier.py stores each provider's (ollama/anthropic/
gemini) labels separately per case, under "provider_results". This script
reads that file and computes, per case and per category, three different
ways of combining the providers' votes into one label set:

    majority      - included if more than half of the providers that ran
                    for this case included it (strict majority)
    union         - included if ANY provider included it
    intersection  - included if ALL providers that ran for this case
                    included it (any disagreement drops the label to 0)

It also writes a second file (--training-output) in the shape
train_bert_classifier.py expects: one row per case with a flat "labels"
list taken from a single strategy (--training-strategy, default
intersection) and the case's text/metadata fields carried through.

A case's provider count varies depending on how many providers you've run
so far (see zero_shot_classifier.py --providers) - reconciliation always
operates over whichever providers are actually present for that case, and
the report below breaks down how many cases have how many providers, so a
comparison across strategies is only really meaningful once most cases
have multiple providers' results.

Usage:
    # Reconcile using whatever's in the default training data path
    python scripts/reconcile_classifications.py

    # Only reconcile over specific providers (ignore others if present)
    python scripts/reconcile_classifications.py --providers ollama anthropic

    # Only report on cases that have ALL of the given providers (a fair
    # apples-to-apples comparison, excluding partially-classified cases)
    python scripts/reconcile_classifications.py --require-all-providers
"""

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

STRATEGIES = ("majority", "union", "intersection")


def reconcile_case(
    provider_results: dict[str, dict[str, Any]],
    providers: list[str],
) -> dict[str, Any]:
    """Compute the three reconciliation strategies for one case.

    Args:
        provider_results: This case's "provider_results" dict, keyed by
            provider name.
        providers: Providers to consider (a case may have others present
            that should be ignored per --providers).

    Returns:
        Dict with "providers_used" (the providers actually present for this
        case, intersected with `providers`), and one key per strategy in
        STRATEGIES, each a list of {category, votes, providers, strategies}.
    """
    providers_used = [p for p in providers if p in provider_results]
    n = len(providers_used)

    # category -> set of providers that included it
    votes_by_category: dict[str, set[str]] = {}
    for provider in providers_used:
        for label in provider_results[provider].get("labels", []):
            votes_by_category.setdefault(label["category"], set()).add(provider)

    result: dict[str, Any] = {"providers_used": providers_used, "provider_count": n}
    for strategy in STRATEGIES:
        included = []
        for category, voters in votes_by_category.items():
            votes = len(voters)
            if strategy == "majority":
                keep = n > 0 and votes > n / 2
            elif strategy == "union":
                keep = votes >= 1
            else:  # intersection
                keep = n > 0 and votes == n
            if keep:
                included.append({
                    "category": category,
                    "votes": votes,
                    "of": n,
                    "providers": sorted(voters),
                })
        result[strategy] = sorted(included, key=lambda x: x["category"])
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile multi-provider zero-shot classifications",
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
        default=REPO_ROOT / "data" / "classification" / "reconciled.json",
        help="Path to write the reconciled output (per-strategy breakdown)",
    )
    parser.add_argument(
        "--training-output",
        type=Path,
        default=REPO_ROOT / "data" / "classification" / "reconciled_training_data.json",
        help="Path to write a train_bert_classifier.py-ready file whose per-case "
        "'labels' come from a single reconciliation strategy",
    )
    parser.add_argument(
        "--training-strategy",
        choices=STRATEGIES,
        default="intersection",
        help="Which strategy's labels to bake into --training-output "
        "(default: intersection = a label is kept only when every provider "
        "that ran for the case agreed on it; any disagreement drops it)",
    )
    parser.add_argument(
        "--summaries",
        type=Path,
        default=REPO_ROOT / "data" / "classification" / "case_summaries.json",
        help="Optional summarize_cases_for_classification.py output; when "
        "present its per-case 'summary' is merged into --training-output so "
        "train_bert_classifier.py can use it as the model input",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        help="Only consider these providers (default: whichever are present in the input)",
    )
    parser.add_argument(
        "--require-all-providers",
        action="store_true",
        help="Only include cases that have results from every considered "
        "provider - excludes partially-classified cases from the report "
        "and output, for a fair apples-to-apples strategy comparison",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if not args.input.exists():
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input) as f:
        data = json.load(f)

    summaries: dict[str, Any] = {}
    if args.summaries and args.summaries.exists():
        with open(args.summaries) as f:
            summaries = json.load(f).get("summaries", {})
        logger.info(f"Loaded {len(summaries)} case summary/summaries from {args.summaries}")

    all_results = data.get("results", [])
    providers = args.providers or sorted({
        provider
        for case in all_results
        for provider in case.get("provider_results", {})
    })
    if not providers:
        print("Error: no provider results found in input file", file=sys.stderr)
        sys.exit(1)

    logger.info(f"Reconciling over providers: {providers}")

    # Coverage breakdown - how many cases have how many of the considered providers
    coverage_counts: Counter[int] = Counter()
    reconciled_cases = []
    training_cases = []
    # Text/metadata fields carried through verbatim so train_bert_classifier.py
    # can build its input text without re-reading the source case files.
    passthrough_fields = (
        "caption", "court", "case_type", "case_received_date", "case_status",
        "text_excerpt", "documents_used", "document_names",
    )
    skipped_incomplete = 0

    for case in all_results:
        provider_results = case.get("provider_results", {})
        reconciliation = reconcile_case(provider_results, providers)
        n = reconciliation["provider_count"]
        coverage_counts[n] += 1

        if args.require_all_providers and n < len(providers):
            skipped_incomplete += 1
            continue
        if n == 0:
            continue

        reconciled_cases.append({
            "case_id": case["case_id"],
            "caption": case.get("caption"),
            **reconciliation,
        })

        training_case = {
            "case_id": case["case_id"],
            **{f: case.get(f) for f in passthrough_fields},
            "labels": [
                {"category": entry["category"]}
                for entry in reconciliation[args.training_strategy]
            ],
            "provider_count": n,
        }
        summary_entry = summaries.get(case["case_id"])
        if summary_entry and summary_entry.get("summary"):
            training_case["summary"] = summary_entry["summary"]
        training_cases.append(training_case)

    # Save reconciled output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output_data = {
        "metadata": {
            "source": str(args.input),
            "providers_considered": providers,
            "require_all_providers": args.require_all_providers,
            "total_cases": len(reconciled_cases),
            "skipped_incomplete_coverage": skipped_incomplete,
        },
        "results": reconciled_cases,
    }
    with open(args.output, "w") as f:
        json.dump(output_data, f, indent=2)

    # Training-ready file: one strategy's labels, plus the case text fields
    args.training_output.parent.mkdir(parents=True, exist_ok=True)
    training_data = {
        "metadata": {
            "source": str(args.input),
            "providers_considered": providers,
            "require_all_providers": args.require_all_providers,
            "reconciliation_strategy": args.training_strategy,
            "categories": data.get("metadata", {}).get("categories"),
            "total_cases": len(training_cases),
            "cases_with_summary": sum(1 for tc in training_cases if tc.get("summary")),
        },
        "results": training_cases,
    }
    with open(args.training_output, "w") as f:
        json.dump(training_data, f, indent=2)

    # --- Report ---
    print("\n=== Provider Coverage ===")
    print(f"Providers considered: {providers}")
    for n in sorted(coverage_counts, reverse=True):
        print(f"  {n}/{len(providers)} provider(s): {coverage_counts[n]} case(s)")
    if args.require_all_providers:
        print(f"Excluded (incomplete coverage): {skipped_incomplete} case(s)")

    print(f"\n=== Reconciliation Comparison ({len(reconciled_cases)} case(s)) ===")
    for strategy in STRATEGIES:
        category_counts: Counter[str] = Counter()
        for case in reconciled_cases:
            for entry in case[strategy]:
                category_counts[entry["category"]] += 1
        total = len(reconciled_cases)
        print(f"\n{strategy}:")
        if not category_counts:
            print("  (no positive labels under this strategy)")
        for category, count in sorted(category_counts.items()):
            pct = count / total * 100 if total else 0.0
            print(f"  {category}: {count} cases ({pct:.1f}%)")

    # Disagreement: cases where the three strategies don't all produce the
    # same label set (i.e. providers didn't fully agree)
    disagreements = [
        case for case in reconciled_cases
        if {e["category"] for e in case["majority"]} != {e["category"] for e in case["union"]}
        or {e["category"] for e in case["union"]} != {e["category"] for e in case["intersection"]}
    ]
    print(f"\nCases where strategies disagree: {len(disagreements)}/{len(reconciled_cases)}")

    # Training-file label distribution (the strategy actually baked in)
    print(f"\n=== Training file ({args.training_strategy}, {len(training_cases)} case(s)) ===")
    train_counts: Counter[str] = Counter()
    empty_rows = 0
    for tc in training_cases:
        if not tc["labels"]:
            empty_rows += 1
        for lbl in tc["labels"]:
            train_counts[lbl["category"]] += 1
    for category, count in sorted(train_counts.items()):
        pct = count / len(training_cases) * 100 if training_cases else 0.0
        print(f"  {category}: {count} positive ({pct:.1f}%)")
    print(f"  cases with no positive label: {empty_rows}")
    n_with_summary = sum(1 for tc in training_cases if tc.get("summary"))
    print(f"  cases with an LLM summary: {n_with_summary}/{len(training_cases)}")

    print(f"\nReconciled output saved to: {args.output}")
    print(f"Training-ready output saved to: {args.training_output}")


if __name__ == "__main__":
    main()
