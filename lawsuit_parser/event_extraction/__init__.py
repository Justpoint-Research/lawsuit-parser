"""Event extraction pipeline for legal documents.

This module provides a modular, extensible pipeline for extracting legal events
and timelines from parsed Docling documents.
"""

from .pipeline import EventExtractionPipeline

__all__ = ["EventExtractionPipeline"]
