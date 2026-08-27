"""Stage 5: Event Synthesis.

Reads Stage 4's dates.json (dates parsed and grouped by paragraph, each
with candidate actors entities.json already resolved nearby) and asks an
LLM, once per cluster, what actually happened, its outcome (if stated),
and who was involved - "who" is constrained to each cluster's own
candidate_actors, so the result is graph-ready: every actor named is
grounded to a name GLiNER already resolved, never invented by the LLM.

Produces events.json.
"""

import logging
import sys
from pathlib import Path
from typing import Any

from tqdm import tqdm

from ..base import BaseStage
from ..llm_validation import synthesize_events_with_llm, synthesize_events_with_nuextract
from ..models import DatesArtifact, Event, EventTimeline, FilesScan, SummariesArtifact

logger = logging.getLogger(__name__)


class Stage5Events(BaseStage):
    """Stage 5: Synthesize events from Stage 4's date clusters with an LLM.

    Depends on Stage 4's dates.json. Optionally uses Stage 1's
    files_scan.json (document_title) and Stage 3's summaries.json
    (document_summary) as context hints, if either is present.

    Outputs:
    - events.json: One or more Event records per date cluster that
      corresponds to an identifiable case event.
    """

    stage_number = 5
    stage_name = "events"

    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute Stage 5 for a given case.

        Args:
            case_id: Case identifier
            config: Stage-specific configuration
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 5: Event Synthesis - {case_id}")
        logger.info(f"{'='*60}\n")

        if not config.get("synthesize_events", True):
            logger.info("Event synthesis disabled (synthesize_events=false), skipping.")
            self.save_artifact(case_id, "events.json", EventTimeline(case_id=case_id))
            return

        dates_artifact = self.load_artifact(case_id, "dates.json", DatesArtifact)
        titles_by_doc, summaries_by_doc = self._load_doc_context(case_id)

        backend = config.get("llm_backend", "ollama")
        llm_model = config["llm_model"]
        llm_base_url = config["llm_base_url"]
        synthesizer = synthesize_events_with_nuextract if backend == "nuextract" else synthesize_events_with_llm

        events: list[Event] = []
        event_id_counter = 0
        clusters_with_events = 0
        eligible_clusters = 0
        cache_hits = 0

        # Cache raw LLM output by (citation, dates, candidate_actors): at
        # temperature=0 an identical prompt always produces an identical
        # result, and a large share of clusters are exact-duplicate text -
        # most commonly a repeated e-filing page-footer stamp (e.g. NYSCEF's
        # "FILED: NEW YORK COUNTY CLERK ..."), which showed up on ~52% of
        # case_95's clusters. document_title/document_summary are left out
        # of the key even though they're part of the prompt - they're
        # optional context hints (see _load_doc_context), not something a
        # bare repeated stamp's answer should hinge on, and keying on them
        # would defeat cross-document cache hits for the exact cases this
        # is meant to catch.
        cache: dict[tuple[str, tuple[str, ...], tuple[str, ...]], list[dict]] = {}

        pbar = tqdm(dates_artifact.clusters, desc="Stage 5: events", unit="cluster", file=sys.__stderr__)
        for cluster in pbar:
            pbar.set_postfix_str(cluster.doc_id)

            # No paragraph context to reason over - a standalone date with
            # no char offset (e.g. a confirmation notice timestamp, see
            # Stage4Dates) has nothing for the LLM to read.
            if not cluster.citation or not cluster.dates:
                continue
            eligible_clusters += 1

            date_texts = [date.text for date in cluster.dates]
            cache_key = (cluster.citation, tuple(date_texts), tuple(sorted(cluster.candidate_actors)))

            if cache_key in cache:
                cache_hits += 1
                raw_events = cache[cache_key]
            else:
                raw_events = synthesizer(
                    citation=cluster.citation,
                    dates=date_texts,
                    candidate_actors=cluster.candidate_actors,
                    document_title=titles_by_doc.get(cluster.doc_id),
                    document_summary=summaries_by_doc.get(cluster.doc_id),
                    model=llm_model,
                    base_url=llm_base_url,
                )
                cache[cache_key] = raw_events
            if raw_events:
                clusters_with_events += 1

            entry_by_text = {date.text: date for date in cluster.dates}
            for raw_event in raw_events:
                covered = [entry_by_text[t] for t in raw_event["dates"] if t in entry_by_text]
                parsed = sorted(d.parsed_date for d in covered if d.parsed_date is not None)
                events.append(Event(
                    event_id=f"event_{event_id_counter:04d}",
                    cluster_id=cluster.cluster_id,
                    event_type=raw_event["event_type"],
                    description=raw_event["description"],
                    outcome=raw_event["outcome"],
                    actors=raw_event["actors"],
                    dates=raw_event["dates"],
                    date_parsed=parsed[0] if parsed else None,
                    source_doc_id=cluster.doc_id,
                    char_start=cluster.char_start,
                    char_end=cluster.char_end,
                    confidence=raw_event["confidence"],
                ))
                event_id_counter += 1

        logger.info(
            f"\n→ {len(events)} events from {clusters_with_events}/{eligible_clusters} eligible clusters "
            f"({len(dates_artifact.clusters) - eligible_clusters} standalone clusters skipped, "
            f"{cache_hits} cache hits - {eligible_clusters - cache_hits} actual LLM calls)"
        )

        artifact = EventTimeline(case_id=case_id, events=events)
        self.save_artifact(case_id, "events.json", artifact)

        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 5 Complete!")
        logger.info(f"{'='*60}\n")

    def validate_inputs(self, case_id: str) -> bool:
        """Check if Stage 4's output is available.

        Args:
            case_id: Case identifier

        Returns:
            True if dates.json exists
        """
        if not self.artifact_exists(case_id, "dates.json"):
            logger.error(f"Error: dates.json not found for {case_id} - run Stage 4 first")
            return False
        return True

    def get_outputs(self, case_id: str) -> list[Path]:
        """Return paths to stage outputs.

        Args:
            case_id: Case identifier

        Returns:
            List of output file paths
        """
        return [self.get_events_dir(case_id) / "events.json"]

    def _load_doc_context(self, case_id: str) -> tuple[dict[str, str], dict[str, str]]:
        """Optional per-document context hints for the LLM prompt: each
        document's identified title (Stage 1) and Stage 3 summary, if
        either artifact is present - a case extracted before Stage 3
        existed still runs Stage 5, just without the summary hint.

        Returns:
            (title by doc_id, summary by doc_id) - either dict may be empty
        """
        titles: dict[str, str] = {}
        summaries: dict[str, str] = {}

        if self.artifact_exists(case_id, "files_scan.json"):
            files_scan = self.load_artifact(case_id, "files_scan.json", FilesScan)
            for doc in files_scan.documents:
                if doc.document_title:
                    titles[doc.doc_id] = doc.document_title

        if self.artifact_exists(case_id, "summaries.json"):
            summaries_artifact = self.load_artifact(case_id, "summaries.json", SummariesArtifact)
            for doc in summaries_artifact.documents:
                if doc.summary:
                    summaries[doc.doc_id] = doc.summary

        return titles, summaries
