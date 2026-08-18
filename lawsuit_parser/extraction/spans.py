"""Stage 3: Exhaustive span sweep with GLiNER.

Runs GLiNER over 100% of segments (not a sample).
This is the recall denominator for the entire pipeline.

Every returned span must be realigned to exact char offsets.
"""

import logging
import re
from collections import defaultdict

from .models import GlinerRunner, RawSpan
from .schemas import GlinerSpan, Span, SpansArtifact, SegmentsArtifact
from .store import ArtifactStore

logger = logging.getLogger(__name__)


CAPTION_SEPARATOR_RE = re.compile(
    r"\s+-\s+(?:v|vs|versus)\.?\s+-\s+|\s+(?:v|vs|versus)\.?\s+", re.IGNORECASE
)
ET_AL_RE = re.compile(r"\bet\s+al\.?$", re.IGNORECASE)
STRIP_CHARS = " .,-"


def _clean_party_name(name: str) -> str:
    """Strip surrounding punctuation/dashes and a trailing "et al." from one
    side of a split caption (e.g. "JODILYNN GIAMELLA et al -" -> "JODILYNN
    GIAMELLA")."""
    name = name.strip(STRIP_CHARS)
    name = ET_AL_RE.sub("", name).strip(STRIP_CHARS)
    return name


def parse_case_caption(caption: str) -> tuple[str, str] | None:
    """Split a short docket caption like "Judith Phillips v. Pfizer Inc. et al"
    or "JODILYNN GIAMELLA et al - v. - WRIGHT MEDICAL TECHNOLOGY INC et al"
    into (plaintiff_name, defendant_name).

    This is the DB-exported case caption (case_info.caption in the case JSON),
    not the in-document caption block segmented in stage 0 - it's a single
    "X v. Y" string, already known before any model runs.

    Returns None if the caption is empty or doesn't contain a recognizable
    "X v. Y" split.
    """
    if not caption:
        return None

    parts = CAPTION_SEPARATOR_RE.split(caption, maxsplit=1)
    if len(parts) != 2:
        return None

    plaintiff = _clean_party_name(parts[0])
    defendant = _clean_party_name(parts[1])

    if not plaintiff or not defendant:
        return None

    return plaintiff, defendant


def build_dynamic_labels(base_labels: list[str], case_caption: str | None) -> list[str]:
    """Derive case-specific plaintiff/defendant GLiNER labels from a DB caption.

    GLiNER is a zero-shot span tagger driven entirely by the label list handed
    to it per call - it has no separate channel for background context. So the
    only way to give it the plaintiff/defendant names already known from the
    case's DB record is to fold them into labels: ask it to tag
    "plaintiff (<name>)" / "defendant (<name>)" specifically, in addition to
    the generic "party or organization" catch-all (which still runs, for
    co-defendants/other parties not named in the caption).

    These are returned separately from base_labels (not merged into one list)
    because GLiNER scores every label in a call jointly - adding labels to an
    existing call measurably shifts recall on the *other* labels in that same
    call (observed: temporal-expression recall dropped ~60% on a real case
    when two party labels were appended to the base 8-label set). Callers
    should run this as its own GLiNER pass over the same segments and merge
    the resulting spans, so the base label set's behavior is unaffected.

    Args:
        base_labels: The configured label list (e.g. from extraction.toml),
            used only to skip a dynamic label that duplicates one already there
        case_caption: The case's DB caption string, or None if unavailable

    Returns:
        A list of 0 or 2 plaintiff/defendant labels.
    """
    if not case_caption:
        return []

    parsed = parse_case_caption(case_caption)
    if parsed is None:
        logger.warning(f"Could not parse case caption for dynamic labels: {case_caption!r}")
        return []

    plaintiff, defendant = parsed
    dynamic = [f"plaintiff ({plaintiff})", f"defendant ({defendant})"]

    return [label for label in dynamic if label not in base_labels]


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
    extra_label_passes: list[list[str]] | None = None,
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
        extra_label_passes: Optional additional label sets (e.g. case-specific
            plaintiff/defendant labels from build_dynamic_labels), each run as
            its own independent GLiNER call over the same segments. GLiNER
            scores every label in a call jointly, so mixing these into
            `labels` would shift recall on `labels` itself - keeping them as
            separate passes leaves `labels`'s behavior unaffected.

    Returns:
        SpansArtifact with spans from `labels` plus every pass in
        `extra_label_passes`, and label_set recording all label sets used.
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

    # Each label set (the base labels, plus any extra passes) is run as its
    # own independent GLiNER call per batch - see extra_label_passes docstring.
    label_passes = [labels] + list(extra_label_passes or [])

    for pass_idx, pass_labels in enumerate(label_passes):
        if pass_idx > 0:
            logger.info(f"Running extra label pass {pass_idx}: {pass_labels}")

        for batch_idx, (batch_texts, batch_segs) in enumerate(batches):
            if (batch_idx + 1) % 10 == 0:
                logger.info(f"Processing batch {batch_idx + 1}/{len(batches)}")

            # Run GLiNER
            predictions = gliner.predict_batch(batch_texts, pass_labels, threshold)

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
        label_set=[label for pass_labels in label_passes for label in pass_labels],
        model_id=gliner.model_name,
        threshold=threshold,
        spans=all_spans,
        realignment_failures=counters["realignment_failures"],
    )
