#!/usr/bin/env python3
"""Classify lawsuits using either trained BERT model or LLM.

This script applies the classifier to new cases, using either the fast
BERT-based model (if trained) or falling back to LLM-based classification.

Usage:
    # Classify using BERT model (fast)
    python scripts/classify_lawsuit.py case_95 --use-bert

    # Classify using LLM (slower but more flexible)
    python scripts/classify_lawsuit.py case_95 --use-llm

    # Classify multiple cases
    python scripts/classify_lawsuit.py case_95 case_227 --use-bert

    # Classify all cases
    python scripts/classify_lawsuit.py --all --use-bert
"""

import argparse
import json
import logging
import sys
import tomllib
from pathlib import Path
from typing import Any

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

# Add parent directory to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Import shared helpers
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from zero_shot_classifier import (
    load_case_context,
    call_ollama,
    build_classification_prompt,
    CLASSIFICATION_RESPONSE_SCHEMA,
    find_cases,
)
from train_bert_classifier import build_bert_input_text
from summarize_cases_for_classification import (
    gather_case_text,
    load_summary_prompt_template,
    summarize_case,
)

logger = logging.getLogger(__name__)


class BERTClassifier:
    """BERT-based lawsuit classifier."""

    def __init__(self, model_path: Path, device: str = "cuda"):
        """Load trained BERT model."""
        self.device = torch.device(
            device if torch.cuda.is_available() else "cpu"
        )

        # Load training metadata (categories, max_length). Older runs wrote
        # this to config.json; current runs use classifier_meta.json so the
        # HF model config.json stays intact.
        meta_path = model_path / "classifier_meta.json"
        if not meta_path.exists():
            meta_path = model_path / "config.json"
        with open(meta_path) as f:
            self.config = json.load(f)

        self.categories = self.config["categories"]
        self.max_length = self.config["max_length"]
        # How the model was trained to read a case (see build_bert_input_text).
        self.input_field = self.config.get("input_field", "auto")

        # Load model and tokenizer
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_path
        )
        self.model.to(self.device)
        self.model.eval()

        self.tokenizer = AutoTokenizer.from_pretrained(model_path)

        logger.info(f"Loaded BERT model from {model_path}")
        logger.info(f"Categories: {self.categories}")

    def classify(
        self,
        text: str,
        threshold: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Classify a case using BERT model."""
        # Tokenize
        encoding = self.tokenizer(
            text,
            max_length=self.max_length,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )

        input_ids = encoding["input_ids"].to(self.device)
        attention_mask = encoding["attention_mask"].to(self.device)

        # Predict
        with torch.no_grad():
            outputs = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
            )

        # Get probabilities
        probs = torch.sigmoid(outputs.logits).squeeze().cpu().numpy()

        # Build results
        labels = []
        for idx, (category, confidence) in enumerate(zip(self.categories, probs)):
            if confidence >= threshold:
                labels.append({
                    "category": category,
                    "confidence": float(confidence),
                    "reasoning": "Classified by trained BERT model",
                })

        return labels


def classify_with_bert(
    case_id: str,
    case_dir: Path,
    classifier: BERTClassifier,
    config: dict[str, Any],
    data_root: Path,
    output_root: Path,
    summary_template: str,
) -> dict[str, Any] | None:
    """Classify a case with the trained BERT model.

    Builds the exact same input the model was trained on
    (build_bert_input_text): for a summary-trained model that means
    generating the case summary here with the local LLM, one call per case.
    """
    logger.info(f"Classifying {case_id} with BERT ({classifier.input_field})...")

    case_metadata = load_case_context(case_dir)
    if not case_metadata:
        logger.warning(f"No metadata found for {case_id}")
        return None

    record: dict[str, Any] = {
        "case_id": case_id,
        "caption": case_metadata.get("caption"),
        "court": case_metadata.get("court"),
        "case_type": case_metadata.get("case_type"),
    }

    if classifier.input_field in ("summary", "auto"):
        summarized = summarize_case(
            case_dir,
            config,
            data_root,
            output_root,
            classifier.tokenizer,
            summary_template,
            max_words=config.get("summary_max_words", 200),
            input_max_chars=config.get("summary_input_max_chars", 30000),
            max_tokens=classifier.max_length - 2,
        )
        if summarized:
            record["summary"] = summarized["summary"]
        elif classifier.input_field == "summary":
            logger.warning(f"{case_id}: could not summarize, skipping")
            return None

    if "summary" not in record:
        # Excerpt fallback - match the ~1000-char snippet the excerpt-trained
        # model saw (zero_shot_classifier stores text_excerpt[:1000]).
        text = gather_case_text(
            case_dir, case_metadata, config, data_root, output_root,
            config.get("summary_input_max_chars", 30000),
        )
        record["text_excerpt"] = text[:1000]
        record["document_names"] = [
            dm["document_name"]
            for dm in case_metadata.get("documents_metadata", [])
            if dm.get("document_name")
        ]

    input_text = build_bert_input_text(record, input_field=classifier.input_field)
    labels = classifier.classify(
        input_text,
        threshold=config.get("min_confidence", 0.6),
    )

    return {
        **{k: record[k] for k in ("case_id", "caption", "court", "case_type")},
        "labels": labels,
        "classifier": "bert",
        "input_field": classifier.input_field,
        "model_path": str(config.get("model_save_path")),
    }


def classify_with_llm(
    case_id: str,
    case_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Classify a case using LLM (reuses zero_shot_classifier logic)."""
    from zero_shot_classifier import classify_case
    return classify_case(case_id, case_dir, config)


def main():
    parser = argparse.ArgumentParser(
        description="Classify lawsuits using BERT or LLM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "cases",
        nargs="*",
        help="Case IDs to classify",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Classify all cases",
    )
    parser.add_argument(
        "--use-bert",
        action="store_true",
        help="Use trained BERT model (fast)",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use LLM (slower, more flexible)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "config" / "event_extraction.toml",
        help="Path to config file",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        help="Path to trained BERT model (default: from config)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON file for results",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Validate arguments
    if not args.use_bert and not args.use_llm:
        parser.error("Must specify either --use-bert or --use-llm")

    if not args.cases and not args.all:
        parser.error("Must specify case IDs or --all")

    # Load config
    with open(args.config, "rb") as f:
        full_config = tomllib.load(f)
    config = full_config.get("lawsuit_classification", {})
    paths_config = full_config.get("paths", {})

    # Initialize classifier
    classifier = None
    if args.use_bert:
        model_path = args.model_path or Path(config.get(
            "model_save_path",
            "data/classification/bert_classifier"
        ))

        if not model_path.exists():
            logger.error(f"BERT model not found at {model_path}")
            logger.error("Train the model first using: python scripts/train_bert_classifier.py")
            sys.exit(1)

        classifier = BERTClassifier(model_path)

    # Find cases - same source-aware discovery as zero_shot_classifier.py
    data_root = Path(config.get("data_root", "data/cases"))
    if not data_root.exists():
        data_root = Path(paths_config.get("data_root", "data/cases"))
    output_root = Path(paths_config.get("output_root", "data/extraction"))
    case_sources = config.get("case_sources", ["ny_sample"])
    case_dirs = find_cases(data_root, case_sources, args.cases or None)

    logger.info(f"Found {len(case_dirs)} cases to classify (sources: {case_sources})")

    summary_template = load_summary_prompt_template(
        REPO_ROOT / "config" / "llm_prompts.toml"
    )

    # Classify cases
    results = []
    for case_dir in case_dirs:
        case_id = case_dir.name

        if args.use_bert:
            result = classify_with_bert(
                case_id, case_dir, classifier, config,
                data_root, output_root, summary_template,
            )
        else:
            result = classify_with_llm(case_id, case_dir, config)

        if result:
            results.append(result)

            # Print result
            print(f"\n{case_id}:")
            print(f"  Caption: {result.get('caption', 'N/A')}")
            print(f"  Labels:")
            for label in result.get("labels", []):
                print(f"    - {label['category']}: {label['confidence']:.3f}")
                if label.get("reasoning"):
                    print(f"      {label['reasoning']}")

    # Save results if output specified
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump({"results": results}, f, indent=2)
        logger.info(f"Saved results to {args.output}")

    # Summary
    print(f"\n=== Summary ===")
    print(f"Classified {len(results)} cases")

    if results:
        category_counts = {}
        for result in results:
            for label in result.get("labels", []):
                cat = label["category"]
                category_counts[cat] = category_counts.get(cat, 0) + 1

        print("\nCategory distribution:")
        for cat, count in sorted(category_counts.items()):
            print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()
