#!/usr/bin/env python3
"""Generate a local-LLM factual summary of each case, sized for BERT input.

The BERT lawsuit classifier (scripts/train_bert_classifier.py) works best
when its input is a dense factual summary of the case rather than the first
~1000 characters of the raw filings (which are mostly caption/summons
boilerplate). This script produces one summary per case with the configured
local Ollama model and guarantees it fits the classifier's token window.

Design notes:

  * The prompt (config/llm_prompts.toml -> [classification_summary]) is
    label-agnostic: it never names the target categories and never asks a
    yes/no classification question. The same fixed prompt runs for every
    case, so the summary carries the discriminative facts without leaking
    the label the classifier is trying to predict.

  * Token budget is enforced against the *actual* string the trainer feeds
    the tokenizer (train_bert_classifier.build_bert_input_text) using the
    BERT tokenizer itself - not a heuristic. If a summary overflows, the
    model is re-prompted once with a tighter word target; if it still
    overflows, the summary is hard-truncated by tokens.

  * Output is resumable and dedups cases whose assembled prompt is byte-for
    -byte identical (shared boilerplate filings), same pattern as
    zero_shot_classifier.py's prompt cache.

Usage:
    # Summarize every case in the configured case_sources
    python scripts/summarize_cases_for_classification.py

    # Specific cases, overwriting any existing summary
    python scripts/summarize_cases_for_classification.py case_1 case_5 --force

    # Then merge into the training data and retrain:
    python scripts/reconcile_classifications.py --require-all-providers
    python scripts/train_bert_classifier.py --input-field summary
"""

import argparse
import hashlib
import json
import logging
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from zero_shot_classifier import (  # noqa: E402
    call_ollama,
    find_cases,
    load_case_context,
    load_document_text,
    select_documents_for_classification,
    select_ollama_num_ctx,
)
from train_bert_classifier import build_bert_input_text  # noqa: E402

logger = logging.getLogger(__name__)

SUMMARY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

# [CLS] + [SEP] wrap the input, so the assembled text must be <= max_length - 2.
SPECIAL_TOKEN_ALLOWANCE = 2


def load_summary_prompt_template(prompt_config_path: Path) -> str:
    with open(prompt_config_path, "rb") as f:
        prompts = tomllib.load(f)
    return prompts["classification_summary"]["template"]


def gather_case_text(
    case_dir: Path,
    case_metadata: dict[str, Any],
    config: dict[str, Any],
    data_root: Path,
    output_root: Path,
    input_max_chars: int,
) -> str:
    """Assemble the document text handed to the summarizer.

    Same document selection as zero_shot_classifier (complaint-type filings
    first, per-document page cap) but capped overall at input_max_chars so
    the local model stays on its fast context tier.
    """
    page_count = config.get("classification_page_count", 3)
    max_chars_per_page = 3000

    parts: list[str] = []

    doc_names = [
        dm["document_name"]
        for dm in case_metadata.get("documents_metadata", [])
        if dm.get("document_name")
    ]
    if doc_names:
        parts.append("=== Document Names from Case Metadata ===\n" + "\n".join(doc_names))

    for doc_path in select_documents_for_classification(case_dir):
        doc_text = load_document_text(
            doc_path, max_chars_per_page * page_count, data_root, output_root
        )
        if doc_text:
            parts.append(f"=== Document: {doc_path.name} ===\n{doc_text}")

    return "\n\n".join(parts)[:input_max_chars]


def build_summary_prompt(
    template: str,
    case_metadata: dict[str, Any],
    text_excerpt: str,
    max_words: int,
) -> str:
    return template.format(
        caption=case_metadata.get("caption") or "Unknown",
        court=case_metadata.get("court") or "Unknown",
        case_type=case_metadata.get("case_type") or "Unknown",
        text_excerpt=text_excerpt,
        max_words=max_words,
    )


def request_summary(
    prompt: str,
    model: str,
    base_url: str,
    config: dict[str, Any],
) -> str:
    num_ctx = select_ollama_num_ctx(prompt, config)
    result = call_ollama(
        model=model,
        base_url=base_url,
        prompt=prompt,
        schema=SUMMARY_RESPONSE_SCHEMA,
        num_ctx=num_ctx,
        timeout=config.get("ollama_timeout", 240.0),
    )
    return " ".join((result.get("summary") or "").split())


def token_count(tokenizer, case_metadata: dict[str, Any], summary: str) -> int:
    text = build_bert_input_text({**case_metadata, "summary": summary}, input_field="summary")
    return len(tokenizer(text, add_special_tokens=True)["input_ids"])


def truncate_summary_to_budget(
    tokenizer,
    case_metadata: dict[str, Any],
    summary: str,
    max_tokens: int,
) -> str:
    """Drop whole words off the end of `summary` until the assembled input fits."""
    words = summary.split()
    # Coarse cut first (proportional), then tighten word-by-word.
    n = len(words)
    while n > 0:
        candidate = " ".join(words[:n])
        if token_count(tokenizer, case_metadata, candidate) <= max_tokens:
            return candidate
        n -= max(1, n // 20)
    return ""


def summarize_case(
    case_dir: Path,
    config: dict[str, Any],
    data_root: Path,
    output_root: Path,
    tokenizer,
    template: str,
    *,
    max_words: int,
    input_max_chars: int,
    max_tokens: int,
    prompt_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Produce one within-budget summary for a case.

    Returns a dict with: summary, token_count, truncated, reasked,
    input_chars, prompt_hash - or None when the case has no usable text.
    """
    case_id = case_dir.name
    case_metadata = load_case_context(case_dir)
    if not case_metadata:
        logger.warning(f"{case_id}: no case metadata, skipping")
        return None

    text_excerpt = gather_case_text(
        case_dir, case_metadata, config, data_root, output_root, input_max_chars
    )
    if not text_excerpt.strip():
        logger.warning(f"{case_id}: no document text, skipping")
        return None

    model = config.get("llm_model", "qwen3:30b-a3b")
    base_url = config.get("llm_base_url", "http://localhost:11434")

    prompt = build_summary_prompt(template, case_metadata, text_excerpt, max_words)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    if prompt_cache is not None and prompt_hash in prompt_cache:
        cached = prompt_cache[prompt_hash]
        logger.info(f"{case_id}: identical prompt to a prior case, reusing summary")
        summary = cached["summary"]
    else:
        summary = request_summary(prompt, model, base_url, config)

    reasked = False
    truncated = False
    n_tokens = token_count(tokenizer, case_metadata, summary)

    if n_tokens > max_tokens:
        # Re-ask once with a word target scaled down from what overflowed.
        tighter = max(40, int(max_words * max_tokens / n_tokens * 0.85))
        logger.info(
            f"{case_id}: summary {n_tokens} tok > {max_tokens}, re-asking for <= {tighter} words"
        )
        retry_prompt = build_summary_prompt(template, case_metadata, text_excerpt, tighter)
        summary = request_summary(retry_prompt, model, base_url, config)
        reasked = True
        n_tokens = token_count(tokenizer, case_metadata, summary)

    if n_tokens > max_tokens:
        logger.warning(f"{case_id}: still {n_tokens} tok after re-ask, truncating")
        summary = truncate_summary_to_budget(tokenizer, case_metadata, summary, max_tokens)
        truncated = True
        n_tokens = token_count(tokenizer, case_metadata, summary)

    if prompt_cache is not None:
        prompt_cache[prompt_hash] = {"summary": summary}

    return {
        "summary": summary,
        "token_count": n_tokens,
        "truncated": truncated,
        "reasked": reasked,
        "input_chars": len(text_excerpt),
        "prompt_hash": prompt_hash,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate local-LLM case summaries for the BERT classifier",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "cases",
        nargs="*",
        help="Case IDs to summarize (default: the cases present in the LLM "
        "training data - the only ones that get trained/evaluated on)",
    )
    parser.add_argument(
        "--all-cases",
        action="store_true",
        help="Summarize every case in case_sources, not just the "
        "LLM-labelled ones (e.g. to pre-compute summaries for inference)",
    )
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "config" / "event_extraction.toml"
    )
    parser.add_argument(
        "--prompt-config", type=Path, default=REPO_ROOT / "config" / "llm_prompts.toml"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON (default: config's case_summaries_path)",
    )
    parser.add_argument(
        "--max-words",
        type=int,
        help="Word target asked of the LLM (default: config's summary_max_words)",
    )
    parser.add_argument(
        "--input-max-chars",
        type=int,
        help="Cap on document text fed to the summarizer "
        "(default: config's summary_input_max_chars)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Regenerate summaries that already exist"
    )
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N cases")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    with open(args.config, "rb") as f:
        full_config = tomllib.load(f)
    config = full_config.get("lawsuit_classification", {})

    data_root = Path(config.get("data_root", "data/cases"))
    if not data_root.exists():
        data_root = Path(full_config.get("paths", {}).get("data_root", "data/cases"))
    output_root = Path(full_config.get("paths", {}).get("output_root", "data/extraction"))

    output_path = args.output or REPO_ROOT / config.get(
        "case_summaries_path", "data/classification/case_summaries.json"
    )
    max_words = args.max_words or config.get("summary_max_words", 200)
    input_max_chars = args.input_max_chars or config.get("summary_input_max_chars", 30000)
    bert_model = config.get("bert_model", "nlpaueb/legal-bert-base-uncased")
    max_length = config.get("bert_max_length", 512)
    max_tokens = max_length - SPECIAL_TOKEN_ALLOWANCE

    template = load_summary_prompt_template(args.prompt_config)

    logger.info(f"Loading BERT tokenizer for budgeting: {bert_model}")
    tokenizer = AutoTokenizer.from_pretrained(bert_model)

    case_sources = config.get("case_sources", ["ny_sample"])
    wanted_cases = args.cases or None
    if wanted_cases is None and not args.all_cases:
        # Default: only the cases that have LLM labels (the train/eval set).
        training_data_path = REPO_ROOT / config.get(
            "training_data_path", "data/classification/training_data.json"
        )
        if training_data_path.exists():
            with open(training_data_path) as f:
                wanted_cases = [r["case_id"] for r in json.load(f).get("results", [])]
            logger.info(
                f"Restricting to {len(wanted_cases)} case(s) from {training_data_path.name} "
                f"(use --all-cases to summarize the whole pool)"
            )
        else:
            logger.warning(
                f"{training_data_path} not found - summarizing the whole pool"
            )

    case_dirs = find_cases(data_root, case_sources, wanted_cases)
    logger.info(f"Found {len(case_dirs)} case(s) (sources: {case_sources})")
    if args.limit > 0:
        case_dirs = case_dirs[: args.limit]

    existing: dict[str, Any] = {}
    if output_path.exists():
        with open(output_path) as f:
            existing = json.load(f).get("summaries", {})
        logger.info(f"Loaded {len(existing)} existing summary/summaries from {output_path}")

    summaries = dict(existing)
    prompt_cache: dict[str, dict[str, Any]] = {}
    n_new = n_reasked = n_truncated = 0

    for case_dir in case_dirs:
        case_id = case_dir.name
        if case_id in summaries and not args.force:
            continue
        result = summarize_case(
            case_dir,
            config,
            data_root,
            output_root,
            tokenizer,
            template,
            max_words=max_words,
            input_max_chars=input_max_chars,
            max_tokens=max_tokens,
            prompt_cache=prompt_cache,
        )
        if result is None:
            continue
        summaries[case_id] = result
        n_new += 1
        n_reasked += int(result["reasked"])
        n_truncated += int(result["truncated"])
        logger.info(
            f"{case_id}: {result['token_count']} tok"
            f"{' (re-asked)' if result['reasked'] else ''}"
            f"{' (truncated)' if result['truncated'] else ''}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(
            {
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "model": config.get("llm_model", "qwen3:30b-a3b"),
                    "bert_model": bert_model,
                    "max_input_tokens": max_tokens,
                    "summary_max_words": max_words,
                    "summary_input_max_chars": input_max_chars,
                    "total_summaries": len(summaries),
                },
                "summaries": summaries,
            },
            f,
            indent=2,
        )

    token_counts = [s["token_count"] for s in summaries.values()]
    print(f"\n=== Summaries ({len(summaries)} total, {n_new} new this run) ===")
    print(f"Re-asked: {n_reasked}   Truncated: {n_truncated}")
    if token_counts:
        token_counts.sort()
        print(
            f"Token counts: min {token_counts[0]}, "
            f"median {token_counts[len(token_counts) // 2]}, "
            f"max {token_counts[-1]} (budget {max_tokens})"
        )
    over = [cid for cid, s in summaries.items() if s["token_count"] > max_tokens]
    if over:
        print(f"WARNING: {len(over)} summary/summaries still over budget: {over}")
    print(f"\nSaved to: {output_path}")


if __name__ == "__main__":
    main()
