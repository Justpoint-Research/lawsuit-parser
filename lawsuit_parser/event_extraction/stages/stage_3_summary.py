"""Stage 3: Document Summary.

For each document, generates a short (1-3 sentence) summary of its core
purpose - why it exists, what it accomplishes - via a local LLM. Produces
summaries.json.
"""

import logging
import sys
from pathlib import Path
from typing import Any

from tqdm import tqdm

from ..base import BaseStage
from ..llm_validation import summarize_document_with_llm, summarize_document_with_nuextract
from ..models import DocumentSummary, FilesScan, SummariesArtifact

logger = logging.getLogger(__name__)


class Stage3Summary(BaseStage):
    """Stage 3: Summarize each document's core purpose with an LLM.

    Depends on Stage 1's files_scan.json for the doc_id/file_name list and
    each document's identified title (document_title), used as a hint.

    Outputs:
    - summaries.json: A short summary for every document in the case
    """

    stage_number = 3
    stage_name = "summary"

    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute Stage 3 for a given case.

        Args:
            case_id: Case identifier
            config: Stage-specific configuration
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 3: Document Summary - {case_id}")
        logger.info(f"{'='*60}\n")

        if not config.get("summarize_documents", True):
            logger.info("Document summarization disabled (summarize_documents=false), skipping.")
            self.save_artifact(case_id, "summaries.json", SummariesArtifact(case_id=case_id))
            return

        files_scan = self.load_artifact(case_id, "files_scan.json", FilesScan)

        backend = config.get("llm_backend", "ollama")
        llm_model = config["llm_model"]
        llm_base_url = config["llm_base_url"]
        max_chars = config.get("max_chars", 8000)
        summarizer = summarize_document_with_nuextract if backend == "nuextract" else summarize_document_with_llm

        summaries: list[DocumentSummary] = []
        summarized_count = 0

        pbar = tqdm(files_scan.documents, desc="Stage 3: summary", unit="doc", file=sys.__stderr__)
        for doc in pbar:
            pbar.set_postfix_str(doc.file_name)
            logger.info(f"\n→ Summarizing {doc.file_name} (doc_id={doc.doc_id})...")

            text = self.load_document_text(case_id, doc.doc_id, doc.file_name)
            if not text:
                logger.warning(f"  Warning: No text found for {doc.doc_id}, skipping")
                summaries.append(DocumentSummary(doc_id=doc.doc_id, file_name=doc.file_name))
                continue

            summary = summarizer(
                text_excerpt=text[:max_chars],
                document_title=doc.document_title,
                model=llm_model,
                base_url=llm_base_url,
            )
            if summary:
                logger.info(f"  Summary: {summary}")
                summarized_count += 1

            summaries.append(DocumentSummary(
                doc_id=doc.doc_id,
                file_name=doc.file_name,
                summary=summary,
                model=llm_model if summary else None,
            ))

        logger.info(f"\n→ Summarized {summarized_count} of {len(files_scan.documents)} documents")

        artifact = SummariesArtifact(case_id=case_id, documents=summaries)
        self.save_artifact(case_id, "summaries.json", artifact)

        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 3 Complete!")
        logger.info(f"{'='*60}\n")

    def validate_inputs(self, case_id: str) -> bool:
        """Check that Stage 1's files_scan.json exists.

        Args:
            case_id: Case identifier

        Returns:
            True if files_scan.json is available
        """
        if not self.artifact_exists(case_id, "files_scan.json"):
            logger.error(f"Error: files_scan.json not found for {case_id} - run Stage 1 first")
            return False
        return True

    def get_outputs(self, case_id: str) -> list[Path]:
        """Return paths to stage outputs.

        Args:
            case_id: Case identifier

        Returns:
            List of output file paths
        """
        return [self.get_events_dir(case_id) / "summaries.json"]
