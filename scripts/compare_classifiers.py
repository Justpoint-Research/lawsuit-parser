#!/usr/bin/env python3
"""Compare lawsuit-classification approaches on one shared train/test split.

Configurations compared:
  - tfidf         + logreg / xgboost / random_forest
  - bert_frozen   + logreg / xgboost / random_forest   (pretrained BERT embeddings, no fine-tuning)
  - bert_finetuned + nn                                 (end-to-end fine-tuning, via train_bert_classifier.py)

All configs share one stratified train/test split (by label-combo) so the
metrics are directly comparable.

Usage:
    uv run python scripts/compare_classifiers.py
    uv run python scripts/compare_classifiers.py --skip-bert-finetune   # skip the slow branch
    uv run python scripts/compare_classifiers.py --exclude-categories class_action
    uv run python scripts/compare_classifiers.py --no-class-weights
"""

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import numpy as np
import torch
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, hamming_loss
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader
from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
from xgboost import XGBClassifier

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.train_bert_classifier import (  # noqa: E402
    LawsuitDataset,
    load_config,
    load_training_data,
    train_epoch,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared split
# ---------------------------------------------------------------------------


def stratified_split(
    texts: list[str],
    labels: list[list[int]],
    test_size: float,
    seed: int,
) -> tuple[list[str], list[str], list[list[int]], list[list[int]]]:
    """Split stratified by label-combo, merging singleton combos into one
    bucket so train_test_split doesn't choke on strata it can't split."""
    combos = [tuple(row) for row in labels]
    counts = Counter(combos)
    rare = {c for c, n in counts.items() if n < 2}
    if rare:
        logger.warning(
            f"{sum(counts[c] for c in rare)} case(s) have a label-combo seen "
            f"<2 times ({len(rare)} combo(s)) - merged into one bucket for "
            f"stratification purposes only, actual labels are untouched"
        )
    strata = ["rare" if c in rare else c for c in combos]

    idx = list(range(len(texts)))
    train_idx, test_idx = train_test_split(
        idx, test_size=test_size, random_state=seed, stratify=strata
    )
    train_texts = [texts[i] for i in train_idx]
    test_texts = [texts[i] for i in test_idx]
    train_labels = [labels[i] for i in train_idx]
    test_labels = [labels[i] for i in test_idx]
    return train_texts, test_texts, train_labels, test_labels


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_metrics(
    y_true: np.ndarray, y_pred: np.ndarray, categories: list[str]
) -> dict[str, float]:
    metrics = {
        "hamming_loss": hamming_loss(y_true, y_pred),
        "f1_micro": f1_score(y_true, y_pred, average="micro", zero_division=0),
        "f1_macro": f1_score(y_true, y_pred, average="macro", zero_division=0),
        "f1_samples": f1_score(y_true, y_pred, average="samples", zero_division=0),
    }
    for idx, category in enumerate(categories):
        metrics[f"f1_{category}"] = f1_score(
            y_true[:, idx], y_pred[:, idx], zero_division=0
        )
    return metrics


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def compute_bert_embeddings(
    texts: list[str],
    model_name: str,
    max_length: int,
    device: torch.device,
    batch_size: int = 8,
) -> np.ndarray:
    """Mean-pooled last-hidden-state embeddings from a frozen, pretrained
    (not fine-tuned) BERT - no gradients."""
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name).to(device)
    model.eval()

    embeddings = []
    with torch.no_grad():
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encoding = tokenizer(
                batch,
                max_length=max_length,
                padding="max_length",
                truncation=True,
                return_tensors="pt",
            ).to(device)
            outputs = model(**encoding)
            mask = encoding["attention_mask"].unsqueeze(-1).float()
            summed = (outputs.last_hidden_state * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-9)
            pooled = summed / counts
            embeddings.append(pooled.cpu().numpy())

    del model
    return np.concatenate(embeddings, axis=0)


# ---------------------------------------------------------------------------
# Per-label sklearn/xgboost heads
# ---------------------------------------------------------------------------


def fit_per_label(
    factory: Callable[[np.ndarray], Any], X_train: np.ndarray, y_train: np.ndarray
) -> list[Any]:
    """Fit one binary estimator per label column - lets each label get its
    own class-imbalance weighting (scale_pos_weight, class_weight)."""
    models = []
    for i in range(y_train.shape[1]):
        y_col = y_train[:, i]
        model = factory(y_col)
        model.fit(X_train, y_col)
        models.append(model)
    return models


def predict_proba_per_label(models: list[Any], X: np.ndarray) -> np.ndarray:
    return np.stack([m.predict_proba(X)[:, 1] for m in models], axis=1)


def make_logreg_factory(use_class_weights: bool) -> Callable[[np.ndarray], Any]:
    return lambda y_col: LogisticRegression(
        max_iter=2000, class_weight="balanced" if use_class_weights else None
    )


def make_random_forest_factory(use_class_weights: bool, seed: int) -> Callable[[np.ndarray], Any]:
    return lambda y_col: RandomForestClassifier(
        n_estimators=300,
        random_state=seed,
        class_weight="balanced" if use_class_weights else None,
    )


def make_xgboost_factory(use_class_weights: bool, seed: int) -> Callable[[np.ndarray], Any]:
    def factory(y_col: np.ndarray) -> XGBClassifier:
        scale_pos_weight = 1.0
        if use_class_weights:
            n_pos = int(y_col.sum())
            n_neg = len(y_col) - n_pos
            scale_pos_weight = n_neg / max(n_pos, 1)
        return XGBClassifier(
            n_estimators=300,
            eval_metric="logloss",
            random_state=seed,
            scale_pos_weight=scale_pos_weight,
        )

    return factory


# ---------------------------------------------------------------------------
# BERT+NN (fine-tuned end-to-end), reusing train_bert_classifier.py
# ---------------------------------------------------------------------------


def run_bert_finetuned(
    train_texts: list[str],
    train_labels: list[list[int]],
    test_texts: list[str],
    test_labels: list[list[int]],
    categories: list[str],
    model_name: str,
    max_length: int,
    batch_size: int,
    epochs: int,
    learning_rate: float,
    device: torch.device,
) -> tuple[dict[str, float], np.ndarray]:
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name,
        num_labels=len(categories),
        problem_type="multi_label_classification",
    ).to(device)

    train_loader = DataLoader(
        LawsuitDataset(train_texts, train_labels, tokenizer, max_length),
        batch_size=batch_size,
        shuffle=True,
    )
    test_loader = DataLoader(
        LawsuitDataset(test_texts, test_labels, tokenizer, max_length),
        batch_size=batch_size,
    )

    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    total_steps = len(train_loader) * epochs
    from transformers import get_linear_schedule_with_warmup

    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=int(0.1 * total_steps),
        num_training_steps=total_steps,
    )

    for epoch in range(epochs):
        loss = train_epoch(model, train_loader, optimizer, scheduler, device)
        logger.info(f"  [bert_finetuned] epoch {epoch + 1}/{epochs} loss={loss:.4f}")

    model.eval()
    all_probs, all_labels = [], []
    with torch.no_grad():
        for batch in test_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            outputs = model(input_ids=input_ids, attention_mask=attention_mask)
            probs = torch.sigmoid(outputs.logits)
            all_probs.extend(probs.cpu().numpy())
            all_labels.extend(batch["labels"].numpy())

    del model
    probs = np.array(all_probs)
    y_pred = (probs > 0.5).astype(int)
    y_true = np.array(all_labels).astype(int)
    return compute_metrics(y_true, y_pred, categories), probs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config" / "event_extraction.toml")
    parser.add_argument("--data", type=Path, help="Path to reconciled training-data JSON (default: from config)")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "data" / "classification" / "model_comparison.json")
    parser.add_argument("--predictions-output", type=Path, default=REPO_ROOT / "data" / "classification" / "model_comparison_predictions.npz")
    parser.add_argument("--exclude-categories", nargs="+", default=["class_action"], metavar="CATEGORY")
    parser.add_argument("--test-split", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tfidf-max-features", type=int, default=20000)
    parser.add_argument("--bert-model", type=str, help="Default: from config")
    parser.add_argument("--bert-epochs", type=int, default=3)
    parser.add_argument("--bert-batch-size", type=int, help="Default: from config")
    parser.add_argument("--bert-learning-rate", type=float, default=2e-5)
    parser.add_argument("--no-class-weights", action="store_true", help="Disable class_weight='balanced'/scale_pos_weight everywhere")
    parser.add_argument("--skip-bert-finetune", action="store_true", help="Skip the bert_finetuned+nn branch (slowest, no GPU on this VM)")
    parser.add_argument("--skip-bert-frozen", action="store_true", help="Skip the bert_frozen+{logreg,xgboost,random_forest} branches")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    config = load_config(args.config)
    data_path = args.data or Path(config.get("reconciled_training_data_path", "data/classification/reconciled_training_data.json"))
    bert_model_name = args.bert_model or config.get("bert_model", "nlpaueb/legal-bert-base-uncased")
    bert_batch_size = args.bert_batch_size or config.get("bert_batch_size", 8)
    max_length = config.get("bert_max_length", 512)
    use_class_weights = not args.no_class_weights

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Using device: {device}")

    texts, labels, categories = load_training_data(
        data_path, drop_empty=False, input_field="summary", exclude_categories=args.exclude_categories
    )
    train_texts, test_texts, train_labels, test_labels = stratified_split(
        texts, labels, args.test_split, args.seed
    )
    logger.info(f"Train: {len(train_texts)}, Test: {len(test_texts)}, Categories: {categories}")

    y_train = np.array(train_labels)
    y_test = np.array(test_labels)

    results: dict[str, dict[str, float]] = {}
    predictions: dict[str, np.ndarray] = {}
    sklearn_heads = {
        "logreg": make_logreg_factory(use_class_weights),
        "xgboost": make_xgboost_factory(use_class_weights, args.seed),
        "random_forest": make_random_forest_factory(use_class_weights, args.seed),
    }

    # --- TF-IDF branch ---
    logger.info("=== tfidf ===")
    vectorizer = TfidfVectorizer(max_features=args.tfidf_max_features, ngram_range=(1, 2))
    X_train_tfidf = vectorizer.fit_transform(train_texts)
    X_test_tfidf = vectorizer.transform(test_texts)

    for head_name, factory in sklearn_heads.items():
        config_name = f"tfidf+{head_name}"
        logger.info(f"Training {config_name}...")
        start = time.time()
        models = fit_per_label(factory, X_train_tfidf, y_train)
        elapsed = time.time() - start
        probs = predict_proba_per_label(models, X_test_tfidf)
        y_pred = (probs > 0.5).astype(int)
        metrics = compute_metrics(y_test, y_pred, categories)
        metrics["train_time_sec"] = elapsed
        results[config_name] = metrics
        predictions[config_name] = probs

    # --- Frozen-BERT branch ---
    if not args.skip_bert_frozen:
        logger.info("=== bert_frozen (embeddings, no fine-tuning) ===")
        start = time.time()
        X_train_bert = compute_bert_embeddings(train_texts, bert_model_name, max_length, device)
        X_test_bert = compute_bert_embeddings(test_texts, bert_model_name, max_length, device)
        embed_time = time.time() - start
        logger.info(f"Embedding extraction took {embed_time:.1f}s")

        for head_name, factory in sklearn_heads.items():
            config_name = f"bert_frozen+{head_name}"
            logger.info(f"Training {config_name}...")
            start = time.time()
            models = fit_per_label(factory, X_train_bert, y_train)
            elapsed = time.time() - start
            probs = predict_proba_per_label(models, X_test_bert)
            y_pred = (probs > 0.5).astype(int)
            metrics = compute_metrics(y_test, y_pred, categories)
            metrics["train_time_sec"] = elapsed
            results[config_name] = metrics
            predictions[config_name] = probs

    # --- Fine-tuned BERT+NN branch ---
    if not args.skip_bert_finetune:
        logger.info("=== bert_finetuned+nn ===")
        start = time.time()
        metrics, probs = run_bert_finetuned(
            train_texts, train_labels, test_texts, test_labels, categories,
            bert_model_name, max_length, bert_batch_size, args.bert_epochs,
            args.bert_learning_rate, device,
        )
        metrics["train_time_sec"] = time.time() - start
        results["bert_finetuned+nn"] = metrics
        predictions["bert_finetuned+nn"] = probs

    # --- Report ---
    metric_cols = ["f1_macro", "f1_micro", "f1_samples", "hamming_loss"] + [
        f"f1_{c}" for c in categories
    ] + ["train_time_sec"]

    col_width = max(12, max(len(m) for m in metric_cols) + 2)
    header = f"{'config':<28}" + "".join(f"{m:>{col_width}}" for m in metric_cols)
    print("\n=== Model Comparison ===")
    print(header)
    for config_name, metrics in results.items():
        row = f"{config_name:<28}" + "".join(
            f"{metrics.get(m, float('nan')):>{col_width}.4f}" for m in metric_cols
        )
        print(row)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(
            {
                "metadata": {
                    "categories": categories,
                    "excluded_categories": args.exclude_categories,
                    "n_train": len(train_texts),
                    "n_test": len(test_texts),
                    "test_split": args.test_split,
                    "seed": args.seed,
                    "class_weights": use_class_weights,
                    "bert_model": bert_model_name,
                },
                "results": results,
            },
            f,
            indent=2,
        )
    logger.info(f"Results saved to: {args.output}")

    # Raw test-set probabilities per config, for notebooks/diagnostic plots
    # (ROC/PR curves, confusion matrices) - the aggregate metrics above don't
    # retain enough information to draw those.
    np.savez(
        args.predictions_output,
        y_test=y_test,
        categories=np.array(categories, dtype=object),
        **{f"proba__{name}": arr for name, arr in predictions.items()},
    )
    logger.info(f"Predictions saved to: {args.predictions_output}")


if __name__ == "__main__":
    main()
