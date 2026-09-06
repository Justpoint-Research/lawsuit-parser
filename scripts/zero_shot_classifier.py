#!/usr/bin/env python3
"""Zero-shot lawsuit classifier using Qwen/Ollama to generate training data.

This script classifies lawsuits into categories (product liability, personal injury,
class action) using an LLM. The output serves as labeled training data for training
a faster BERT-based classifier.

Usage:
    # Classify all cases
    python scripts/zero_shot_classifier.py

    # Classify specific cases
    python scripts/zero_shot_classifier.py case_67 mdl-1954

    # Force re-classification of existing cases
    python scripts/zero_shot_classifier.py --force

    # Use different config file
    python scripts/zero_shot_classifier.py --config config/custom.toml
"""

import argparse
import json
import logging
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from tqdm import tqdm

# Add parent directory to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)


# Classification response schema
CLASSIFICATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["product_liability", "personal_injury", "class_action"]
                    },
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["category", "confidence", "reasoning"],
            },
        },
    },
    "required": ["labels"],
}


def load_config(config_path: Path) -> dict[str, Any]:
    """Load configuration from TOML file."""
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
    return config.get("lawsuit_classification", {})


def load_llm_prompt_template() -> str:
    """Load the lawsuit classification prompt template."""
    prompt_config_path = REPO_ROOT / "config" / "llm_prompts.toml"
    with open(prompt_config_path, "rb") as f:
        prompts = tomllib.load(f)
    return prompts["lawsuit_classification"]["template"]


def build_classification_prompt(
    case_id: str,
    case_metadata: dict[str, Any],
    text_excerpt: str,
    page_count: int,
) -> str:
    """Build the classification prompt from the template."""
    template = load_llm_prompt_template()

    # Build context section with available metadata
    caption = case_metadata.get("caption", "Unknown")
    court = case_metadata.get("court", "Unknown")
    case_type = case_metadata.get("case_type", "Unknown")

    return template.format(
        case_id=case_id,
        caption=caption,
        court=court,
        case_type=case_type,
        text_excerpt=text_excerpt[:30000],  # Limit to prevent token overflow
        page_count=page_count,
    )


def call_ollama(
    model: str,
    base_url: str,
    prompt: str,
    schema: dict,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Call Ollama API with JSON schema constraint."""
    response = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"]
    return json.loads(content)


def load_case_context(case_dir: Path) -> dict[str, Any]:
    """Load case-level metadata from the database export JSON.

    Returns a dict with: caption, court, case_type, case_received_date,
    case_status, efiling_status, and documents list.
    """
    case_json = case_dir / f"{case_dir.name}.json"
    if not case_json.exists():
        return {}

    try:
        with open(case_json) as f:
            case_data = json.load(f)
        case_info = case_data.get("case_info", {})
        documents_meta = case_data.get("documents", [])

        return {
            "caption": case_info.get("caption"),
            "court": case_info.get("court"),
            "case_type": case_info.get("case_type"),
            "case_received_date": case_info.get("case_received_date"),
            "case_status": case_info.get("case_status"),
            "efiling_status": case_info.get("efiling_status"),
            "case_id_official": case_info.get("case_id"),
            "documents_metadata": documents_meta,
        }
    except Exception as e:
        logger.warning(f"Failed to load case context: {e}")
        return {}


def load_document_text(doc_path: Path, max_chars: int) -> str:
    """Load document text from .txt or .docling.json file."""
    # Try .txt file first (canonical text)
    txt_path = doc_path.parent / f"{doc_path.stem}.txt"
    if txt_path.exists():
        try:
            with open(txt_path, encoding="utf-8") as f:
                return f.read()[:max_chars]
        except Exception as e:
            logger.warning(f"Failed to load {txt_path}: {e}")

    # Try docling output
    docling_dir = doc_path.parent.parent / "docling" / "documents"
    docling_path = docling_dir / f"{doc_path.stem}.docling.json"
    if docling_path.exists():
        try:
            with open(docling_path) as f:
                docling_data = json.load(f)
            text = "\n".join(
                item.get("text", "")
                for item in docling_data.get("texts", [])
            )
            return text[:max_chars]
        except Exception as e:
            logger.warning(f"Failed to load {docling_path}: {e}")

    return ""


def select_documents_for_classification(
    case_dir: Path,
    doc_count: int,
) -> list[Path]:
    """Select documents to use for classification, prioritizing complaints."""
    docs_dir = case_dir / "documents"
    if not docs_dir.exists():
        return []

    # Get all PDFs
    pdf_files = sorted(docs_dir.glob("*.pdf"))
    if not pdf_files:
        return []

    # Prioritize documents that look like complaints/initial filings
    complaint_keywords = [
        "complaint", "petition", "summons", "verified",
        "amended_complaint", "class_action_complaint"
    ]

    priority_docs = []
    other_docs = []

    for pdf in pdf_files:
        if any(kw in pdf.stem.lower() for kw in complaint_keywords):
            priority_docs.append(pdf)
        else:
            other_docs.append(pdf)

    # Return priority docs first, then others, up to doc_count
    selected = (priority_docs + other_docs)[:doc_count]
    return selected


def classify_case(
    case_id: str,
    case_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Classify a single case using LLM."""
    logger.info(f"Classifying {case_id}...")

    # Load case metadata from database export
    case_metadata = load_case_context(case_dir)

    if not case_metadata:
        logger.warning(f"No metadata found for {case_id}")
        return None

    # Select documents
    doc_count = config.get("classification_doc_count", 2)
    documents = select_documents_for_classification(case_dir, doc_count)

    if not documents:
        logger.warning(f"No documents found for {case_id}")
        return None

    # Gather text from documents
    page_count = config.get("classification_page_count", 3)
    max_chars_per_page = 3000

    text_parts = []

    # Add document names from metadata as additional context
    doc_names = []
    for doc_meta in case_metadata.get("documents_metadata", [])[:doc_count]:
        if doc_meta.get("document_name"):
            doc_names.append(doc_meta["document_name"])

    if doc_names:
        text_parts.append(f"=== Document Names from Case Metadata ===\n" + "\n".join(doc_names))

    # Add actual document text
    for doc_path in documents:
        doc_text = load_document_text(doc_path, max_chars_per_page * page_count)
        if doc_text:
            text_parts.append(f"=== Document: {doc_path.name} ===\n{doc_text}")

    if not text_parts:
        logger.warning(f"No text extracted for {case_id}")
        return None

    text_excerpt = "\n\n".join(text_parts)

    # Build prompt
    prompt = build_classification_prompt(
        case_id=case_id,
        case_metadata=case_metadata,
        text_excerpt=text_excerpt,
        page_count=page_count * len(documents),
    )

    # Call LLM
    try:
        result = call_ollama(
            model=config["llm_model"],
            base_url=config["llm_base_url"],
            prompt=prompt,
            schema=CLASSIFICATION_RESPONSE_SCHEMA,
            timeout=config.get("timeout", 120.0),
        )

        # Filter by confidence threshold
        min_confidence = config.get("min_confidence", 0.6)
        labels = [
            label for label in result.get("labels", [])
            if label.get("confidence", 0.0) >= min_confidence
        ]

        return {
            "case_id": case_id,
            "caption": case_metadata.get("caption"),
            "court": case_metadata.get("court"),
            "case_type": case_metadata.get("case_type"),
            "case_received_date": case_metadata.get("case_received_date"),
            "case_status": case_metadata.get("case_status"),
            "labels": labels,
            "text_excerpt": text_excerpt[:1000],  # Store snippet for review
            "classified_at": datetime.now().isoformat(),
            "model": config["llm_model"],
            "documents_used": [doc.name for doc in documents],
            "document_names": doc_names,
        }

    except Exception as e:
        logger.error(f"Failed to classify {case_id}: {e}")
        return None


def find_cases(data_root: Path, case_ids: list[str] | None = None) -> list[Path]:
    """Find case directories to process."""
    if case_ids:
        # Process specific cases
        case_dirs = []
        for case_id in case_ids:
            case_dir = data_root / case_id
            if case_dir.exists() and case_dir.is_dir():
                case_dirs.append(case_dir)
            else:
                logger.warning(f"Case directory not found: {case_dir}")
        return case_dirs
    else:
        # Process all cases
        return [d for d in data_root.iterdir() if d.is_dir()]


def main():
    parser = argparse.ArgumentParser(
        description="Zero-shot lawsuit classifier using Qwen/Ollama",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "cases",
        nargs="*",
        help="Case IDs to classify (default: all cases)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "config" / "event_extraction.toml",
        help="Path to config file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON file (default: from config)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify cases already in output file",
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

    # Load config
    config = load_config(args.config)

    # Determine output path
    output_path = args.output or Path(config.get(
        "training_data_path",
        "data/classification/training_data.json"
    ))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing results if not forcing
    existing_results = {}
    if output_path.exists() and not args.force:
        with open(output_path) as f:
            data = json.load(f)
            existing_results = {r["case_id"]: r for r in data.get("results", [])}
        logger.info(f"Loaded {len(existing_results)} existing classifications")

    # Find cases
    data_root = Path(config.get("data_root", "data/cases"))
    if not data_root.exists():
        # Try paths.data_root from config
        config_file = args.config
        with open(config_file, "rb") as f:
            full_config = tomllib.load(f)
        data_root = Path(full_config.get("paths", {}).get("data_root", "data/cases"))

    case_dirs = find_cases(data_root, args.cases or None)
    logger.info(f"Found {len(case_dirs)} cases to process")

    # Classify cases
    results = []
    for case_dir in tqdm(case_dirs, desc="Classifying cases"):
        case_id = case_dir.name

        # Skip if already classified and not forcing
        if case_id in existing_results and not args.force:
            results.append(existing_results[case_id])
            continue

        result = classify_case(case_id, case_dir, config)
        if result:
            results.append(result)

    # Save results
    output_data = {
        "metadata": {
            "created_at": datetime.now().isoformat(),
            "model": config["llm_model"],
            "config_file": str(args.config),
            "total_cases": len(results),
            "categories": config.get("categories", []),
        },
        "results": results,
    }

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    logger.info(f"Saved {len(results)} classifications to {output_path}")

    # Print summary statistics
    category_counts = {}
    for result in results:
        for label in result.get("labels", []):
            category = label["category"]
            category_counts[category] = category_counts.get(category, 0) + 1

    print("\n=== Classification Summary ===")
    print(f"Total cases classified: {len(results)}")
    print("\nCategory distribution:")
    for category, count in sorted(category_counts.items()):
        print(f"  {category}: {count} cases ({count/len(results)*100:.1f}%)")

    print(f"\nTraining data saved to: {output_path}")


if __name__ == "__main__":
    main()
