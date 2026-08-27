"""Stage 4: Date Clustering.

Parses every date Stage 1 found (files_scan.json's per-document
extracted_dates) into a real datetime, groups dates that fall in the same
document paragraph together (see utils.split_paragraphs), and
cross-references each group with the actors/products entities.json
(Stage 2) already resolved in that same paragraph - so each cluster is
ready for Stage 5 to turn into one or more Event records without having to
re-derive proximity itself.

Produces dates.json.
"""

import bisect
import logging
import sys
from pathlib import Path
from typing import Any

from tqdm import tqdm

from ..base import BaseStage
from ..models import DateCluster, DateEntry, DatesArtifact, EntitiesArtifact, ExtractedDate, FilesScan
from ..utils import parse_date_loosely, split_paragraphs

logger = logging.getLogger(__name__)


class Stage4Dates(BaseStage):
    """Stage 4: Parse and cluster case dates by paragraph.

    Depends on Stage 1's files_scan.json (raw ExtractedDate list + document
    text) and Stage 2's entities.json (for cross-referencing dates with the
    people/products already found nearby).

    Outputs:
    - dates.json: Every case date, parsed and grouped into same-paragraph
      clusters with their candidate actors.
    """

    stage_number = 4
    stage_name = "dates"

    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute Stage 4 for a given case.

        Args:
            case_id: Case identifier
            config: Stage-specific configuration
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 4: Date Clustering - {case_id}")
        logger.info(f"{'='*60}\n")

        files_scan = self.load_artifact(case_id, "files_scan.json", FilesScan)
        entities = self.load_artifact(case_id, "entities.json", EntitiesArtifact)

        entities_by_doc: dict[str, list] = {}
        for entity in entities.entities:
            entities_by_doc.setdefault(entity.doc_id, []).append(entity)

        clusters: list[DateCluster] = []
        cluster_id_counter = 0
        date_id_counter = 0
        parsed_count = 0

        pbar = tqdm(files_scan.documents, desc="Stage 4: dates", unit="doc", file=sys.__stderr__)
        for doc in pbar:
            pbar.set_postfix_str(doc.file_name)
            if not doc.extracted_dates:
                continue

            text = self.load_document_text(case_id, doc.doc_id, doc.file_name)
            paragraph_spans = split_paragraphs(text) if text else []
            doc_entities = entities_by_doc.get(doc.doc_id, [])
            para_starts = [start for start, _ in paragraph_spans]

            # Dates with a real char offset get grouped by which paragraph
            # they fall in; dates with none (e.g. a confirmation notice's
            # timestamp - never regex-matched against document text, see
            # Stage1Metadata) each become their own standalone cluster.
            groups: dict[int, list[ExtractedDate]] = {}
            standalone: list[ExtractedDate] = []
            for date in doc.extracted_dates:
                idx = self._paragraph_index(date.char_start, para_starts, len(paragraph_spans))
                if idx is None:
                    standalone.append(date)
                    continue
                p_start, p_end = paragraph_spans[idx]
                if not (p_start <= date.char_start < p_end):
                    standalone.append(date)
                    continue
                groups.setdefault(idx, []).append(date)

            for idx, group_dates in groups.items():
                p_start, p_end = paragraph_spans[idx]

                entries, date_id_counter, count = self._build_entries(group_dates, doc.doc_id, date_id_counter)
                parsed_count += count

                candidate_actors = sorted({
                    e.linked_actor for e in doc_entities
                    if e.linked_actor and p_start <= e.char_start < p_end
                })

                clusters.append(DateCluster(
                    cluster_id=f"cluster_{cluster_id_counter:04d}",
                    doc_id=doc.doc_id,
                    char_start=p_start,
                    char_end=p_end,
                    citation=self._mark_dates(text[p_start:p_end], p_start, group_dates),
                    dates=entries,
                    candidate_actors=candidate_actors,
                ))
                cluster_id_counter += 1

            for date in standalone:
                entries, date_id_counter, count = self._build_entries([date], doc.doc_id, date_id_counter)
                parsed_count += count
                clusters.append(DateCluster(
                    cluster_id=f"cluster_{cluster_id_counter:04d}",
                    doc_id=doc.doc_id,
                    char_start=date.char_start,
                    char_end=date.char_end,
                    citation=None,
                    dates=entries,
                    candidate_actors=[],
                ))
                cluster_id_counter += 1

        total_dates = sum(len(c.dates) for c in clusters)
        logger.info(f"\n→ {total_dates} dates in {len(clusters)} clusters ({parsed_count} parsed to a real date)")

        artifact = DatesArtifact(case_id=case_id, clusters=clusters)
        self.save_artifact(case_id, "dates.json", artifact)

        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 4 Complete!")
        logger.info(f"{'='*60}\n")

    def validate_inputs(self, case_id: str) -> bool:
        """Check if Stage 1/2 outputs are available.

        Args:
            case_id: Case identifier

        Returns:
            True if required inputs exist
        """
        required_files = ["files_scan.json", "entities.json"]

        for filename in required_files:
            if not self.artifact_exists(case_id, filename):
                logger.error(f"Error: Required artifact not found: {filename}")
                logger.info(f"       Run Stage 1/2 first!")
                return False

        return True

    def get_outputs(self, case_id: str) -> list[Path]:
        """Return paths to stage outputs.

        Args:
            case_id: Case identifier

        Returns:
            List of output file paths
        """
        return [self.get_events_dir(case_id) / "dates.json"]

    @staticmethod
    def _paragraph_index(char_start: int | None, para_starts: list[int], num_paragraphs: int) -> int | None:
        """Index into paragraph_spans containing char_start, or None if
        char_start is unset or there are no paragraphs to place it in."""
        if char_start is None or num_paragraphs == 0:
            return None
        return max(0, min(bisect.bisect_right(para_starts, char_start) - 1, num_paragraphs - 1))

    @staticmethod
    def _build_entries(
        dates: list[ExtractedDate], doc_id: str, date_id_counter: int
    ) -> tuple[list[DateEntry], int, int]:
        """Build DateEntry records for a group of ExtractedDates.

        Returns:
            (entries, next date_id_counter, count that parsed to a real date)
        """
        entries = []
        parsed_count = 0
        for date in dates:
            parsed_date = parse_date_loosely(date.text)
            if parsed_date is not None:
                parsed_count += 1
            entries.append(DateEntry(
                date_id=f"date_{date_id_counter:04d}",
                text=date.text,
                parsed_date=parsed_date,
                date_type=date.type,
                source=date.source,
                doc_id=doc_id,
                char_start=date.char_start,
                char_end=date.char_end,
            ))
            date_id_counter += 1
        return entries, date_id_counter, parsed_count

    @staticmethod
    def _mark_dates(paragraph_text: str, paragraph_offset: int, dates: list[ExtractedDate]) -> str:
        """Wrap each date's substring in ** markers within paragraph_text -
        same convention as Entity.context. Applied right-to-left so an
        earlier insertion doesn't shift a later date's offset."""
        spans = sorted(
            {
                (date.char_start - paragraph_offset, date.char_end - paragraph_offset)
                for date in dates
                if date.char_start is not None and date.char_end is not None
            },
            reverse=True,
        )
        for start, end in spans:
            if 0 <= start < end <= len(paragraph_text):
                paragraph_text = paragraph_text[:start] + "**" + paragraph_text[start:end] + "**" + paragraph_text[end:]
        return paragraph_text
