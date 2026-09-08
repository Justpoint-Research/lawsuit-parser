#!/usr/bin/env python3
"""Zero-shot lawsuit classifier using multiple LLM providers to generate training data.

This script classifies lawsuits into categories (product liability, personal injury,
class action) using an LLM, querying one or more providers - local Ollama (free),
Claude (Anthropic API), and Gemini (Google API). Each case's per-provider results
are stored side by side (see "provider_results" in the output), so a case classified
by Ollama today can get Claude/Gemini results added in a later run without losing
the earlier ones. Run scripts/reconcile_classifications.py afterwards to combine
multiple providers' results into one label set for BERT training.

Cloud providers cost money per call and are not run unless you ask for them with
--providers - nothing is ever silently sent to a paid API.

Usage:
    # Classify all cases with the local Ollama model (free)
    python scripts/zero_shot_classifier.py --providers ollama

    # Add Claude results to the same cases (needs ANTHROPIC_API_KEY set)
    python scripts/zero_shot_classifier.py --providers anthropic

    # Add Gemini results too (needs `gcloud auth application-default login`)
    python scripts/zero_shot_classifier.py --providers gemini

    # Query multiple providers in one run
    python scripts/zero_shot_classifier.py --providers ollama anthropic gemini

    # Classify specific cases
    python scripts/zero_shot_classifier.py case_67 mdl-1954 --providers ollama

    # Force re-classification of cases that already have results for the
    # requested provider(s)
    python scripts/zero_shot_classifier.py --providers ollama --force

    # Use different config file
    python scripts/zero_shot_classifier.py --config config/custom.toml
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from tqdm import tqdm

PROVIDERS = ("ollama", "anthropic", "gemini")
PROVIDER_API_KEY_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
}

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
                    "reasoning": {
                        "type": "string",
                        "description": "A short paragraph (3-5 sentences) justifying "
                        "this label, citing the specific document(s) it draws on by name.",
                    },
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
    max_text_chars: int = 100_000,
) -> str:
    """Build the classification prompt from the template.

    max_text_chars is the last-resort cap on the combined text of every
    document in the case (see select_documents_for_classification - now all
    of them, not a sample) - only bites for unusually document-heavy cases.
    Complaint-type documents are ordered first, so a truncation here drops
    from the end (later, less central documents) rather than the complaint.
    """
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
        text_excerpt=text_excerpt[:max_text_chars],
        page_count=page_count,
    )


# ~chars/token measured for this corpus (qwen3 tokenizer).
CHARS_PER_TOKEN_ESTIMATE = 2.8

# Reserved for the model's JSON response, plus estimation slop.
OUTPUT_TOKEN_BUFFER = 1500


def select_ollama_num_ctx(prompt: str, config: dict[str, Any]) -> int:
    """Pick the smallest of two context tiers that fits `prompt`.

    num_ctx sizes the KV cache for the whole model load, not per-request; on
    this VRAM-constrained GPU, a bigger num_ctx forces part of the model
    onto CPU, which cost ~10x wall-clock (not just a one-time reload). So
    default small/GPU-resident, only bump to the large tier when needed.
    """
    default_ctx = config.get("ollama_num_ctx", 40960)
    max_ctx = config.get("ollama_num_ctx_max", 100000)
    needed = int(len(prompt) / CHARS_PER_TOKEN_ESTIMATE) + OUTPUT_TOKEN_BUFFER
    if needed <= default_ctx:
        return default_ctx
    if needed > max_ctx:
        logger.warning(
            f"Estimated prompt tokens ({needed}) exceed ollama_num_ctx_max "
            f"({max_ctx}) - proceeding anyway, response may be truncated."
        )
    return max_ctx


def call_ollama(
    model: str,
    base_url: str,
    prompt: str,
    schema: dict,
    num_ctx: int,
    timeout: float = 240.0,
) -> dict[str, Any]:
    """Call Ollama API with JSON schema constraint."""
    response = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": num_ctx},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"]
    return json.loads(content)


def _with_additional_properties_false(schema: dict) -> dict:
    """Return a deep copy of a JSON schema with `additionalProperties: false`
    added to every object node.

    Claude's structured-output (`output_config.format`) requires this for
    strict schema validation; Ollama and Gemini accept the schema as-is, so
    this is only applied for the Claude call.
    """
    schema = json.loads(json.dumps(schema))  # cheap deep copy

    def add_recursively(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
            for value in node.values():
                add_recursively(value)
        elif isinstance(node, list):
            for item in node:
                add_recursively(item)

    add_recursively(schema)
    return schema


def call_claude(
    model: str,
    prompt: str,
    schema: dict,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Call the Claude API with JSON-schema constrained output.

    Reads the API key from the ANTHROPIC_API_KEY env var (the anthropic
    SDK's own default credential resolution) - nothing else to configure.
    """
    import anthropic

    client = anthropic.Anthropic()
    response = client.with_options(timeout=timeout).messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": _with_additional_properties_false(schema),
            }
        },
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def call_gemini(
    model: str,
    prompt: str,
    schema: dict,
    project: str | None = None,
    location: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Call Gemini via Vertex AI with JSON-schema constrained output.

    Authenticates with Application Default Credentials - run
    `gcloud auth application-default login` once (no API key needed). The
    project and location come from the config file, else the
    GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION env vars, else the active
    gcloud config.
    """
    from google import genai

    client = genai.Client(
        vertexai=True,
        project=project or _default_gcloud_project(),
        location=location or os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_json_schema": schema,
        },
    )
    return json.loads(response.text)


def _default_gcloud_project() -> str | None:
    """The project id from GOOGLE_CLOUD_PROJECT, else `gcloud config`."""
    for env_var in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCP_PROJECT"):
        if os.environ.get(env_var):
            return os.environ[env_var]
    try:
        import google.auth

        _, project = google.auth.default()
        return project
    except Exception:
        return None


def check_provider_credentials(provider: str) -> None:
    """Fail fast with a clear message if a cloud provider's credentials aren't
    available, rather than a deep SDK stack trace on the first call."""
    env_var = PROVIDER_API_KEY_ENV_VARS.get(provider)
    if env_var and not os.environ.get(env_var):
        raise SystemExit(
            f"{env_var} is not set - required to call the '{provider}' provider. "
            f"export {env_var}=... and re-run."
        )
    if provider == "gemini":
        try:
            import google.auth

            google.auth.default()
        except Exception:
            raise SystemExit(
                "No Google Application Default Credentials found - required to "
                "call the 'gemini' provider via Vertex AI. Run "
                "`gcloud auth application-default login` and re-run."
            )
        if not _default_gcloud_project():
            raise SystemExit(
                "No Google Cloud project configured for the 'gemini' provider. "
                "Set GOOGLE_CLOUD_PROJECT, run `gcloud config set project <id>`, "
                "or set gemini_project in the config file."
            )


def call_provider(
    provider: str,
    prompt: str,
    schema: dict,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch to the given provider's API, returning parsed {"labels": [...]}."""
    timeout = config.get("timeout", 120.0)
    if provider == "ollama":
        return call_ollama(
            model=config["llm_model"],
            base_url=config["llm_base_url"],
            prompt=prompt,
            schema=schema,
            num_ctx=select_ollama_num_ctx(prompt, config),
            timeout=config.get("ollama_timeout", 240.0),
        )
    elif provider == "anthropic":
        return call_claude(
            model=config.get("anthropic_model", "claude-opus-5"),
            prompt=prompt,
            schema=schema,
            timeout=timeout,
        )
    elif provider == "gemini":
        return call_gemini(
            model=config.get("gemini_model", "gemini-2.5-flash"),
            prompt=prompt,
            schema=schema,
            project=config.get("gemini_project"),
            location=config.get("gemini_location"),
            timeout=timeout,
        )
    else:
        raise ValueError(f"Unknown provider: {provider!r} (expected one of {PROVIDERS})")


def provider_model_name(provider: str, config: dict[str, Any]) -> str:
    """The configured model name for a provider, for recording in output."""
    if provider == "ollama":
        return config["llm_model"]
    elif provider == "anthropic":
        return config.get("anthropic_model", "claude-opus-5")
    elif provider == "gemini":
        return config.get("gemini_model", "gemini-2.5-flash")
    raise ValueError(f"Unknown provider: {provider!r}")


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


def load_document_text(doc_path: Path, max_chars: int, data_root: Path, output_root: Path) -> str:
    """Load document text from .txt or .docling.json file.

    Docling output lives under output_root (data/extraction), not next to
    the source PDF - see lawsuit_parser.parsers.batch.get_docling_dir.
    """
    from lawsuit_parser.parsers.batch import get_docling_dir

    # Try .txt file first (canonical text) - written next to the PDF by
    # CaseExporter's own extract_text pass, unrelated to Docling's output.
    txt_path = doc_path.parent / f"{doc_path.stem}.txt"
    if txt_path.exists():
        try:
            with open(txt_path, encoding="utf-8") as f:
                return f.read()[:max_chars]
        except Exception as e:
            logger.warning(f"Failed to load {txt_path}: {e}")

    # Try docling output
    docling_dir = get_docling_dir(doc_path, data_root, output_root)
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


def select_documents_for_classification(case_dir: Path) -> list[Path]:
    """Select every document in the case for classification, complaints
    (initial filings) ordered first.

    All of a case's documents are used - not a capped sample - so a label
    can't be missed just because the deciding allegation is in a later
    filing. Complaint-type documents are still ordered first purely so that
    if the overall prompt budget (see max_text_chars) ever has to truncate,
    it drops from the least central documents, not the complaint itself.
    """
    docs_dir = case_dir / "documents"
    if not docs_dir.exists():
        return []

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

    return priority_docs + other_docs


def classify_case(
    case_id: str,
    case_dir: Path,
    config: dict[str, Any],
    data_root: Path,
    output_root: Path,
    provider: str,
    prompt_cache: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Classify a single case with one LLM provider.

    Returns the case's shared (provider-independent) fields plus a single
    "provider_results" entry for `provider` - see main()'s merge logic for
    how a case accumulates entries from multiple providers across runs.
    """
    logger.info(f"Classifying {case_id} with {provider}...")

    # Load case metadata from database export
    case_metadata = load_case_context(case_dir)

    if not case_metadata:
        logger.warning(f"No metadata found for {case_id}")
        return None

    # Select documents - every document in the case, see
    # select_documents_for_classification's docstring for why.
    documents = select_documents_for_classification(case_dir)

    if not documents:
        logger.warning(f"No documents found for {case_id}")
        return None

    # Gather text from documents. classification_page_count still caps how
    # much of any *one* document is read (a single huge exhibit shouldn't
    # crowd out every other document's text), independent of how many
    # documents there are.
    page_count = config.get("classification_page_count", 3)
    max_chars_per_page = 3000

    text_parts = []

    # Add document names from metadata as additional context
    doc_names = []
    for doc_meta in case_metadata.get("documents_metadata", []):
        if doc_meta.get("document_name"):
            doc_names.append(doc_meta["document_name"])

    if doc_names:
        text_parts.append(f"=== Document Names from Case Metadata ===\n" + "\n".join(doc_names))

    # Add actual document text
    for doc_path in documents:
        doc_text = load_document_text(doc_path, max_chars_per_page * page_count, data_root, output_root)
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
        max_text_chars=config.get("max_text_chars", 100_000),
    )

    # Call the LLM - dedup identical excerpts within this run (e.g.
    # boilerplate filings shared across cases) via a content-hash cache, per
    # provider (different providers must not share a cache entry), same
    # pattern as Stage 5's citation cache in the event extraction pipeline.
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    cache_key = (provider, prompt_hash)
    try:
        if prompt_cache is not None and cache_key in prompt_cache:
            labels = prompt_cache[cache_key]
        else:
            result = call_provider(provider, prompt, CLASSIFICATION_RESPONSE_SCHEMA, config)

            # Filter by confidence threshold
            min_confidence = config.get("min_confidence", 0.6)
            labels = [
                label for label in result.get("labels", [])
                if label.get("confidence", 0.0) >= min_confidence
            ]

            if prompt_cache is not None:
                prompt_cache[cache_key] = labels

        return {
            "case_id": case_id,
            "caption": case_metadata.get("caption"),
            "court": case_metadata.get("court"),
            "case_type": case_metadata.get("case_type"),
            "case_received_date": case_metadata.get("case_received_date"),
            "case_status": case_metadata.get("case_status"),
            "text_excerpt": text_excerpt[:1000],  # Store snippet for review
            "documents_used": [doc.name for doc in documents],
            "document_names": doc_names,
            "provider_results": {
                provider: {
                    "labels": labels,
                    "model": provider_model_name(provider, config),
                    "classified_at": datetime.now().isoformat(),
                }
            },
        }

    except Exception as e:
        logger.error(f"Failed to classify {case_id} with {provider}: {e}")
        return None


def find_cases(
    data_root: Path,
    case_sources: list[str],
    case_ids: list[str] | None = None,
) -> list[Path]:
    """Find case directories to process.

    Cases live under source-specific subdirectories, e.g.
    ``data_root/<source>/<case_id>/`` (one source per distinct export batch -
    see ``case_sources`` in config). Only sources listed in ``case_sources``
    are searched, so switching the training-data pool is a config change,
    not a code change.
    """
    source_dirs = []
    for source in case_sources:
        source_dir = data_root / source
        if source_dir.exists() and source_dir.is_dir():
            source_dirs.append(source_dir)
        else:
            logger.warning(f"Configured case source not found: {source_dir}")

    if case_ids:
        # Process specific cases - search for each within the configured sources
        case_dirs = []
        for case_id in case_ids:
            matches = [
                source_dir / case_id
                for source_dir in source_dirs
                if (source_dir / case_id).is_dir()
            ]
            if matches:
                case_dirs.append(matches[0])
                if len(matches) > 1:
                    logger.warning(
                        f"Case {case_id} found in multiple sources "
                        f"{[m.parent.name for m in matches]}; using {matches[0]}"
                    )
            else:
                logger.warning(
                    f"Case directory not found for {case_id} in sources {case_sources}"
                )
        return case_dirs
    else:
        # Process all cases across the configured sources
        return [
            case_dir
            for source_dir in source_dirs
            for case_dir in sorted(source_dir.iterdir())
            if case_dir.is_dir()
        ]


def main():
    parser = argparse.ArgumentParser(
        description="Zero-shot lawsuit classifier using multiple LLM providers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "cases",
        nargs="*",
        help="Case IDs to classify (default: all cases)",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=PROVIDERS,
        help="Which provider(s) to query this run (default: config's "
        "llm_providers). Cloud providers cost money per call and are only "
        "queried when named here or in config.",
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
        help="Re-classify cases that already have results for the "
        "requested provider(s) (other providers' existing results are kept)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max number of cases to process this run (default: 50). "
        "Use 0 for no limit.",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load config
    config = load_config(args.config)
    providers = args.providers or config.get("llm_providers", ["ollama"])
    for provider in providers:
        check_provider_credentials(provider)
    logger.info(f"Providers for this run: {providers}")

    # Determine output path
    output_path = args.output or Path(config.get(
        "training_data_path",
        "data/classification/training_data.json"
    ))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing results (always - even with --force, other providers'
    # results for a case must be kept, only the requested provider(s) are
    # re-run for that case)
    existing_results: dict[str, dict[str, Any]] = {}
    if output_path.exists():
        with open(output_path) as f:
            data = json.load(f)
            existing_results = {r["case_id"]: r for r in data.get("results", [])}
        logger.info(f"Loaded {len(existing_results)} existing case(s) from {output_path}")

    # Find cases
    with open(args.config, "rb") as f:
        full_config = tomllib.load(f)

    data_root = Path(config.get("data_root", "data/cases"))
    if not data_root.exists():
        # Try paths.data_root from config
        data_root = Path(full_config.get("paths", {}).get("data_root", "data/cases"))
    output_root = Path(full_config.get("paths", {}).get("output_root", "data/extraction"))

    case_sources = config.get("case_sources", ["ny_sample"])
    case_dirs = find_cases(data_root, case_sources, args.cases or None)
    logger.info(f"Found {len(case_dirs)} cases (sources: {case_sources})")
    if args.limit > 0 and len(case_dirs) > args.limit:
        case_dirs = case_dirs[:args.limit]
        logger.info(f"Limiting to first {args.limit} case(s) (--limit)")

    # Classify cases - for each case, only call providers that don't already
    # have a result for it (unless --force), merging into whatever
    # provider_results already exist so earlier providers' work is preserved.
    prompt_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    cache_hits = 0
    calls_made = 0
    results = []
    for case_dir in tqdm(case_dirs, desc="Classifying cases"):
        case_id = case_dir.name
        existing_case = existing_results.get(case_id)
        merged = dict(existing_case) if existing_case else {"case_id": case_id}
        merged.setdefault("provider_results", {})

        for provider in providers:
            if provider in merged["provider_results"] and not args.force:
                continue  # already have this provider's result for this case

            hits_before = len(prompt_cache)
            result = classify_case(case_id, case_dir, config, data_root, output_root, provider, prompt_cache)
            calls_made += 1
            if len(prompt_cache) == hits_before:
                cache_hits += 1
            if result is None:
                continue

            merged["provider_results"][provider] = result["provider_results"][provider]
            for key in (
                "caption", "court", "case_type", "case_received_date", "case_status",
                "text_excerpt", "documents_used", "document_names",
            ):
                if key in result:
                    merged[key] = result[key]

        if merged.get("provider_results"):
            results.append(merged)

    if cache_hits:
        logger.info(f"Prompt cache: {cache_hits}/{calls_made} call(s) reused an identical excerpt's result")

    # Save results
    providers_present = sorted({
        provider
        for result in results
        for provider in result.get("provider_results", {})
    })
    output_data = {
        "metadata": {
            "created_at": datetime.now().isoformat(),
            "providers_run_this_session": providers,
            "providers_present": providers_present,
            "config_file": str(args.config),
            "total_cases": len(results),
            "categories": config.get("categories", []),
        },
        "results": results,
    }

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    logger.info(f"Saved {len(results)} case(s) to {output_path}")

    # Print summary statistics, per provider - a case with results from
    # multiple providers is counted once per provider (reconciling across
    # providers into one label set is scripts/reconcile_classifications.py's
    # job, not this one's).
    print("\n=== Classification Summary ===")
    print(f"Total cases with any results: {len(results)}")
    for provider in providers_present:
        provider_results = [r["provider_results"][provider] for r in results if provider in r["provider_results"]]
        category_counts: dict[str, int] = {}
        for provider_result in provider_results:
            for label in provider_result.get("labels", []):
                category_counts[label["category"]] = category_counts.get(label["category"], 0) + 1

        print(f"\n{provider} ({len(provider_results)} case(s) classified):")
        for category, count in sorted(category_counts.items()):
            pct = count / len(provider_results) * 100 if provider_results else 0.0
            print(f"  {category}: {count} cases ({pct:.1f}%)")

    print(f"\nTraining data saved to: {output_path}")


if __name__ == "__main__":
    main()
