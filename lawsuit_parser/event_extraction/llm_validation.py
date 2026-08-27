"""Optional LLM assistance for Stage 1 (actor roster validation, accused-
product identification, document title) and Stage 3 (document summary).

Regex/heuristic extraction (database, caption parsing, confirmation
notices) can mis-assign a role or let a stray fragment (an address line, a
boilerplate phrase) through as an "actor". validate_actors_with_llm/
validate_actors_with_nuextract make one call to an LLM to sanity-check the
roster before it's saved to actors.json and turned into GLiNER labels -
correcting roles, dropping obvious junk, and phrasing the GLiNER label text.

identify_products_with_llm/identify_products_with_nuextract instead
identify the medical substance/drug/medical device/cosmetic product the
plaintiff accuses of causing harm and the defendant(s) it's attributed to -
a reading-comprehension task with no fixed textual format (unlike a
caption block or a citation), so an LLM does the identification directly
rather than being a validation pass over a regex-built candidate list.

identify_document_title_with_llm/identify_document_title_with_nuextract
identify a single document's formal title/type (e.g. "Summons",
"Stipulation of Discontinuance"). Docling rarely tags an actual "title"
element on these filings, and a plain heuristic (see
utils.find_title_candidates) can't reliably tell a title line from a
party-name or boilerplate line by itself - so the LLM makes the final call,
reading the page-1 text with Docling's title (if any) and the heuristic
candidates as hints, not a hard candidate list.

summarize_document_with_llm/summarize_document_with_nuextract produce a
1-3 sentence summary of a document's core purpose (why it exists / what it
accomplishes), given a longer text excerpt and the identified title as a
hint. Runs in Stage 3.

synthesize_events_with_llm/synthesize_events_with_nuextract read one
Stage 4 DateCluster (a document paragraph, its date(s), and the actors/
products entities.json already resolved nearby) and produce Event records:
what happened, the outcome if stated, and who was involved - "who" is
constrained to the cluster's own candidate_actors (via a JSON-schema enum
built per call, see prompts.build_event_response_schema) so the model can
only name someone GLiNER already resolved, never invent one. Runs in
Stage 5.

Prompt wording for all of the above lives in config/llm_prompts.toml,
assembled by .prompts - edit that file to change what's asked, this one to
change how it's asked (backend I/O) or how a response is turned into a
result.

Two backends are available for all of these, selected via each stage's
own llm_backend config (Stage1Config.llm_backend, Stage3Config.llm_backend):
- "ollama" (default): a locally running Ollama model, e.g. qwen3:30b-a3b.
  Verified working against this repo's local Ollama server. The model is
  just a config value (Stage1Config.llm_model / Stage3Config.llm_model, or
  config/event_extraction.toml) - swap it to any other pulled Ollama tag
  without touching this module.
- "nuextract": this module's NuExtractClient (see .nuextract_client) against
  a vLLM-served NuExtract3 endpoint. Not exercised against a live server as
  part of this change - the vLLM endpoint wasn't reachable in this
  environment. Switching to it also means overriding llm_model/llm_base_url
  to the NuExtract values (numind/NuExtract3, http://localhost:8000/v1) -
  see config/event_extraction.toml.

Both fall back to the unvalidated, deterministically-labeled roster on any
failure (server down, model missing, bad/malformed response) rather than
failing the pipeline - this is a refinement pass, not a required dependency.
"""

import json
import logging
from typing import Any, Callable, TypeVar

import requests

from .models import Actor
from .prompts import (
    ACTOR_RESPONSE_SCHEMA,
    PRODUCT_RESPONSE_SCHEMA,
    PRODUCT_TYPES,
    SUMMARY_RESPONSE_SCHEMA,
    TITLE_RESPONSE_SCHEMA,
    VALID_ROLES,
    build_actor_prompt,
    build_event_prompt,
    build_event_response_schema,
    build_product_prompt,
    build_summary_prompt,
    build_title_prompt,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def unload_ollama_model(model: str, base_url: str, timeout: float = 30.0) -> None:
    """Ask the Ollama server to immediately unload `model` from GPU memory.

    Ollama keeps a model resident in VRAM for a keep_alive window (5 minutes
    by default) after its last request, rather than releasing it as soon as
    the caller is done. A later step in the same run that needs that VRAM
    for its own model - e.g. Stage 2/GLiNER, which runs right after this
    module's Stage 1 calls - can then hit a CUDA OOM even though nothing in
    this process is still using it. Sending keep_alive=0 with no prompt is
    Ollama's documented way to unload a model on demand.

    Best-effort and non-fatal: a server that's already unloaded it,
    unreachable, or running a version without this behavior shouldn't break
    the pipeline over a memory optimization. Only meaningful for the
    "ollama" backend - there's no equivalent call for "nuextract" (a
    vLLM-served endpoint), so callers should skip this for that backend.
    """
    try:
        requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=timeout,
        )
    except Exception as e:
        logger.warning(f"  Warning: couldn't unload Ollama model {model!r} ({e})")


def _with_fallback(task: str, fallback: T, call: Callable[[], T]) -> T:
    """Run `call`, logging and returning `fallback` on any failure.

    Every LLM-assisted step in this module is a refinement pass over a
    result that already exists without it (an unvalidated roster, no
    title/summary) - so a network error, an unreachable server, a
    schema-violating response, or a response with the wrong shape (e.g. one
    actor entry per candidate) should degrade to that fallback rather than
    fail the pipeline.
    """
    try:
        return call()
    except Exception as e:
        logger.warning(f"  Warning: {task} unavailable ({e}), using fallback")
        return fallback


def _call_ollama(*, model: str, base_url: str, prompt: str, schema: dict, timeout: float) -> str:
    """POST `prompt` to Ollama's /api/chat with a JSON-schema format
    constraint, returning the raw response content string.

    Returns the raw string rather than a parsed dict: despite the schema
    constraint, some models sometimes return a bare value (e.g.
    "Stipulation of Discontinuance") instead of the requested JSON object -
    parsing is left to the caller so it can choose whether to salvage that
    (see _parse_single_field_response) or treat it as a failure.
    """
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
    return response.json()["message"]["content"]


def _call_nuextract(*, model: str, base_url: str, prompt: str, template: dict, timeout: float) -> dict[str, Any]:
    """Extract structured data via NuExtractClient (see .nuextract_client),
    against a vLLM-served NuExtract3 endpoint."""
    from .nuextract_client import NuExtractClient

    client = NuExtractClient(base_url=base_url, model=model, timeout_s=int(timeout))
    return client.extract(prompt, template)


def _parse_single_field_response(content: str, field: str) -> str | None:
    """Pull a single string field out of an Ollama chat response's content.

    Despite the JSON-schema `format` constraint, some models sometimes
    return the value as a bare string (e.g. "Stipulation of Discontinuance")
    instead of the requested {"<field>": "..."} object - salvage that
    rather than discarding a perfectly good answer.
    """
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    return parsed.get(field) if isinstance(parsed, dict) else parsed


# ============================================================================
# Actor roster validation
# ============================================================================


def _apply_actor_results(actors: list[Actor], results: list[dict]) -> list[Actor]:
    if len(results) != len(actors):
        raise ValueError(f"got {len(results)} actors for {len(actors)} candidates")

    validated = []
    for actor, result in zip(actors, results):
        keep = str(result.get("keep", True)).strip().lower()
        if keep in ("false", "no", "0", ""):
            continue
        role = str(result.get("role") or actor.role).strip().lower()
        if role not in VALID_ROLES:
            role = actor.role
        validated.append(actor.model_copy(update={
            "role": role,
            "gliner_label": result.get("gliner_label") or actor.gliner_label,
        }))
    return validated


def validate_actors_with_llm(
    actors: list[Actor],
    *,
    model: str,
    base_url: str,
    caption: str | None = None,
    court: str | None = None,
    timeout: float = 60.0,
) -> list[Actor]:
    """Validate/refine an actor roster with a local Ollama model.

    Args:
        actors: Regex/heuristic-discovered actors to validate
        caption: Case caption, for context (e.g. "Bonnie Darling v. Loreal USA, Inc.")
        court: Court name, for context
        model: Ollama model tag (must already be pulled)
        base_url: Ollama server URL
        timeout: Request timeout in seconds

    Returns:
        A corrected roster (role fixes applied, junk entries dropped,
        gliner_label filled in from the model's suggestion), or the input
        roster unchanged if the model is unreachable or its response
        can't be used.
    """
    if not actors:
        return actors

    def call() -> list[Actor]:
        prompt = build_actor_prompt(actors, caption, court)
        content = _call_ollama(model=model, base_url=base_url, prompt=prompt, schema=ACTOR_RESPONSE_SCHEMA, timeout=timeout)
        results = json.loads(content).get("actors", [])
        return _apply_actor_results(actors, results)

    return _with_fallback("LLM actor validation", actors, call)


def validate_actors_with_nuextract(
    actors: list[Actor],
    *,
    model: str,
    base_url: str,
    caption: str | None = None,
    court: str | None = None,
    timeout: float = 60.0,
) -> list[Actor]:
    """Validate/refine an actor roster with NuExtract, served via vLLM.

    Same contract as validate_actors_with_llm (Ollama-backed): corrects
    roles, drops junk entries, and fills in gliner_label - but goes
    through NuExtractClient (see .nuextract_client) against a vLLM-served
    NuExtract3 endpoint.

    Args:
        actors: Regex/heuristic-discovered actors to validate
        caption: Case caption, for context
        court: Court name, for context
        model: vLLM-served model identifier (must match a running server)
        base_url: vLLM OpenAI-compatible API base URL
        timeout: Request timeout in seconds

    Returns:
        A corrected roster, or the input roster unchanged if the vLLM
        server is unreachable or its response can't be used.
    """
    if not actors:
        return actors

    # NuExtract templates describe the desired output shape with
    # placeholder values.
    template = {
        "actors": [
            {
                "canonical_name": "",
                "role": "plaintiff|defendant|judge|court_clerk|counsel|witness|attorney|other",
                "gliner_label": "",
                "keep": "true|false",
            }
        ]
    }

    def call() -> list[Actor]:
        prompt = build_actor_prompt(actors, caption, court)
        result = _call_nuextract(model=model, base_url=base_url, prompt=prompt, template=template, timeout=timeout)
        return _apply_actor_results(actors, result.get("actors", []))

    return _with_fallback("NuExtract actor validation", actors, call)


# ============================================================================
# Accused-product identification
# ============================================================================


def _clean_product_results(raw_products: list[dict]) -> list[dict]:
    results = []
    for product in raw_products:
        name = str(product.get("name") or "").strip()
        product_type = str(product.get("product_type") or "").strip().lower()
        if not name or product_type not in PRODUCT_TYPES:
            continue
        results.append({
            "name": name,
            "product_type": product_type,
            "attributed_to": [str(a).strip() for a in (product.get("attributed_to") or []) if str(a).strip()],
            "aliases": [str(a).strip() for a in (product.get("aliases") or []) if str(a).strip()],
        })
    return results


def identify_products_with_llm(
    *,
    model: str,
    base_url: str,
    caption: str | None = None,
    court: str | None = None,
    case_type: str | None = None,
    litigation_caption_candidates: list[str] | None = None,
    defendant_names: list[str] | None = None,
    text_sample: str | None = None,
    timeout: float = 90.0,
) -> list[dict]:
    """Identify the accused product(s) in a product-liability case with a
    local Ollama model.

    Unlike party/role identification, there's no fixed textual format to
    regex for an arbitrary product name - this is a reading-comprehension
    task, so the LLM identifies it directly from case context (caption,
    court, case type, any "In Re ... Litigation" caption already found by
    utils.find_litigation_captions, and an excerpt of a filing) rather than
    validating a pre-built candidate list.

    Args:
        caption: Case caption, for context
        court: Court name, for context
        case_type: Case type/nature of suit (e.g. "Torts - Product Liability")
        litigation_caption_candidates: Product names already found via
            utils.find_litigation_captions, if any
        defendant_names: Known defendant names, so the product can be
            attributed to one by exact name
        text_sample: Excerpt of a filing likely to describe the product/harm
        model: Ollama model tag (must already be pulled)
        base_url: Ollama server URL
        timeout: Request timeout in seconds

    Returns:
        List of dicts with "name", "product_type", "attributed_to",
        "aliases" - empty if no product was identified or the model is
        unreachable/its response can't be used.
    """
    def call() -> list[dict]:
        prompt = build_product_prompt(
            caption, court, case_type, litigation_caption_candidates or [], defendant_names or [], text_sample,
        )
        content = _call_ollama(
            model=model, base_url=base_url, prompt=prompt, schema=PRODUCT_RESPONSE_SCHEMA, timeout=timeout
        )
        raw_products = json.loads(content).get("products", [])
        return _clean_product_results(raw_products)

    return _with_fallback("LLM product identification", [], call)


def identify_products_with_nuextract(
    *,
    model: str,
    base_url: str,
    caption: str | None = None,
    court: str | None = None,
    case_type: str | None = None,
    litigation_caption_candidates: list[str] | None = None,
    defendant_names: list[str] | None = None,
    text_sample: str | None = None,
    timeout: float = 90.0,
) -> list[dict]:
    """Identify the accused product(s) with NuExtract, served via vLLM.

    Same contract as identify_products_with_llm (Ollama-backed) - see that
    function's docstring. Goes through NuExtractClient (see
    .nuextract_client).

    Returns:
        List of dicts with "name", "product_type", "attributed_to",
        "aliases" - empty if no product was identified or the vLLM server
        is unreachable/its response can't be used.
    """
    template = {
        "products": [
            {
                "name": "",
                "product_type": "drug|medical_device|cosmetic_product|chemical_substance|other_product",
                "attributed_to": [""],
                "aliases": [""],
            }
        ]
    }

    def call() -> list[dict]:
        prompt = build_product_prompt(
            caption, court, case_type, litigation_caption_candidates or [], defendant_names or [], text_sample,
        )
        result = _call_nuextract(model=model, base_url=base_url, prompt=prompt, template=template, timeout=timeout)
        return _clean_product_results(result.get("products", []))

    return _with_fallback("NuExtract product identification", [], call)


# ============================================================================
# Document title identification
# ============================================================================


def _clean_title_result(raw_title: str | None) -> str | None:
    title = str(raw_title or "").strip()
    return title or None


def identify_document_title_with_llm(
    *,
    model: str,
    base_url: str,
    text_excerpt: str | None = None,
    candidates: list[str] | None = None,
    docling_title: str | None = None,
    timeout: float = 60.0,
) -> str | None:
    """Identify a document's formal title/type with a local Ollama model.

    Combines heuristic candidate lines (see utils.find_title_candidates)
    and Docling's own layout-detected title (if any) with an actual read of
    the page text - the LLM makes the final call rather than picking
    blindly from the candidate list, since neither signal is reliable
    enough alone (Docling rarely tags a title element on these filings; the
    heuristic can't distinguish a title line from a party-name or
    boilerplate line by itself).

    Args:
        text_excerpt: First-page text, for context
        candidates: Heuristic candidate title lines (see utils.find_title_candidates)
        docling_title: Docling's own layout-detected title, if any
        model: Ollama model tag (must already be pulled)
        base_url: Ollama server URL
        timeout: Request timeout in seconds

    Returns:
        The identified title, or None if undeterminable or the model is
        unreachable/its response can't be used.
    """
    def call() -> str | None:
        prompt = build_title_prompt(text_excerpt or "", candidates or [], docling_title)
        content = _call_ollama(
            model=model, base_url=base_url, prompt=prompt, schema=TITLE_RESPONSE_SCHEMA, timeout=timeout
        )
        return _clean_title_result(_parse_single_field_response(content, "title"))

    return _with_fallback("LLM title identification", None, call)


def identify_document_title_with_nuextract(
    *,
    model: str,
    base_url: str,
    text_excerpt: str | None = None,
    candidates: list[str] | None = None,
    docling_title: str | None = None,
    timeout: float = 60.0,
) -> str | None:
    """Identify a document's formal title/type with NuExtract, served via
    vLLM. Same contract as identify_document_title_with_llm (Ollama-backed) -
    see that function's docstring.

    Returns:
        The identified title, or None if undeterminable or the vLLM server
        is unreachable/its response can't be used.
    """
    def call() -> str | None:
        prompt = build_title_prompt(text_excerpt or "", candidates or [], docling_title)
        result = _call_nuextract(model=model, base_url=base_url, prompt=prompt, template={"title": ""}, timeout=timeout)
        return _clean_title_result(result.get("title"))

    return _with_fallback("NuExtract title identification", None, call)


# ============================================================================
# Document summary (Stage 3)
# ============================================================================


def _clean_summary_result(raw_summary: str | None) -> str | None:
    summary = str(raw_summary or "").strip()
    return summary or None


def summarize_document_with_llm(
    *,
    model: str,
    base_url: str,
    text_excerpt: str | None = None,
    document_title: str | None = None,
    timeout: float = 60.0,
) -> str | None:
    """Summarize a document's core purpose in 5-15 sentences with a local
    Ollama model.

    Args:
        text_excerpt: Document text, for context (see Stage3Config.max_chars
            for how much of the document this typically covers)
        document_title: The document's identified title/type (see
            identify_document_title_with_llm), used as a hint
        model: Ollama model tag (must already be pulled)
        base_url: Ollama server URL
        timeout: Request timeout in seconds

    Returns:
        The summary, or None if undeterminable or the model is
        unreachable/its response can't be used.
    """
    def call() -> str | None:
        prompt = build_summary_prompt(text_excerpt or "", document_title)
        content = _call_ollama(
            model=model, base_url=base_url, prompt=prompt, schema=SUMMARY_RESPONSE_SCHEMA, timeout=timeout
        )
        return _clean_summary_result(_parse_single_field_response(content, "summary"))

    return _with_fallback("LLM document summarization", None, call)


def summarize_document_with_nuextract(
    *,
    model: str,
    base_url: str,
    text_excerpt: str | None = None,
    document_title: str | None = None,
    timeout: float = 60.0,
) -> str | None:
    """Summarize a document's core purpose in 1-3 sentences with NuExtract,
    served via vLLM. Same contract as summarize_document_with_llm
    (Ollama-backed) - see that function's docstring.

    Returns:
        The summary, or None if undeterminable or the vLLM server is
        unreachable/its response can't be used.
    """
    def call() -> str | None:
        prompt = build_summary_prompt(text_excerpt or "", document_title)
        result = _call_nuextract(
            model=model, base_url=base_url, prompt=prompt, template={"summary": ""}, timeout=timeout
        )
        return _clean_summary_result(result.get("summary"))

    return _with_fallback("NuExtract document summarization", None, call)


# ============================================================================
# Event synthesis (Stage 5)
# ============================================================================


def _clean_event_results(raw_events: list[dict], candidate_actors: list[str], candidate_dates: list[str]) -> list[dict]:
    """Validate/clean the model's raw `events` list: drop entries missing
    an event_type/description, drop an entry whose `dates` don't match any
    of this cluster's own date texts (nothing to anchor it to), and filter
    `actors` down to entries actually on the candidate list - a safety net
    behind the JSON-schema `enum` constraint (see build_event_response_schema),
    since the "nuextract" backend has no equivalent constraint to enforce
    grounding at the format level."""
    valid_actors = set(candidate_actors)
    valid_dates = set(candidate_dates)

    results = []
    for event in raw_events:
        event_type = str(event.get("event_type") or "").strip()
        description = str(event.get("description") or "").strip()
        if not event_type or not description:
            continue

        dates = [d for d in (str(d).strip() for d in (event.get("dates") or [])) if d in valid_dates]
        if not dates:
            continue

        outcome = str(event.get("outcome") or "").strip() or None
        actors = [a for a in (str(a).strip() for a in (event.get("actors") or [])) if a in valid_actors]

        try:
            confidence = float(event.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        results.append({
            "event_type": event_type,
            "description": description,
            "outcome": outcome,
            "actors": actors,
            "dates": dates,
            "confidence": confidence,
        })
    return results


def synthesize_events_with_llm(
    *,
    model: str,
    base_url: str,
    citation: str,
    dates: list[str],
    candidate_actors: list[str],
    document_title: str | None = None,
    document_summary: str | None = None,
    timeout: float = 90.0,
) -> list[dict]:
    """Synthesize event(s) from one Stage 4 DateCluster with a local Ollama
    model: what happened on the date(s) found in this paragraph, the
    outcome if stated, and who was involved - grounded to entities.json by
    constraining `actors` to `candidate_actors` via a JSON-schema `enum`
    built per call (see build_event_response_schema), so the model can only
    name someone entities.json already resolved, never invent one.

    Args:
        citation: Paragraph text, with each date substring marked (see
            DateCluster.citation)
        dates: Raw date text(s) found in this paragraph (DateEntry.text)
        candidate_actors: Canonical linked_actor names of entities.json
            entries found in the same paragraph
        document_title: The document's identified title/type, for context
        document_summary: The document's Stage 3 summary, for context
        model: Ollama model tag (must already be pulled)
        base_url: Ollama server URL
        timeout: Request timeout in seconds

    Returns:
        List of dicts with "event_type", "description", "outcome",
        "actors", "dates", "confidence" - empty if no event was identified
        or the model is unreachable/its response can't be used.
    """
    if not dates:
        return []

    def call() -> list[dict]:
        prompt = build_event_prompt(citation, dates, candidate_actors, document_title, document_summary)
        schema = build_event_response_schema(candidate_actors, dates)
        content = _call_ollama(model=model, base_url=base_url, prompt=prompt, schema=schema, timeout=timeout)
        raw_events = json.loads(content).get("events", [])
        return _clean_event_results(raw_events, candidate_actors, dates)

    return _with_fallback("LLM event synthesis", [], call)


def synthesize_events_with_nuextract(
    *,
    model: str,
    base_url: str,
    citation: str,
    dates: list[str],
    candidate_actors: list[str],
    document_title: str | None = None,
    document_summary: str | None = None,
    timeout: float = 90.0,
) -> list[dict]:
    """Synthesize event(s) from one Stage 4 DateCluster with NuExtract,
    served via vLLM. Same contract as synthesize_events_with_llm
    (Ollama-backed) - see that function's docstring. NuExtract has no
    format-level enum constraint, so grounding relies entirely on the
    prompt plus _clean_event_results' post-hoc filtering.

    Returns:
        List of event dicts, empty if no event was identified or the vLLM
        server is unreachable/its response can't be used.
    """
    if not dates:
        return []

    template = {
        "events": [
            {
                "event_type": "",
                "description": "",
                "outcome": "",
                "actors": [""],
                "dates": [""],
                "confidence": "0.0-1.0",
            }
        ]
    }

    def call() -> list[dict]:
        prompt = build_event_prompt(citation, dates, candidate_actors, document_title, document_summary)
        result = _call_nuextract(model=model, base_url=base_url, prompt=prompt, template=template, timeout=timeout)
        return _clean_event_results(result.get("events", []), candidate_actors, dates)

    return _with_fallback("NuExtract event synthesis", [], call)
