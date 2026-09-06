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

# Import from zero_shot_classifier
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from zero_shot_classifier import (
    load_case_context,
    load_document_text,
    select_documents_for_classification,
    call_ollama,
    build_classification_prompt,
    CLASSIFICATION_RESPONSE_SCHEMA,
)

logger = logging.getLogger(__name__)


class BERTClassifier:
    """BERT-based lawsuit classifier."""

    def __init__(self, model_path: Path, device: str = "cuda"):
        """Load trained BERT model."""
        self.device = torch.device(
            device if torch.cuda.is_available() else "cpu"
        )

        # Load config
        config_path = model_path / "config.json"
        with open(config_path) as f:
            self.config = json.load(f)

        self.categories = self.config["categories"]
        self.max_length = self.config["max_length"]

        # Load model and tokenizer
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_path
        )
        self.model.to(self.device)
        self.model.eval()

        self.tokenizer = AutoTokenizer.from_pretrained(model_path)

        logger.info(f"Loaded BERT model from {model_path}")
        logger.info(f"Categories: {self.categories}")

    def prepare_text(
        self,
        case_metadata: dict[str, Any],
        text_excerpt: str,
    ) -> str:
        """Prepare input text for classification."""
        text_parts = [
            f"Caption: {case_metadata.get('caption', 'Unknown')}",
            f"Court: {case_metadata.get('court', 'Unknown')}",
            f"Case Type: {case_metadata.get('case_type', 'Unknown')}",
        ]

        # Add document names if available
        if case_metadata.get("document_names"):
            text_parts.append(
                f"Documents: {', '.join(case_metadata['document_names'])}"
            )

        # Add text excerpt
        if text_excerpt:
            text_parts.append(f"\nExcerpt:\n{text_excerpt}")

        return "\n".join(text_parts)

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
) -> dict[str, Any] | None:
    """Classify a case using trained BERT model."""
    logger.info(f"Classifying {case_id} with BERT...")

    # Load case metadata
    case_metadata = load_case_context(case_dir)
    if not case_metadata:
        logger.warning(f"No metadata found for {case_id}")
        return None

    # Select and load documents
    doc_count = config.get("classification_doc_count", 2)
    documents = select_documents_for_classification(case_dir, doc_count)

    if not documents:
        logger.warning(f"No documents found for {case_id}")
        return None

    # Gather text
    page_count = config.get("classification_page_count", 3)
    max_chars_per_page = 3000

    text_parts = []

    # Add document names
    doc_names = []
    for doc_meta in case_metadata.get("documents_metadata", [])[:doc_count]:
        if doc_meta.get("document_name"):
            doc_names.append(doc_meta["document_name"])

    case_metadata["document_names"] = doc_names

    # Add document text
    for doc_path in documents:
        doc_text = load_document_text(doc_path, max_chars_per_page * page_count)
        if doc_text:
            text_parts.append(doc_text)

    text_excerpt = "\n\n".join(text_parts)

    # Prepare input and classify
    input_text = classifier.prepare_text(case_metadata, text_excerpt)
    labels = classifier.classify(
        input_text,
        threshold=config.get("min_confidence", 0.6),
    )

    return {
        "case_id": case_id,
        "caption": case_metadata.get("caption"),
        "court": case_metadata.get("court"),
        "case_type": case_metadata.get("case_type"),
        "labels": labels,
        "classifier": "bert",
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

    # Find cases
    data_root = Path(paths_config.get("data_root", "data/cases"))
    if args.all:
        case_dirs = [d for d in data_root.iterdir() if d.is_dir()]
    else:
        case_dirs = [data_root / case_id for case_id in args.cases]
        case_dirs = [d for d in case_dirs if d.exists()]

    logger.info(f"Found {len(case_dirs)} cases to classify")

    # Classify cases
    results = []
    for case_dir in case_dirs:
        case_id = case_dir.name

        if args.use_bert:
            result = classify_with_bert(case_id, case_dir, classifier, config)
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
