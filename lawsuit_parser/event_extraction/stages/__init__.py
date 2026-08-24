"""Pipeline stages for event extraction."""

from .stage_1_metadata import Stage1Metadata
from .stage_2_gliner import Stage2GLiNER
from .stage_3_summary import Stage3Summary

# Registry of all available stages
STAGES = [
    Stage1Metadata,
    Stage2GLiNER,
    Stage3Summary,
]

__all__ = ["STAGES", "Stage1Metadata", "Stage2GLiNER", "Stage3Summary"]
