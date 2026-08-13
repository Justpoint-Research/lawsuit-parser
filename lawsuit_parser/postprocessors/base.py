"""Postprocessing stage that runs after text extraction.

Postprocessing is a chain ("pipeline") of steps. Each step takes the
`ParsedDocument` produced by text extraction (or by the previous step) and
returns a `ParsedDocument` for the next step to consume.

To add a new step, either:
- write a plain function matching the `PostprocessingStep` signature, or
- subclass `BasePostprocessingStep` and implement `process`.
"""

from abc import ABC, abstractmethod
from typing import Protocol

from lawsuit_parser.parsers.pdf_parser import ParsedDocument


class PostprocessingStep(Protocol):
    """Interface for a single postprocessing step."""

    def __call__(self, document: ParsedDocument) -> ParsedDocument: ...


class BasePostprocessingStep(ABC):
    """Convenience base class for postprocessing steps implemented as classes."""

    @abstractmethod
    def process(self, document: ParsedDocument) -> ParsedDocument:
        """Transform the document and return the result."""
        raise NotImplementedError

    def __call__(self, document: ParsedDocument) -> ParsedDocument:
        return self.process(document)


class PostprocessingPipeline:
    """Runs a sequence of postprocessing steps, in order.

    The output of each step is fed as the input to the next.
    """

    def __init__(self, steps: list[PostprocessingStep] | None = None) -> None:
        self.steps = list(steps) if steps else []

    def run(self, document: ParsedDocument) -> ParsedDocument:
        for step in self.steps:
            document = step(document)
        return document

    def __call__(self, document: ParsedDocument) -> ParsedDocument:
        return self.run(document)