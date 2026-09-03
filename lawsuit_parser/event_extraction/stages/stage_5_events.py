"""Stage 5: Event Synthesis.

Reads Stage 4's dates.json (dates parsed and grouped by paragraph, each
with candidate actors entities.json already resolved nearby). Every date
is first classified (see _resolve_span) as either resolvable - its text
actually appears somewhere in the document's canonical body text - or
stamp-only - it only ever came from a page header/footer/e-filing stamp
Stage 1 scanned once from page 1 (see
stage_1_metadata._extract_from_docling), so it has no counterpart passage
in the body text at all. Stamp-only dates are set aside in
stamp_dates.json and never reach event synthesis below - there's nothing
to quote or ask an LLM about.

Every resolvable date becomes one Event, graph-ready (actors are canonical
names entities.json already resolved, never invented). Two modes, per
[stage_5].use_llm:

- False (default): no LLM call. `description` is a direct quote - the
  sentence containing the date, plus one sentence of context on each side -
  and `actors` is the cluster's full candidate_actors, uncurated.
- True: an LLM reads each cluster's citation and fills in event_type/
  description/outcome and curates actors down to who was actually
  involved, constrained to candidate_actors via a JSON-schema enum.

Produces events.json and stamp_dates.json.
"""

import bisect
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from tqdm import tqdm

from ..base import BaseStage
from ..llm_validation import (
    synthesize_events_batch_with_llm,
    synthesize_events_with_llm,
    synthesize_events_with_nuextract,
)
from ..models import (
    DateCluster,
    DateEntry,
    DatesArtifact,
    Event,
    EventTimeline,
    FilesScan,
    StampDate,
    StampDatesArtifact,
    SummariesArtifact,
)
from ..utils import split_sentences

logger = logging.getLogger(__name__)

DocContext = Callable[[str], tuple[str, list[tuple[int, int]]]]


def _resolve_span(doc_text: str, cluster: DateCluster, date_entry: DateEntry) -> tuple[int, int] | None:
    """date_entry.char_start/char_end is only trustworthy when it actually
    points at date_entry.text in doc_text - some ExtractedDate sources
    (e.g. Stage 1's docling_first_page/docling_header passes, which scan a
    separately assembled header/first-page string) compute offsets against
    a different, shorter text than the full document text loaded here, so
    a mismatch is expected for those and not a bug in this stage. Falls
    back to searching doc_text for the date's own text, preferring an
    occurrence inside the cluster's own paragraph span, then the closest
    occurrence anywhere in the document (Stage 4 grouped this date into
    that paragraph using the same untrustworthy offset, so the paragraph
    itself can be wrong too - a docling-only date can be genuinely absent
    from the paragraph Stage 4 assigned it to even though it does appear,
    correctly, elsewhere in the same document).

    Returns:
        (char_start, char_end) into doc_text, or None if the date text
        can't be located anywhere in it (e.g. a form field/stamp Docling
        read that the plain-text extraction never captured at all).
    """
    cs, ce = date_entry.char_start, date_entry.char_end
    if cs is not None and ce is not None and doc_text[cs:ce] == date_entry.text:
        return cs, ce

    if cluster.char_start is not None and cluster.char_end is not None:
        idx = doc_text.find(date_entry.text, cluster.char_start, cluster.char_end)
        if idx != -1:
            return idx, idx + len(date_entry.text)

    anchor = cluster.char_start if cluster.char_start is not None else 0
    best_idx, best_dist = None, None
    start = 0
    while (idx := doc_text.find(date_entry.text, start)) != -1:
        dist = abs(idx - anchor)
        if best_dist is None or dist < best_dist:
            best_idx, best_dist = idx, dist
        start = idx + 1
    if best_idx is not None:
        return best_idx, best_idx + len(date_entry.text)

    return None


def _quote_with_context(doc_text: str, sentence_spans: list[tuple[int, int]], char_start: int, char_end: int) -> str:
    """The sentence containing [char_start, char_end), plus one sentence of
    context on each side, with the date substring itself wrapped in ** -
    same marker convention as DateCluster.citation. Widened (not clipped) to
    always fully include [char_start, char_end) even if the sentence
    tokenizer's boundary falls short of it."""
    starts = [s for s, _ in sentence_spans]
    idx = max(0, min(bisect.bisect_right(starts, char_start) - 1, len(sentence_spans) - 1))
    lo, hi = max(0, idx - 1), min(len(sentence_spans) - 1, idx + 1)
    quote_start = min(sentence_spans[lo][0], char_start)
    quote_end = max(sentence_spans[hi][1], char_end)
    marked = doc_text[quote_start:char_start] + "**" + doc_text[char_start:char_end] + "**" + doc_text[char_end:quote_end]
    return marked.strip()


class Stage5Events(BaseStage):
    """Stage 5: Build an Event per Stage 4 date, either as a direct quote
    (default) or LLM-synthesized (see module docstring / [stage_5].use_llm).

    Depends on Stage 4's dates.json. The LLM path also optionally uses
    Stage 1's files_scan.json (document_title) and Stage 3's
    summaries.json (document_summary) as context hints, if either is
    present; the quote path uses files_scan.json for each document's
    file_name (to load its full text) and doesn't need summaries.json.

    Outputs:
    - events.json: One Event per resolvable date in an eligible
      (non-standalone) Stage 4 cluster.
    - stamp_dates.json: Dates set aside as stamp-only - see StampDate.
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
            self.save_artifact(case_id, "stamp_dates.json", StampDatesArtifact(case_id=case_id))
            return

        dates_artifact = self.load_artifact(case_id, "dates.json", DatesArtifact)
        doc_context = self._doc_context_loader(case_id)

        resolved, stamp_dates = self._classify_dates(dates_artifact, doc_context)
        self.save_artifact(case_id, "stamp_dates.json", StampDatesArtifact(case_id=case_id, dates=stamp_dates))
        logger.info(
            f"\n→ {len(stamp_dates)} stamp-only dates set aside to stamp_dates.json "
            f"(page header/footer/e-filing stamp text Stage 1 scanned once from page 1, "
            f"absent from the canonical body text - see StampDate)"
        )

        if not config.get("use_llm", False):
            self._build_quote_events(case_id, resolved, doc_context)
        else:
            self._build_llm_events(case_id, resolved, config)

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
        events_dir = self.get_events_dir(case_id)
        return [events_dir / "events.json", events_dir / "stamp_dates.json"]

    def _doc_context_loader(self, case_id: str) -> DocContext:
        """A cached doc_id -> (full text, sentence spans) lookup, shared by
        date classification (_classify_dates) and the quote-based path
        (_build_quote_events) - each document is loaded and sentence-split
        at most once per run regardless of how many dates/clusters it has."""
        file_names_by_doc = self._load_file_names(case_id)
        doc_text_cache: dict[str, str] = {}
        sentence_spans_cache: dict[str, list[tuple[int, int]]] = {}

        def doc_context(doc_id: str) -> tuple[str, list[tuple[int, int]]]:
            if doc_id not in doc_text_cache:
                file_name = file_names_by_doc.get(doc_id, "")
                text = self.load_document_text(case_id, doc_id, file_name) if file_name else ""
                doc_text_cache[doc_id] = text
                sentence_spans_cache[doc_id] = split_sentences(text)
            return doc_text_cache[doc_id], sentence_spans_cache[doc_id]

        return doc_context

    def _classify_dates(
        self, dates_artifact: DatesArtifact, doc_context: DocContext
    ) -> tuple[list[tuple[DateCluster, DateEntry, tuple[int, int]]], list[StampDate]]:
        """Split every date in every eligible (non-standalone) cluster into
        resolvable (its text actually appears in the document, at the
        returned span) vs stamp-only (see StampDate) - shared by both
        [stage_5].use_llm modes, so a stamp-only date never reaches event
        synthesis either way, LLM calls included.

        Returns:
            (resolved: one (cluster, date_entry, span) per resolvable date,
             stamp_dates: one StampDate per date that couldn't be found)
        """
        eligible = [c for c in dates_artifact.clusters if c.citation and c.dates]

        resolved: list[tuple[DateCluster, DateEntry, tuple[int, int]]] = []
        stamp_dates: list[StampDate] = []
        for cluster in eligible:
            doc_text, _ = doc_context(cluster.doc_id)
            for date_entry in cluster.dates:
                span = _resolve_span(doc_text, cluster, date_entry) if doc_text else None
                if span is None:
                    stamp_dates.append(StampDate(
                        cluster_id=cluster.cluster_id,
                        doc_id=cluster.doc_id,
                        date_id=date_entry.date_id,
                        text=date_entry.text,
                        parsed_date=date_entry.parsed_date,
                        date_type=date_entry.date_type,
                        source=date_entry.source,
                    ))
                else:
                    resolved.append((cluster, date_entry, span))

        return resolved, stamp_dates

    def _build_quote_events(
        self,
        case_id: str,
        resolved: list[tuple[DateCluster, DateEntry, tuple[int, int]]],
        doc_context: DocContext,
    ) -> None:
        """Build one Event per resolved date, no LLM call: `quote` is
        a direct quote (see _quote_with_context), `summary` is left unset,
        `actors` is the source cluster's full candidate_actors (uncurated),
        `event_type`/`outcome` are left unset."""
        events: list[Event] = []
        for event_id_counter, (cluster, date_entry, span) in enumerate(resolved):
            doc_text, sentence_spans = doc_context(cluster.doc_id)
            events.append(Event(
                event_id=f"event_{event_id_counter:04d}",
                cluster_id=cluster.cluster_id,
                quote=_quote_with_context(doc_text, sentence_spans, *span),
                summary=None,
                actors=cluster.candidate_actors,
                dates=[date_entry.text],
                date_parsed=date_entry.parsed_date,
                source_doc_id=cluster.doc_id,
                char_start=span[0],
                char_end=span[1],
                confidence=1.0,
            ))

        logger.info(f"\n→ {len(events)} events (direct quotes, no LLM)")
        self.save_artifact(case_id, "events.json", EventTimeline(case_id=case_id, events=events))

    def _build_llm_events(
        self,
        case_id: str,
        resolved: list[tuple[DateCluster, DateEntry, tuple[int, int]]],
        config: dict[str, Any],
    ) -> None:
        """Build Event(s) per cluster via an LLM (see module docstring),
        restricted to each cluster's resolved (non-stamp-only) dates - a
        cluster whose dates were all stamp-only doesn't appear here at
        all, so it costs nothing. Generates both a direct quote from the
        source paragraph and an LLM-synthesized summary for each event."""
        titles_by_doc, summaries_by_doc = self._load_doc_context(case_id)
        doc_context = self._doc_context_loader(case_id)

        backend = config.get("llm_backend", "ollama")
        llm_model = config["llm_model"]
        llm_base_url = config["llm_base_url"]
        # Batching (several clusters synthesized in one call, see
        # synthesize_events_batch_with_llm) is Ollama-only - NuExtract's
        # template contract doesn't support the per-key nested schema this
        # relies on, so that backend always makes one call per distinct
        # cluster regardless of this setting.
        batch_size = max(1, config.get("batch_size", 1)) if backend != "nuextract" else 1

        by_cluster: dict[str, tuple[DateCluster, list[DateEntry], dict[str, tuple[int, int]]]] = {}
        for cluster, date_entry, span in resolved:
            cluster_data = by_cluster.setdefault(cluster.cluster_id, (cluster, [], {}))
            cluster_data[1].append(date_entry)
            cluster_data[2][date_entry.text] = span

        # Dedupe by (citation, resolved dates, candidate_actors) *before*
        # calling the LLM at all, not just via a cache checked as we go: at
        # temperature=0 an identical prompt always produces an identical
        # result, and a large share of clusters are exact-duplicate text -
        # most commonly a repeated e-filing page-footer stamp (e.g.
        # NYSCEF's "FILED: NEW YORK COUNTY CLERK ..."), which showed up on
        # ~52% of case_95's clusters. document_title/document_summary are
        # left out of the key even though they're part of the prompt -
        # they're optional context hints (see _load_doc_context), not
        # something a bare repeated stamp's answer should hinge on, and
        # keying on them would defeat cross-document cache hits for the
        # exact cases this is meant to catch.
        def cache_key(cluster: DateCluster, dates: list[DateEntry]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
            return (cluster.citation, tuple(d.text for d in dates), tuple(sorted(cluster.candidate_actors)))

        pending: dict[tuple[str, tuple[str, ...], tuple[str, ...]], tuple[DateCluster, list[DateEntry], dict[str, tuple[int, int]]]] = {}
        cluster_keys: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {}
        for cluster, dates, spans in by_cluster.values():
            key = cache_key(cluster, dates)
            cluster_keys[cluster.cluster_id] = key
            pending.setdefault(key, (cluster, dates, spans))
        cache_hits = len(by_cluster) - len(pending)

        results: dict[tuple[str, tuple[str, ...], tuple[str, ...]], list[dict]] = {}
        pending_keys = list(pending.keys())
        num_calls = len(pending_keys) if batch_size == 1 else -(-len(pending_keys) // batch_size)
        pbar = tqdm(total=num_calls, desc="Stage 5: events", unit="call", file=sys.__stderr__)
        for i in range(0, len(pending_keys), batch_size):
            batch_keys = pending_keys[i:i + batch_size]
            batch = [pending[k] for k in batch_keys]
            pbar.set_postfix_str(batch[0][0].doc_id)

            if backend == "nuextract" or batch_size == 1:
                for key, (cluster, dates, _spans) in zip(batch_keys, batch):
                    synthesizer = synthesize_events_with_nuextract if backend == "nuextract" else synthesize_events_with_llm
                    results[key] = synthesizer(
                        citation=cluster.citation,
                        dates=[d.text for d in dates],
                        candidate_actors=cluster.candidate_actors,
                        document_title=titles_by_doc.get(cluster.doc_id),
                        document_summary=summaries_by_doc.get(cluster.doc_id),
                        model=llm_model,
                        base_url=llm_base_url,
                    )
            else:
                items = [
                    {
                        "key": f"c{j}",
                        "citation": cluster.citation,
                        "dates": [d.text for d in dates],
                        "candidate_actors": cluster.candidate_actors,
                        "document_title": titles_by_doc.get(cluster.doc_id),
                        "document_summary": summaries_by_doc.get(cluster.doc_id),
                    }
                    for j, (cluster, dates, _spans) in enumerate(batch)
                ]
                batch_results = synthesize_events_batch_with_llm(items=items, model=llm_model, base_url=llm_base_url)
                for j, key in enumerate(batch_keys):
                    results[key] = batch_results.get(f"c{j}", [])
            pbar.update(1)
        pbar.close()

        events: list[Event] = []
        event_id_counter = 0
        clusters_with_events = 0

        for cluster, dates, date_spans in by_cluster.values():
            raw_events = results.get(cluster_keys[cluster.cluster_id], [])
            if raw_events:
                clusters_with_events += 1

            entry_by_text = {date.text: date for date in dates}
            doc_text, sentence_spans = doc_context(cluster.doc_id)

            for raw_event in raw_events:
                covered = [entry_by_text[t] for t in raw_event["dates"] if t in entry_by_text]
                parsed = sorted(d.parsed_date for d in covered if d.parsed_date is not None)

                # Generate quote from the first date in the event, or use cluster citation if no dates
                quote = cluster.citation or ""
                if raw_event["dates"] and raw_event["dates"][0] in date_spans:
                    first_date_span = date_spans[raw_event["dates"][0]]
                    quote = _quote_with_context(doc_text, sentence_spans, *first_date_span)

                events.append(Event(
                    event_id=f"event_{event_id_counter:04d}",
                    cluster_id=cluster.cluster_id,
                    event_type=raw_event["event_type"],
                    quote=quote,
                    summary=raw_event["description"],
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
            f"\n→ {len(events)} events from {clusters_with_events}/{len(by_cluster)} eligible clusters "
            f"({cache_hits} cache hits, {len(pending)} distinct clusters in {num_calls} LLM call(s), "
            f"batch_size={batch_size})"
        )
        self.save_artifact(case_id, "events.json", EventTimeline(case_id=case_id, events=events))

    def _load_file_names(self, case_id: str) -> dict[str, str]:
        """doc_id -> original file_name (see files_scan.json), needed to
        load a document's full text."""
        if not self.artifact_exists(case_id, "files_scan.json"):
            return {}
        files_scan = self.load_artifact(case_id, "files_scan.json", FilesScan)
        return {doc.doc_id: doc.file_name for doc in files_scan.documents}

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
