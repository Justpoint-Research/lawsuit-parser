"""Pipeline stages for event extraction."""

from .stage_1_metadata import Stage1Metadata
from .stage_2_gliner import Stage2GLiNER
from .stage_3_summary import Stage3Summary
from .stage_4_dates import Stage4Dates
from .stage_5_events import Stage5Events
from .stage_6_relations import Stage6Relations

# Registry of all available stages
STAGES = [
    Stage1Metadata,
    Stage2GLiNER,
    Stage3Summary,
    Stage4Dates,
    Stage5Events,
    Stage6Relations,
]

__all__ = [
    "STAGES",
    "Stage1Metadata",
    "Stage2GLiNER",
    "Stage3Summary",
    "Stage4Dates",
    "Stage5Events",
    "Stage6Relations",
]
