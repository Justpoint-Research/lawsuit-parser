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

Two backends are available for all of these, selected via each stage's
own llm_backend config (Stage1Config.llm_backend, Stage3Config.llm_backend):
- "ollama" (default): a locally running Ollama model, e.g. gemma4:e4b.
  Verified working against this repo's local Ollama server.
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

import requests

from .models import Actor

VALID_ROLES = {
    "plaintiff", "defendant", "judge", "court_clerk", "counsel",
    "witness", "attorney", "other",
}

ACTOR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "actors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string"},
                    "role": {"type": "string", "enum": sorted(VALID_ROLES)},
                    "gliner_label": {"type": "string"},
                    "keep": {"type": "boolean"},
                },
                "required": ["canonical_name", "role", "gliner_label", "keep"],
            },
        },
    },
    "required": ["actors"],
}


def _build_prompt(actors: list[Actor], caption: str | None, court: str | None) -> str:
    lines = [
        "You are validating a roster of people/roles extracted by regex from a "
        "lawsuit's court filings (case captions and e-filing confirmation notices).",
        f"Case caption: {caption or 'unknown'}",
        f"Court: {court or 'unknown'}",
        "",
        "For each candidate below, confirm or correct its role from this set: "
        f"{', '.join(sorted(VALID_ROLES))}.",
        "Set keep=false if the entry is clearly not a real person/organization name: "
        "a stray street address, page furniture, OCR garbage, or a single generic "
        "word/phrase that reads like a document title rather than a party "
        "(e.g. 'STIPULATION', 'ORDER', 'NOTICE', 'AFFIDAVIT') - court filings "
        "often place these right next to the party list.",
        "Propose a short GLiNER detection label: for a named actor use "
        "'<role> (<name>)' (e.g. 'plaintiff (Jane Doe)'); for a generic/unnamed "
        "placeholder use the bare role (e.g. 'witness').",
        "Return exactly one output entry per candidate, in the same order given.",
        "",
        "Candidates:",
    ]
    for i, actor in enumerate(actors):
        lines.append(
            f"{i}. name={actor.canonical_name!r} current_role={actor.role} "
            f"is_named={actor.is_named} source={actor.source} "
            f"seen_in={len(actor.doc_ids)} document(s)"
        )
    return "\n".join(lines)


def validate_actors_with_llm(
    actors: list[Actor],
    caption: str | None = None,
    court: str | None = None,
    model: str = "gemma4:e4b",
    base_url: str = "http://localhost:11434",
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

    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": _build_prompt(actors, caption, court)}],
                "format": ACTOR_RESPONSE_SCHEMA,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        results = json.loads(content).get("actors", [])
    except Exception as e:
        print(f"  Warning: LLM actor validation unavailable ({e}), keeping unvalidated roster")
        return actors

    if len(results) != len(actors):
        print(
            f"  Warning: LLM returned {len(results)} actors for {len(actors)} "
            f"candidates, keeping unvalidated roster"
        )
        return actors

    validated = []
    for actor, result in zip(actors, results):
        if not result.get("keep", True):
            continue
        role = result.get("role")
        if role not in VALID_ROLES:
            role = actor.role
        validated.append(actor.model_copy(update={
            "role": role,
            "gliner_label": result.get("gliner_label") or actor.gliner_label,
        }))

    return validated


def validate_actors_with_nuextract(
    actors: list[Actor],
    caption: str | None = None,
    court: str | None = None,
    model: str = "numind/NuExtract3",
    base_url: str = "http://localhost:8000/v1",
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

    Returns:
        A corrected roster, or the input roster unchanged if the vLLM
        server is unreachable or its response can't be used.
    """
    if not actors:
        return actors

    try:
        from .nuextract_client import ExtractionError, NuExtractClient
    except ImportError as e:
        print(f"  Warning: NuExtract client unavailable ({e}), keeping unvalidated roster")
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

    try:
        client = NuExtractClient(base_url=base_url, model=model)
        result = client.extract(_build_prompt(actors, caption, court), template)
        results = result.get("actors", [])
    except ExtractionError as e:
        print(f"  Warning: NuExtract actor validation failed ({e}), keeping unvalidated roster")
        return actors
    except Exception as e:
        print(f"  Warning: NuExtract actor validation unavailable ({e}), keeping unvalidated roster")
        return actors

    if not isinstance(results, list) or len(results) != len(actors):
        print(
            f"  Warning: NuExtract returned {len(results) if isinstance(results, list) else 0} "
            f"actors for {len(actors)} candidates, keeping unvalidated roster"
        )
        return actors

    validated = []
    for actor, result in zip(actors, results):
        keep = str(result.get("keep", "true")).strip().lower()
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


# ============================================================================
# Accused-product identification
# ============================================================================

PRODUCT_TYPES = {
    "drug", "medical_device", "cosmetic_product", "chemical_substance", "other_product",
}

PRODUCT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "product_type": {"type": "string", "enum": sorted(PRODUCT_TYPES)},
                    "attributed_to": {"type": "array", "items": {"type": "string"}},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "product_type", "attributed_to"],
            },
        },
    },
    "required": ["products"],
}


def _build_product_prompt(
    caption: str | None,
    court: str | None,
    case_type: str | None,
    litigation_caption_candidates: list[str],
    defendant_names: list[str],
    text_sample: str | None,
) -> str:
    lines = [
        "You are identifying the medical substance, drug, medical device, or cosmetic "
        "product that the plaintiff in this lawsuit accuses of causing harm, and the "
        "defendant(s) it is attributed to (its manufacturer, seller, or distributor).",
        f"Case caption: {caption or 'unknown'}",
        f"Court: {court or 'unknown'}",
        f"Case type: {case_type or 'unknown'}",
    ]
    if litigation_caption_candidates:
        lines.append(
            "Coordinated-litigation captions found in this case's filings name the "
            f"product directly: {'; '.join(litigation_caption_candidates)}"
        )
    if defendant_names:
        lines.append(
            "Known defendants in this case - attribute the product to one or more of "
            f"these by exact name where you can tell: {', '.join(defendant_names)}"
        )
    if text_sample:
        lines.append("\nExcerpt from a filing in this case, for context:\n" + text_sample)
    lines.append(
        "\nReturn one entry per distinct accused product. If the pleading treats "
        "several defendants' branded products as one legally defined category (e.g. "
        "\"the Cosmetic Products\" or \"the PRODUCTS\"), name that category, not each "
        "individual brand. Return an empty products list if no product/substance is "
        "clearly the subject of the plaintiff's harm allegations - do not guess."
    )
    return "\n".join(lines)


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
    caption: str | None = None,
    court: str | None = None,
    case_type: str | None = None,
    litigation_caption_candidates: list[str] | None = None,
    defendant_names: list[str] | None = None,
    text_sample: str | None = None,
    model: str = "gemma4:e4b",
    base_url: str = "http://localhost:11434",
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
    prompt = _build_product_prompt(
        caption, court, case_type, litigation_caption_candidates or [], defendant_names or [], text_sample,
    )

    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": PRODUCT_RESPONSE_SCHEMA,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        raw_products = json.loads(content).get("products", [])
    except Exception as e:
        print(f"  Warning: LLM product identification unavailable ({e})")
        return []

    return _clean_product_results(raw_products)


def identify_products_with_nuextract(
    caption: str | None = None,
    court: str | None = None,
    case_type: str | None = None,
    litigation_caption_candidates: list[str] | None = None,
    defendant_names: list[str] | None = None,
    text_sample: str | None = None,
    model: str = "numind/NuExtract3",
    base_url: str = "http://localhost:8000/v1",
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
    try:
        from .nuextract_client import ExtractionError, NuExtractClient
    except ImportError as e:
        print(f"  Warning: NuExtract client unavailable ({e})")
        return []

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
    prompt = _build_product_prompt(
        caption, court, case_type, litigation_caption_candidates or [], defendant_names or [], text_sample,
    )

    try:
        client = NuExtractClient(base_url=base_url, model=model, timeout_s=int(timeout))
        result = client.extract(prompt, template)
        raw_products = result.get("products", [])
    except ExtractionError as e:
        print(f"  Warning: NuExtract product identification failed ({e})")
        return []
    except Exception as e:
        print(f"  Warning: NuExtract product identification unavailable ({e})")
        return []

    return _clean_product_results(raw_products)


# ============================================================================
# Document title identification
# ============================================================================

TITLE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
    },
    "required": ["title"],
}


def _build_title_prompt(text_excerpt: str, candidates: list[str], docling_title: str | None) -> str:
    lines = [
        "You are identifying the formal name/type of a single legal document "
        "from its first page (e.g. \"Summons\", \"Verified Complaint\", "
        "\"Notice of Motion\", \"Stipulation of Discontinuance\", \"Affidavit "
        "of Service\", \"Memorandum of Law in Support\").",
    ]
    if docling_title:
        lines.append(f"A layout-detection pass found this as a likely title: {docling_title!r}")
    if candidates:
        lines.append(
            "Short, mostly-uppercase lines found on page 1 that may name the "
            f"document (not necessarily the title - could be a party name or "
            f"boilerplate): {', '.join(repr(c) for c in candidates)}"
        )
    if text_excerpt:
        lines.append("\nFirst-page text, for context:\n" + text_excerpt)
    lines.append(
        "\nReturn the single best short title for this document, in normal "
        "title case (not all-caps) even if the source text is all-caps. "
        "Return an empty string if the text doesn't clearly name a specific "
        "document type - do not guess or invent one."
    )
    return "\n".join(lines)


def _clean_title_result(raw_title: str | None) -> str | None:
    title = str(raw_title or "").strip()
    return title or None


def _parse_single_field_response(content: str, field: str) -> str | None:
    """Pull a single string field out of an Ollama chat response's content.

    Despite the JSON-schema `format` constraint, gemma4 sometimes returns
    the value as a bare string (e.g. "Stipulation of Discontinuance")
    instead of the requested {"<field>": "..."} object - salvage that
    rather than discarding a perfectly good answer.
    """
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    return parsed.get(field) if isinstance(parsed, dict) else parsed


def identify_document_title_with_llm(
    text_excerpt: str | None = None,
    candidates: list[str] | None = None,
    docling_title: str | None = None,
    model: str = "gemma4:e4b",
    base_url: str = "http://localhost:11434",
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
    prompt = _build_title_prompt(text_excerpt or "", candidates or [], docling_title)

    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": TITLE_RESPONSE_SCHEMA,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        raw_title = _parse_single_field_response(content, "title")
    except Exception as e:
        print(f"  Warning: LLM title identification unavailable ({e})")
        return None

    return _clean_title_result(raw_title)


def identify_document_title_with_nuextract(
    text_excerpt: str | None = None,
    candidates: list[str] | None = None,
    docling_title: str | None = None,
    model: str = "numind/NuExtract3",
    base_url: str = "http://localhost:8000/v1",
    timeout: float = 60.0,
) -> str | None:
    """Identify a document's formal title/type with NuExtract, served via
    vLLM. Same contract as identify_document_title_with_llm (Ollama-backed) -
    see that function's docstring.

    Returns:
        The identified title, or None if undeterminable or the vLLM server
        is unreachable/its response can't be used.
    """
    try:
        from .nuextract_client import ExtractionError, NuExtractClient
    except ImportError as e:
        print(f"  Warning: NuExtract client unavailable ({e})")
        return None

    template = {"title": ""}
    prompt = _build_title_prompt(text_excerpt or "", candidates or [], docling_title)

    try:
        client = NuExtractClient(base_url=base_url, model=model, timeout_s=int(timeout))
        result = client.extract(prompt, template)
        raw_title = result.get("title")
    except ExtractionError as e:
        print(f"  Warning: NuExtract title identification failed ({e})")
        return None
    except Exception as e:
        print(f"  Warning: NuExtract title identification unavailable ({e})")
        return None

    return _clean_title_result(raw_title)


# ============================================================================
# Document summary (Stage 3)
# ============================================================================

SUMMARY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
    },
    "required": ["summary"],
}


def _build_summary_prompt(text_excerpt: str, document_title: str | None) -> str:
    lines = [
        "You are summarizing a single legal document in 1-3 sentences. "
        "Focus on the document's core purpose: why it was filed or created, "
        "and what it accomplishes (e.g. who is asking a court for what, who "
        "is notifying whom of what, what fact or event it records) - not a "
        "restatement of its contents or boilerplate caption/header text.",
    ]
    if document_title:
        lines.append(f"This document's identified title/type: {document_title!r}")
    if text_excerpt:
        lines.append("\nDocument text, for context:\n" + text_excerpt)
    lines.append(
        "\nReturn a 1-3 sentence summary. Return an empty string if the "
        "text doesn't give you enough to summarize - do not guess or "
        "invent one."
    )
    return "\n".join(lines)


def _clean_summary_result(raw_summary: str | None) -> str | None:
    summary = str(raw_summary or "").strip()
    return summary or None


def summarize_document_with_llm(
    text_excerpt: str | None = None,
    document_title: str | None = None,
    model: str = "gemma4:e4b",
    base_url: str = "http://localhost:11434",
    timeout: float = 60.0,
) -> str | None:
    """Summarize a document's core purpose in 1-3 sentences with a local
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
    prompt = _build_summary_prompt(text_excerpt or "", document_title)

    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": SUMMARY_RESPONSE_SCHEMA,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        raw_summary = _parse_single_field_response(content, "summary")
    except Exception as e:
        print(f"  Warning: LLM document summarization unavailable ({e})")
        return None

    return _clean_summary_result(raw_summary)


def summarize_document_with_nuextract(
    text_excerpt: str | None = None,
    document_title: str | None = None,
    model: str = "numind/NuExtract3",
    base_url: str = "http://localhost:8000/v1",
    timeout: float = 60.0,
) -> str | None:
    """Summarize a document's core purpose in 1-3 sentences with NuExtract,
    served via vLLM. Same contract as summarize_document_with_llm
    (Ollama-backed) - see that function's docstring.

    Returns:
        The summary, or None if undeterminable or the vLLM server is
        unreachable/its response can't be used.
    """
    try:
        from .nuextract_client import ExtractionError, NuExtractClient
    except ImportError as e:
        print(f"  Warning: NuExtract client unavailable ({e})")
        return None

    template = {"summary": ""}
    prompt = _build_summary_prompt(text_excerpt or "", document_title)

    try:
        client = NuExtractClient(base_url=base_url, model=model, timeout_s=int(timeout))
        result = client.extract(prompt, template)
        raw_summary = result.get("summary")
    except ExtractionError as e:
        print(f"  Warning: NuExtract document summarization failed ({e})")
        return None
    except Exception as e:
        print(f"  Warning: NuExtract document summarization unavailable ({e})")
        return None

    return _clean_summary_result(raw_summary)
