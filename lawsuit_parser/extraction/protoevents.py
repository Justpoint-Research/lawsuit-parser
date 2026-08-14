"""Stage 4: Proto-events with optional GLiNER-Relex.

Priority segment selection always runs (even when Relex disabled).
A segment is priority if it contains:
- A temporal expression span, OR
- Both a legal action/event span AND a party mention

If Relex is enabled, run it over priority segments to produce proto-events.
If disabled, emit empty proto_events list - this must be a supported path.
"""

import logging
from collections import defaultdict

from .models import RelexRunner, DisabledRelexRunner, RawProtoEvent
from .schemas import (
    ProtoEvent,
    ProtoEventEdge,
    ProtoEventsArtifact,
    SegmentsArtifact,
    SpansArtifact,
    RegistryArtifact,
    Span,
)
from .store import ArtifactStore

logger = logging.getLogger(__name__)


def select_priority_segments(
    segments: SegmentsArtifact,
    spans: SpansArtifact,
    registry: RegistryArtifact,
) -> list[str]:
    """Select priority segments for event extraction.

    A segment is priority if it contains:
    - A temporal expression span, OR
    - Both a legal action/event span AND a party mention

    Args:
        segments: Segmentation artifact
        spans: Spans artifact from stage 3
        registry: Registry artifact from stage 2

    Returns:
        List of segment IDs
    """
    # Build indices
    temporal_segs = set()
    event_segs = set()
    party_mention_segs = set()

    for gliner_span in spans.spans:
        if gliner_span.label == "temporal expression":
            temporal_segs.add(gliner_span.seg_id)
        elif gliner_span.label == "legal action or event":
            event_segs.add(gliner_span.seg_id)

    # Find which segments have party mentions
    for mention in registry.mentions:
        # Find segment containing this span
        for segment in segments.segments:
            if segment.doc_id == mention.span.doc_id:
                if (
                    segment.char_start <= mention.span.char_start
                    and segment.char_end >= mention.span.char_end
                ):
                    party_mention_segs.add(segment.seg_id)
                    break

    # Priority: temporal OR (event AND party)
    priority = temporal_segs | (event_segs & party_mention_segs)

    return sorted(list(priority))


def build_proto_events(
    case_id: str,
    segments: SegmentsArtifact,
    spans: SpansArtifact,
    registry: RegistryArtifact,
    relex: RelexRunner,
    store: ArtifactStore,
    enabled: bool = False,
    relations: list[str] | None = None,
) -> ProtoEventsArtifact:
    """Build proto-events from priority segments.

    This is Stage 4.

    Args:
        case_id: Case identifier
        segments: Segmentation artifact from stage 0
        spans: Spans artifact from stage 3
        registry: Registry artifact from stage 2
        relex: Relation extraction runner
        store: Artifact store
        enabled: Whether Relex is enabled
        relations: Relation types to extract

    Returns:
        ProtoEventsArtifact
    """
    counters = {
        "priority_segments": 0,
        "proto_events": 0,
        "edges_by_relation": defaultdict(int),
    }

    # Step 1: Always select priority segments
    priority_seg_ids = select_priority_segments(segments, spans, registry)
    counters["priority_segments"] = len(priority_seg_ids)

    logger.info(f"Selected {len(priority_seg_ids)} priority segments")

    proto_events = []

    # Step 2: If Relex enabled, extract proto-events
    if enabled and relations:
        proto_id_counter = 1

        for seg_id in priority_seg_ids:
            # Find segment
            segment = None
            for seg in segments.segments:
                if seg.seg_id == seg_id:
                    segment = seg
                    break

            if not segment:
                logger.warning(f"Priority segment {seg_id} not found")
                continue

            # Get segment text
            canonical_text = store.read_canonical_text(segment.doc_id)
            segment_text = canonical_text[segment.char_start : segment.char_end]

            # Run Relex
            try:
                raw_proto_events = relex.predict(segment_text, relations)
            except Exception as e:
                logger.error(f"Relex failed for {seg_id}: {e}")
                continue

            # Convert to ProtoEvent
            for raw_pe in raw_proto_events:
                # Realign predicate
                pred_start = canonical_text.find(
                    raw_pe.predicate.text, segment.char_start
                )
                if pred_start == -1:
                    logger.warning(
                        f"Could not realign predicate '{raw_pe.predicate.text}'"
                    )
                    continue

                pred_end = pred_start + len(raw_pe.predicate.text)
                predicate_span = Span(
                    doc_id=segment.doc_id,
                    char_start=pred_start,
                    char_end=pred_end,
                    text=raw_pe.predicate.text,
                )

                # Realign edges
                edges = []
                for raw_rel in raw_pe.relations:
                    tail_start = canonical_text.find(
                        raw_rel.tail.text, segment.char_start
                    )
                    if tail_start == -1:
                        logger.warning(
                            f"Could not realign relation tail '{raw_rel.tail.text}'"
                        )
                        continue

                    tail_end = tail_start + len(raw_rel.tail.text)
                    tail_span = Span(
                        doc_id=segment.doc_id,
                        char_start=tail_start,
                        char_end=tail_end,
                        text=raw_rel.tail.text,
                    )

                    edge = ProtoEventEdge(
                        relation=raw_rel.relation,
                        target=tail_span,
                        score=raw_rel.score,
                    )

                    edges.append(edge)
                    counters["edges_by_relation"][raw_rel.relation] += 1

                # Create proto-event
                proto_event = ProtoEvent(
                    proto_id=f"pe_{proto_id_counter:04d}",
                    seg_id=seg_id,
                    predicate=predicate_span,
                    edges=edges,
                )

                proto_events.append(proto_event)
                proto_id_counter += 1
                counters["proto_events"] += 1

    logger.info(f"Stage 4 counters: {counters}")

    return ProtoEventsArtifact(
        case_id=case_id,
        enabled=enabled,
        proto_events=proto_events,
        priority_segments=priority_seg_ids,
    )
