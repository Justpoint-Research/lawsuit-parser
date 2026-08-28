"""Configuration management for event extraction pipeline."""

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PathsConfig(BaseModel):
    """Path configuration."""

    data_root: str = Field(default="data/cases", description="Source case data: documents, confirmations, docling")
    output_root: str = Field(
        default="data/extraction",
        description="Where pipeline-generated artifacts (events/, stages/) are written, kept "
                     "separate from data_root so iteration output can be wiped without touching source data",
    )
    events_dir: str = Field(default="events")


class Stage1Config(BaseModel):
    """Stage 1 (Metadata Extraction) configuration."""

    extract_from_database: bool = Field(default=True)
    extract_from_pdfs: bool = Field(default=True)
    extract_from_docling: bool = Field(default=True)
    extract_from_confirmations: bool = Field(
        default=True,
        description="Extract filer/judge/timestamp metadata from matching confirmations/ notices",
    )
    comprehensive_llm_extraction: bool = Field(
        default=True,
        description="Run a comprehensive LLM extraction pass to extract ALL parties, counsel, "
                     "judges, products, and other entities with full contact information. One "
                     "LLM call per document, each attributed to the document it came from.",
    )
    llm_extraction_doc_count: int = Field(
        default=-1,
        description="How many documents to run comprehensive_llm_extraction over. -1 = all "
                     "documents in the case; a positive N caps it to the first N documents.",
    )
    llm_extraction_page_count: int = Field(
        default=15,
        gt=0,
        description="Extract from the first N pages (~3000 chars/page) of each document in "
                     "comprehensive_llm_extraction, to bound cost against outlier documents "
                     "running to hundreds of pages.",
    )
    validate_actors_with_llm: bool = Field(
        default=True,
        description="Sanity-check the regex-discovered actor roster with an LLM before "
                     "writing actors.json - corrects roles, drops obvious junk, and phrases "
                     "GLiNER labels. Falls back to the unvalidated roster if the backend "
                     "server isn't reachable.",
    )
    llm_backend: str = Field(
        default="ollama",
        description="'ollama' (local Ollama server) or 'nuextract' (this repo's existing "
                     "vLLM-served NuExtract client). Switching backend also requires setting "
                     "llm_model/llm_base_url to match - see config/event_extraction.toml.",
    )
    llm_model: str = Field(description="Model tag/name for the selected llm_backend")
    llm_base_url: str = Field(description="Server URL for the selected llm_backend")
    extract_products: bool = Field(
        default=True,
        description="Identify the medical substance/drug/medical device/cosmetic product the "
                     "plaintiff accuses of causing harm and the defendant(s) it's attributed to, "
                     "via the same llm_backend used for actor validation. Written to products.json "
                     "and merged into gliner_config.json's dynamic labels alongside actors.",
    )
    date_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\d{1,2}/\d{1,2}/\d{4}",
            r"\d{4}-\d{2}-\d{2}",
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
        ]
    )


class Stage2Config(BaseModel):
    """Stage 2 (GLiNER Entity Detection) configuration."""

    model: str = Field(default="urchade/gliner_multi-v2.1")
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    batch_size: int = Field(default=8, gt=0)
    use_gpu: bool = Field(default=True)
    context_sentences_before: int = Field(
        default=2, ge=0, description="Minimum full sentences of context before each detected entity"
    )
    context_sentences_after: int = Field(
        default=2, ge=0, description="Minimum full sentences of context after each detected entity"
    )
    enable_gazetteer: bool = Field(
        default=True,
        description="After GLiNER, regex-search each named actor's canonical name/aliases and add "
                     "any exact-match spans GLiNER's threshold missed (deterministic recall backstop, "
                     "not a replacement - GLiNER still finds generic/unnamed labels grep can't touch)",
    )
    gazetteer_min_term_length: int = Field(
        default=3, ge=1, description="Skip actor names/aliases shorter than this many characters"
    )
    static_labels: list[str] = Field(
        default_factory=lambda: [
            "temporal expression",
            "legal action or event",
            "court",
            "geographic location",
            "monetary amount",
            "document reference",
            "medical substance, drug, medical device, or cosmetic product",
        ]
    )


class Stage3Config(BaseModel):
    """Stage 3 (Document Summary) configuration."""

    summarize_documents: bool = Field(
        default=True,
        description="Generate a 1-3 sentence summary of each document's core purpose via an "
                     "LLM. Falls back to no summary for a document if the backend server isn't "
                     "reachable, so this is safe to leave on.",
    )
    llm_backend: str = Field(
        default="ollama",
        description="'ollama' (local Ollama server) or 'nuextract' (this repo's existing "
                     "vLLM-served NuExtract client). Switching backend also requires setting "
                     "llm_model/llm_base_url to match - see config/event_extraction.toml.",
    )
    llm_model: str = Field(description="Model tag/name for the selected llm_backend")
    llm_base_url: str = Field(description="Server URL for the selected llm_backend")
    max_chars: int = Field(
        default=8000,
        gt=0,
        description="Maximum characters of document text sent to the LLM - larger than Stage "
                     "1's title excerpt since a document's core purpose often isn't stated on "
                     "page 1 alone (e.g. a complaint's factual background, a motion's argument).",
    )


class Stage4Config(BaseModel):
    """Stage 4 (Date Clustering) configuration."""


class Stage5Config(BaseModel):
    """Stage 5 (Event Synthesis) configuration."""

    synthesize_events: bool = Field(
        default=True,
        description="Synthesize an Event (what happened, outcome, who was involved) for each "
                    "Stage 4 date cluster via an LLM. Falls back to no events for a cluster if "
                    "the backend server isn't reachable, so this is safe to leave on.",
    )
    llm_backend: str = Field(
        default="ollama",
        description="'ollama' (local Ollama server) or 'nuextract' (this repo's existing "
                     "vLLM-served NuExtract client). Switching backend also requires setting "
                     "llm_model/llm_base_url to match - see config/event_extraction.toml.",
    )
    llm_model: str = Field(description="Model tag/name for the selected llm_backend")
    llm_base_url: str = Field(description="Server URL for the selected llm_backend")
    use_llm: bool = Field(
        default=False,
        description="False (default): build each Event deterministically, no LLM call - "
                     "description is a direct quote (date's sentence +/-1 sentence of context), "
                     "event_type/outcome unset, actors is the cluster's full candidate_actors. "
                     "True: LLM-synthesize event_type/description/outcome and curate actors - "
                     "much slower, see config/event_extraction.toml.",
    )
    batch_size: int = Field(
        default=1,
        description="Synthesize this many distinct date clusters per LLM call instead of one "
                     "call each (Ollama backend only, ignored for 'nuextract'). Only used when "
                     "use_llm is true. See config/event_extraction.toml for the full tradeoff "
                     "explanation.",
    )


class EventExtractionConfig(BaseModel):
    """Complete configuration for event extraction pipeline."""

    paths: PathsConfig = Field(default_factory=PathsConfig)
    stage_1: Stage1Config = Field(default_factory=Stage1Config)
    stage_2: Stage2Config = Field(default_factory=Stage2Config)
    stage_3: Stage3Config = Field(default_factory=Stage3Config)
    stage_4: Stage4Config = Field(default_factory=Stage4Config)
    stage_5: Stage5Config = Field(default_factory=Stage5Config)


def load_config(config_path: Path | None = None) -> EventExtractionConfig:
    """Load event extraction configuration from TOML file.

    Args:
        config_path: Path to config file. If None, uses default location
                     at config/event_extraction.toml

    Returns:
        Loaded and validated configuration

    Raises:
        FileNotFoundError: If config file doesn't exist
    """
    if config_path is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        config_path = repo_root / "config" / "event_extraction.toml"

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found at {config_path}")

    with open(config_path, "rb") as f:
        config_data = tomllib.load(f)

    return EventExtractionConfig.model_validate(config_data)


def get_config_dict(config: EventExtractionConfig, stage: str) -> dict[str, Any]:
    """Get configuration dictionary for a specific stage.

    Args:
        config: Main configuration object
        stage: Stage name (e.g., 'stage_1', 'stage_2')

    Returns:
        Stage-specific configuration as dictionary
    """
    stage_config = getattr(config, stage, None)
    if stage_config is None:
        raise ValueError(f"Unknown stage: {stage}")

    return stage_config.model_dump()
