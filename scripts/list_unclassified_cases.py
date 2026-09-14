#!/usr/bin/env python
"""
Print the case IDs (space-separated, e.g. "case_6 case_36") from the
classification sample that have no result yet in training_data.json - the
ones zero_shot_classifier.py still needs to process.

Used by `make classification-add-cases` so a re-run of the pipeline only
pays for LLM calls on newly-added cases, not the whole sample again.

Usage:
    uv run python scripts/list_unclassified_cases.py
    uv run python scripts/list_unclassified_cases.py --sample-file ... --training-data ...
"""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-file", type=Path,
                         default=Path("data/classification_sample_ids.json"))
    parser.add_argument("--training-data", type=Path,
                         default=Path("data/classification/training_data.json"))
    args = parser.parse_args()

    sample = json.load(open(args.sample_file))
    all_ids = [f"case_{i}" for i in sample["positive_ids"] + sample["negative_ids"]]

    labelled = set()
    if args.training_data.exists():
        training = json.load(open(args.training_data))
        labelled = {r["case_id"] for r in training.get("results", [])}

    unlabelled = sorted(set(all_ids) - labelled, key=lambda s: int(s.split("_")[1]))
    print(" ".join(unlabelled))


if __name__ == "__main__":
    main()
