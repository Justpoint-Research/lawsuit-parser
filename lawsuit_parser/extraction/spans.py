"""Stage 3: Exhaustive span sweep with GLiNER.

Runs GLiNER over 100% of segments (not a sample).
This is the recall denominator for the entire pipeline.

Every returned span must be realigned to exact char offsets.
"""

import logging
from collections import defaultdict

from .models import GlinerRunner, RawSpan
from .schemas import GlinerSpan, Span, SpansArtifact, SegmentsArtifact
from .store import ArtifactStore

logger = logging.getLogger(__name__)


def realign_span(
    raw_span: RawSpan,
    segment_text: str,
    canonical_text: str,
    seg_char_start: int,
    doc_id: str,
) -> Span | None:
    """Realign a GLiNER span to exact char offsets in canonical text.

    GLiNER returns entity strings and offsets relative to its input.
    We must find the exact location in canonical text.

    Args:
        raw_span: Raw span from GLiNER
        segment_text: The text passed to GLiNER
        canonical_text: Full canonical text for the document
        seg_char_start: Starting char offset of segment in canonical text
        doc_id: Document ID

    Returns:
        Realigned Span if unique match found, None otherwise
    """
    target_text = raw_span.text

    # First verify the text appears in segment_text at the expected offset
    if not (
        raw_span.start >= 0
        and raw_span.end <= len(segment_text)
        and segment_text[raw_span.start : raw_span.end] == target_text
    ):
        logger.warning(
            f"GLiNER offset mismatch in segment: "
            f"expected '{target_text}' at [{raw_span.start}:{raw_span.end}]"
        )
        return None

    # Search for the text in the canonical text within the segment's range
    segment_in_canonical = canonical_text[
        seg_char_start : seg_char_start + len(segment_text)
    ]

    # Find all occurrences in this segment
    occurrences = []
    start = 0
    while True:
        pos = segment_in_canonical.find(target_text, start)
        if pos == -1:
            break
        occurrences.append(pos)
        start = pos + 1

    if len(occurrences) == 0:
        logger.warning(f"Could not find '{target_text}' in canonical text segment")
        return None

    if len(occurrences) > 1:
        # Multiple occurrences - try to use GLiNER's offset as a hint
        # The occurrence closest to raw_span.start is most likely correct
        closest = min(occurrences, key=lambda p: abs(p - raw_span.start))
        char_start = seg_char_start + closest
    else:
        # Unique match
        char_start = seg_char_start + occurrences[0]

    char_end = char_start + len(target_text)

    # Verify
    if canonical_text[char_start:char_end] != target_text:
        logger.error(
            f"Realignment verification failed: "
            f"canonical[{char_start}:{char_end}] != '{target_text}'"
        )
        return None

    return Span(
        doc_id=doc_id,
        char_start=char_start,
        char_end=char_end,
        text=target_text,
    )


def sweep_spans(
    case_id: str,
    segments: SegmentsArtifact,
    gliner: GlinerRunner,
    store: ArtifactStore,
    labels: list[str],
    threshold: float,
    batch_size: int = 8,
) -> SpansArtifact:
    """Run GLiNER over all segments to extract spans.

    This is Stage 3.

    Args:
        case_id: Case identifier
        segments: Segmentation artifact from stage 0
        gliner: GLiNER runner (must be in context)
        store: Artifact store
        labels: Entity labels to extract
        threshold: Confidence threshold
        batch_size: Batch size for GLiNER

    Returns:
        SpansArtifact with all extracted spans
    """
    counters = {
        "total_segments": len(segments.segments),
        "total_returned": 0,
        "realignment_failures": 0,
        "spans_by_label": defaultdict(int),
        "score_sum_by_label": defaultdict(float),
    }

    all_spans = []

    # Prepare batches
    batches = []
    current_batch = []
    current_batch_segs = []

    for segment in segments.segments:
        canonical_text = store.read_canonical_text(segment.doc_id)
        segment_text = canonical_text[segment.char_start : segment.char_end]

        current_batch.append(segment_text)
        current_batch_segs.append(segment)

        if len(current_batch) >= batch_size:
            batches.append((current_batch, current_batch_segs))
            current_batch = []
            current_batch_segs = []

    # Add remaining
    if current_batch:
        batches.append((current_batch, current_batch_segs))

    # Process batches
    for batch_idx, (batch_texts, batch_segs) in enumerate(batches):
        if (batch_idx + 1) % 10 == 0:
            logger.info(f"Processing batch {batch_idx + 1}/{len(batches)}")

        # Run GLiNER
        predictions = gliner.predict_batch(batch_texts, labels, threshold)

        # Process predictions
        for seg_idx, (segment, raw_spans) in enumerate(zip(batch_segs, predictions)):
            canonical_text = store.read_canonical_text(segment.doc_id)
            segment_text = batch_texts[seg_idx]

            for raw_span in raw_spans:
                counters["total_returned"] += 1

                # Realign to canonical text
                aligned_span = realign_span(
                    raw_span,
                    segment_text,
                    canonical_text,
                    segment.char_start,
                    segment.doc_id,
                )

                if aligned_span is None:
                    counters["realignment_failures"] += 1
                    continue

                # Create GlinerSpan
                gliner_span = GlinerSpan(
                    span=aligned_span,
                    seg_id=segment.seg_id,
                    label=raw_span.label,
                    score=raw_span.score,
                )

                all_spans.append(gliner_span)
                counters["spans_by_label"][raw_span.label] += 1
                counters["score_sum_by_label"][raw_span.label] += raw_span.score

    # Compute mean scores
    mean_scores = {}
    for label, count in counters["spans_by_label"].items():
        if count > 0:
            mean_scores[label] = counters["score_sum_by_label"][label] / count

    # Check realignment failure rate
    if counters["total_returned"] > 0:
        failure_rate = counters["realignment_failures"] / counters["total_returned"]
        if failure_rate > 0.02:
            logger.error(
                f"Realignment failure rate too high: {failure_rate:.2%} "
                f"({counters['realignment_failures']}/{counters['total_returned']})"
            )

    logger.info(f"Stage 3 counters:")
    logger.info(f"  Total segments: {counters['total_segments']}")
    logger.info(f"  Total spans returned: {counters['total_returned']}")
    logger.info(f"  Realignment failures: {counters['realignment_failures']}")
    logger.info(f"  Spans by label: {dict(counters['spans_by_label'])}")
    logger.info(f"  Mean scores by label: {mean_scores}")

    return SpansArtifact(
        case_id=case_id,
        label_set=labels,
        model_id=gliner.model_name,
        threshold=threshold,
        spans=all_spans,
        realignment_failures=counters["realignment_failures"],
    )
