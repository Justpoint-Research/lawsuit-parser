"""Dummy postprocessing step, used as an example and as the default no-op."""

from lawsuit_parser.parsers.pdf_parser import ParsedDocument
from lawsuit_parser.postprocessors.base import BasePostprocessingStep


class PassthroughStep(BasePostprocessingStep):
    """Example postprocessing step that passes the document through unchanged."""

    def process(self, document: ParsedDocument) -> ParsedDocument:
        return document