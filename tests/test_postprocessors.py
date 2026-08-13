"""Tests for the postprocessing pipeline."""

from lawsuit_parser.parsers.pdf_parser import ParsedDocument
from lawsuit_parser.postprocessors import (
    BasePostprocessingStep,
    PassthroughStep,
    PostprocessingPipeline,
    default_postprocessors,
)


def test_passthrough_step_returns_document_unchanged():
    document = ParsedDocument(title="Motion", raw_text="hello world")

    result = PassthroughStep().process(document)

    assert result is document


def test_pipeline_runs_steps_in_order():
    document = ParsedDocument(raw_text="hello")

    class AppendStep(BasePostprocessingStep):
        def __init__(self, suffix: str) -> None:
            self.suffix = suffix

        def process(self, document: ParsedDocument) -> ParsedDocument:
            document.raw_text += self.suffix
            return document

    pipeline = PostprocessingPipeline([AppendStep(" world"), AppendStep("!")])
    result = pipeline.run(document)

    assert result.raw_text == "hello world!"


def test_empty_pipeline_returns_document_unchanged():
    document = ParsedDocument(raw_text="hello")

    result = PostprocessingPipeline().run(document)

    assert result is document


def test_default_postprocessors_is_passthrough_only():
    steps = default_postprocessors()

    assert len(steps) == 1
    assert isinstance(steps[0], PassthroughStep)
