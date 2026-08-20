"""Configuration management for event extraction pipeline."""

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class PathsConfig(BaseModel):
    """Path configuration."""

    data_root: str = Field(default="data/cases")
    events_dir: str = Field(default="events")


class Stage1Config(BaseModel):
    """Stage 1 (Metadata Extraction) configuration."""

    extract_from_database: bool = Field(default=True)
    extract_from_pdfs: bool = Field(default=True)
    extract_from_docling: bool = Field(default=True)
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
    static_labels: list[str] = Field(
        default_factory=lambda: [
            "temporal expression",
            "legal action or event",
            "court",
            "geographic location",
            "monetary amount",
            "document reference",
        ]
    )


class EventExtractionConfig(BaseModel):
    """Complete configuration for event extraction pipeline."""

    paths: PathsConfig = Field(default_factory=PathsConfig)
    stage_1: Stage1Config = Field(default_factory=Stage1Config)
    stage_2: Stage2Config = Field(default_factory=Stage2Config)


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
        # Return default configuration if file doesn't exist
        print(f"Config file not found at {config_path}, using defaults")
        return EventExtractionConfig()

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
