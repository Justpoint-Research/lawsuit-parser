"""Postprocessing stage applied to documents after text extraction."""

from lawsuit_parser.postprocessors.base import (
    BasePostprocessingStep,
    PostprocessingPipeline,
    PostprocessingStep,
)
from lawsuit_parser.postprocessors.passthrough import PassthroughStep


def default_postprocessors() -> list[PostprocessingStep]:
    """Default postprocessing steps applied when none are specified."""
    return [PassthroughStep()]


__all__ = [
    "BasePostprocessingStep",
    "PassthroughStep",
    "PostprocessingPipeline",
    "PostprocessingStep",
    "default_postprocessors",
]