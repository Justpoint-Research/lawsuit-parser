"""Prompt assembly for llm_validation.py's Ollama/NuExtract-backed calls.

Wording lives in config/llm_prompts.toml so it can be edited without
touching this module. This module's job is just filling in each template's
placeholders and deciding which optional *_line templates to include - a
task's context (a case's litigation caption candidates, a document's page-1
text excerpt, ...) is genuinely optional, so a missing piece is left out of
the prompt entirely rather than rendered as an "unknown" placeholder line.

Response schemas and the enums they constrain (VALID_ROLES, PRODUCT_TYPES)
live here too since they're part of what a prompt is asking for, not how
llm_validation.py calls a backend or cleans its response.
"""

import tomllib
from functools import lru_cache
from pathlib import Path

from .models import Actor

VALID_ROLES = {
    "plaintiff", "defendant", "judge", "court_clerk", "counsel",
    "witness", "attorney", "other",
}

PRODUCT_TYPES = {
    "drug", "medical_device", "cosmetic_product", "chemical_substance", "other_product",
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

TITLE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}

SUMMARY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def build_event_response_schema(candidate_actors: list[str], candidate_dates: list[str]) -> dict:
    """Response schema for event_synthesis, built per call rather than as a
    module-level constant: `actors`/`dates` are constrained via JSON-schema
    `enum` to exactly this DateCluster's own candidate_actors/dates, so the
    model can only pick from names entities.json already resolved (or dates
    already found in this passage) - never invent one. An empty
    candidate_actors list (no known actor found nearby) uses maxItems=0
    instead of enum, since an empty `enum` array is invalid JSON Schema.
    """
    actors_items = {"type": "string", "enum": candidate_actors} if candidate_actors else {"type": "string"}
    actors_schema: dict = {"type": "array", "items": actors_items}
    if not candidate_actors:
        actors_schema["maxItems"] = 0

    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event_type": {"type": "string"},
                        "description": {"type": "string"},
                        "outcome": {"type": "string"},
                        "actors": actors_schema,
                        "dates": {"type": "array", "items": {"type": "string", "enum": candidate_dates}},
                        "confidence": {"type": "number"},
                    },
                    "required": ["event_type", "description", "actors", "dates", "confidence"],
                },
            },
        },
        "required": ["events"],
    }


@lru_cache(maxsize=1)
def _templates() -> dict:
    path = Path(__file__).resolve().parent.parent.parent / "config" / "llm_prompts.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)


def _render(sections: list[str]) -> str:
    return "\n\n".join(s.strip() for s in sections if s and s.strip())


def build_actor_prompt(actors: list[Actor], caption: str | None, court: str | None) -> str:
    candidates = "\n".join(
        f"{i}. name={actor.canonical_name!r} current_role={actor.role} "
        f"is_named={actor.is_named} source={actor.source} "
        f"seen_in={len(actor.doc_ids)} document(s)"
        for i, actor in enumerate(actors)
    )
    return _templates()["actor_validation"]["template"].format(
        caption=caption or "unknown",
        court=court or "unknown",
        valid_roles=", ".join(sorted(VALID_ROLES)),
        candidates=candidates,
    ).strip()


def build_product_prompt(
    caption: str | None,
    court: str | None,
    case_type: str | None,
    litigation_caption_candidates: list[str],
    defendant_names: list[str],
    text_sample: str | None,
) -> str:
    t = _templates()["product_identification"]
    sections = [
        t["intro"].format(
            caption=caption or "unknown", court=court or "unknown", case_type=case_type or "unknown"
        ),
    ]
    if litigation_caption_candidates:
        sections.append(t["litigation_caption_line"].format(candidates="; ".join(litigation_caption_candidates)))
    if defendant_names:
        sections.append(t["defendants_line"].format(names=", ".join(defendant_names)))
    if text_sample:
        sections.append(t["text_sample_line"].format(text_sample=text_sample))
    sections.append(t["closing"])
    return _render(sections)


def build_title_prompt(text_excerpt: str, candidates: list[str], docling_title: str | None) -> str:
    t = _templates()["title_identification"]
    sections = [t["intro"]]
    if docling_title:
        sections.append(t["docling_title_line"].format(docling_title=repr(docling_title)))
    if candidates:
        sections.append(t["candidates_line"].format(candidates=", ".join(repr(c) for c in candidates)))
    if text_excerpt:
        sections.append(t["text_excerpt_line"].format(text_excerpt=text_excerpt))
    sections.append(t["closing"])
    return _render(sections)


def build_summary_prompt(text_excerpt: str, document_title: str | None) -> str:
    t = _templates()["summary"]
    sections = [t["intro"]]
    if document_title:
        sections.append(t["title_line"].format(document_title=repr(document_title)))
    if text_excerpt:
        sections.append(t["text_excerpt_line"].format(text_excerpt=text_excerpt))
    sections.append(t["closing"])
    return _render(sections)


def build_event_prompt(
    citation: str,
    dates: list[str],
    candidate_actors: list[str],
    document_title: str | None,
    document_summary: str | None,
) -> str:
    t = _templates()["event_synthesis"]
    sections = [t["intro"]]
    if document_title:
        sections.append(t["title_line"].format(document_title=repr(document_title)))
    if document_summary:
        sections.append(t["summary_line"].format(document_summary=document_summary))
    sections.append(t["dates_line"].format(dates=", ".join(repr(d) for d in dates)))
    if candidate_actors:
        sections.append(t["actors_line"].format(candidate_actors=", ".join(repr(a) for a in candidate_actors)))
    else:
        sections.append(t["no_actors_line"])
    sections.append(t["passage_line"].format(citation=citation))
    sections.append(t["closing"])
    return _render(sections)
