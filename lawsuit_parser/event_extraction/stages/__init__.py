"""Pipeline stages for event extraction."""

from .stage_1_metadata import Stage1Metadata
from .stage_2_gliner import Stage2GLiNER

# Registry of all available stages
STAGES = [
    Stage1Metadata,
    Stage2GLiNER,
]

__all__ = ["STAGES", "Stage1Metadata", "Stage2GLiNER"]
