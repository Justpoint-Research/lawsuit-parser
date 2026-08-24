"""Stage 2: GLiNER Entity Detection.

Runs GLiNER to detect entities using the configuration from Stage 1.
Produces entities.json artifact.
"""

import bisect
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..gliner_runner import GlinerRunner
from ...parsers.batch import get_docling_dir
from ..base import BaseStage
from ..models import (
    Actor,
    EntitiesArtifact,
    Entity,
    FilesScan,
    GLiNERConfig,
    ModelConfig,
)
from ..utils import split_sentences


class Stage2GLiNER(BaseStage):
    """Stage 2: Extract entities using GLiNER.

    This stage:
    1. Loads GLiNER configuration from Stage 1
    2. Loads canonical text for each document
    3. Segments text into chunks for batch processing
    4. Runs GLiNER with both static and dynamic labels
    5. Links entities to known actors
    6. Regex gazetteer pass: adds exact named-actor mentions GLiNER's
       threshold missed (see _gazetteer_entities) - a deterministic recall
       backstop, not a replacement for GLiNER's open-vocabulary detection
    7. Saves entities.json artifact

    Outputs:
    - entities.json: All detected entities with scores and locations
    """

    stage_number = 2
    stage_name = "gliner"

    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute Stage 2 for a given case.

        Args:
            case_id: Case identifier
            config: Stage-specific configuration
        """
        print(f"\n{'='*60}")
        print(f"Stage 2: GLiNER Entity Detection - {case_id}")
        print(f"{'='*60}\n")

        # Load Stage 1 outputs
        print("→ Loading Stage 1 artifacts...")
        gliner_config = self.load_artifact(case_id, "gliner_config.json", GLiNERConfig)
        files_scan = self.load_artifact(case_id, "files_scan.json", FilesScan)

        # Override config with any stage-specific settings
        use_gpu = config.get("use_gpu", True)
        context_sentences_before = config.get("context_sentences_before", 2)
        context_sentences_after = config.get("context_sentences_after", 2)
        enable_gazetteer = config.get("enable_gazetteer", True)
        gazetteer_min_term_length = config.get("gazetteer_min_term_length", 3)

        print(f"  GLiNER model: {gliner_config.model}")
        print(f"  Threshold: {gliner_config.threshold}")
        print(f"  Batch size: {gliner_config.batch_size}")
        print(f"  Static labels: {len(gliner_config.labels.static)}")
        print(f"  Dynamic labels: {len(gliner_config.labels.dynamic)}")
        print(f"  GPU: {use_gpu}")

        # Initialize results
        all_entities = []
        entity_counts: dict[str, int] = {}
        entity_id_counter = 0

        # Process each document
        for doc_metadata in files_scan.documents:
            doc_id = doc_metadata.doc_id
            print(f"\n→ Processing {doc_metadata.file_name} (doc_id={doc_id})...")

            # Load canonical text
            text = self._load_document_text(case_id, doc_id, doc_metadata.file_name)
            if not text:
                print(f"  Warning: No text found for {doc_id}, skipping")
                continue

            print(f"  Text length: {len(text)} characters")

            # Sentence spans for this document, so per-entity context can be
            # anchored to whole sentences rather than a fixed character window.
            sentence_spans = split_sentences(text)

            # Segment text for batch processing
            segments = self._segment_text(text, max_length=1000, overlap=100)
            print(f"  Created {len(segments)} segments")

            # Run GLiNER on segments
            print(f"  Running GLiNER...")

            with GlinerRunner(
                gliner_config.model,
                threshold=gliner_config.threshold,
            ) as gliner:
                # Combine all labels
                all_labels = gliner_config.labels.static + gliner_config.labels.dynamic

                # Process in batches
                segment_texts = [seg["text"] for seg in segments]

                predictions = gliner.predict_batch(
                    segment_texts,
                    all_labels,
                    threshold=gliner_config.threshold
                )

                # Process predictions
                for seg_idx, seg_preds in enumerate(predictions):
                    segment = segments[seg_idx]
                    seg_offset = segment["offset"]

                    for pred in seg_preds:
                        # Calculate absolute character positions
                        char_start = seg_offset + pred.start
                        char_end = seg_offset + pred.end

                        # Verify span matches (sanity check)
                        actual_text = text[char_start:char_end]
                        if actual_text != pred.text:
                            # Try to find the correct position
                            char_start, char_end = self._find_span_position(
                                text, pred.text, char_start, char_end
                            )
                            if char_start == -1:
                                print(f"  Warning: Could not locate span '{pred.text}', skipping")
                                continue

                        # Create entity
                        entity_id = f"ent_{entity_id_counter:04d}"
                        entity_id_counter += 1

                        # Link to actor if possible
                        linked_actor = self._link_to_actor(pred.text, pred.label, gliner_config)

                        # Extract context: at least N sentences before and
                        # after the entity, not a fixed character window.
                        context = self._extract_sentence_context(
                            text,
                            char_start,
                            char_end,
                            pred.text,
                            sentence_spans,
                            sentences_before=context_sentences_before,
                            sentences_after=context_sentences_after,
                        )

                        entity = Entity(
                            entity_id=entity_id,
                            text=pred.text,
                            label=pred.label,
                            score=pred.score,
                            doc_id=doc_id,
                            char_start=char_start,
                            char_end=char_end,
                            linked_actor=linked_actor,
                            context=context,
                        )

                        all_entities.append(entity)

                        # Update counts
                        entity_counts[pred.label] = entity_counts.get(pred.label, 0) + 1

            gliner_entity_count = len([e for e in all_entities if e.doc_id == doc_id])
            print(f"  ✓ GLiNER extracted {gliner_entity_count} entities")

            # Gazetteer pass: catch exact named-actor mentions GLiNER's
            # threshold missed. Runs after GLiNER so it knows which spans
            # are already covered and only adds what's missing.
            if enable_gazetteer:
                doc_gliner_spans = [
                    (e.char_start, e.char_end) for e in all_entities if e.doc_id == doc_id
                ]
                gazetteer_entities, entity_id_counter = self._gazetteer_entities(
                    text,
                    doc_id,
                    gliner_config,
                    doc_gliner_spans,
                    sentence_spans,
                    entity_id_counter,
                    context_sentences_before=context_sentences_before,
                    context_sentences_after=context_sentences_after,
                    min_term_length=gazetteer_min_term_length,
                )
                if gazetteer_entities:
                    print(f"  ✓ Gazetteer pass added {len(gazetteer_entities)} entities GLiNER missed")
                all_entities.extend(gazetteer_entities)
                for gazetteer_entity in gazetteer_entities:
                    entity_counts[gazetteer_entity.label] = entity_counts.get(gazetteer_entity.label, 0) + 1

        print(f"\n→ Total entities extracted: {len(all_entities)}")
        print(f"→ Entity breakdown:")
        for label, count in sorted(entity_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"  {label}: {count}")

        # Create entities artifact
        entities_artifact = EntitiesArtifact(
            case_id=case_id,
            extraction_timestamp=datetime.now(),
            gliner_config=ModelConfig(
                model=gliner_config.model,
                threshold=gliner_config.threshold,
            ),
            entities=all_entities,
            entity_counts=entity_counts,
        )

        self.save_artifact(case_id, "entities.json", entities_artifact)

        print(f"\n{'='*60}")
        print(f"Stage 2 Complete!")
        print(f"{'='*60}\n")

    def validate_inputs(self, case_id: str) -> bool:
        """Check if Stage 1 outputs are available.

        Args:
            case_id: Case identifier

        Returns:
            True if required inputs exist
        """
        required_files = ["files_scan.json", "gliner_config.json"]

        for filename in required_files:
            if not self.artifact_exists(case_id, filename):
                print(f"Error: Required artifact not found: {filename}")
                print(f"       Run Stage 1 first!")
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
        return [events_dir / "entities.json"]

    def _load_document_text(self, case_id: str, doc_id: str, file_name: str) -> str:
        """Load canonical text for a document.

        Args:
            case_id: Case identifier
            doc_id: Document ID
            file_name: Original file name

        Returns:
            Document text
        """
        # Try canonical text from existing pipeline
        text_path = self.get_documents_dir(case_id) / f"{doc_id}.txt"
        if text_path.exists():
            return self.load_text(text_path)

        # Try to load from parsed files, alongside the PDF in documents/
        pdf_path = self.get_documents_dir(case_id) / file_name
        parsed_path = pdf_path.with_suffix(".json")

        if parsed_path.exists():
            try:
                parsed_data = self.load_json(parsed_path)
                if "raw_text" in parsed_data:
                    return parsed_data["raw_text"]
                if "paragraphs" in parsed_data:
                    return "\n\n".join(parsed_data["paragraphs"])
            except Exception as e:
                print(f"  Warning: Failed to load parsed JSON: {e}")

        # Try Docling JSON, saved under docling/documents/ - see
        # lawsuit_parser.parsers.batch.get_docling_dir.
        docling_path = get_docling_dir(pdf_path) / f"{pdf_path.stem}.docling.json"
        if docling_path.exists():
            try:
                docling_data = self.load_json(docling_path)
                if "texts" in docling_data:
                    texts = [item.get("text", "") for item in docling_data["texts"]]
                    return "\n".join(texts)
            except Exception as e:
                print(f"  Warning: Failed to load Docling JSON: {e}")

        return ""

    def _segment_text(self, text: str, max_length: int = 1000, overlap: int = 100) -> list[dict[str, Any]]:
        """Segment text into overlapping chunks for GLiNER processing.

        Args:
            text: Full document text
            max_length: Maximum segment length
            overlap: Overlap between segments

        Returns:
            List of segment dictionaries with text and offset
        """
        segments = []

        # Split by paragraphs first
        paragraphs = re.split(r'\n\n+', text)

        current_segment = ""
        current_offset = 0

        for para in paragraphs:
            para_len = len(para)

            # If adding this paragraph would exceed max_length, save current segment
            if current_segment and len(current_segment) + para_len > max_length:
                segments.append({
                    "text": current_segment,
                    "offset": current_offset,
                })

                # Start new segment with overlap
                if overlap > 0 and len(current_segment) > overlap:
                    overlap_text = current_segment[-overlap:]
                    current_offset += len(current_segment) - overlap
                    current_segment = overlap_text + "\n\n" + para
                else:
                    current_offset += len(current_segment) + 2  # +2 for \n\n
                    current_segment = para
            else:
                # Add to current segment
                if current_segment:
                    current_segment += "\n\n" + para
                else:
                    current_segment = para

        # Add final segment
        if current_segment:
            segments.append({
                "text": current_segment,
                "offset": current_offset,
            })

        return segments

    def _find_span_position(self, text: str, span_text: str, hint_start: int, hint_end: int) -> tuple[int, int]:
        """Find the correct position of a span in text.

        Args:
            text: Full text
            span_text: Span text to find
            hint_start: Suggested start position
            hint_end: Suggested end position

        Returns:
            (start, end) tuple, or (-1, -1) if not found
        """
        # First, check the hint position
        if text[hint_start:hint_end] == span_text:
            return hint_start, hint_end

        # Search near the hint position
        search_window = 200
        search_start = max(0, hint_start - search_window)
        search_end = min(len(text), hint_end + search_window)
        search_text = text[search_start:search_end]

        # Find the span in the search window
        offset = search_text.find(span_text)
        if offset != -1:
            return search_start + offset, search_start + offset + len(span_text)

        # Last resort: search the entire text
        offset = text.find(span_text)
        if offset != -1:
            return offset, offset + len(span_text)

        return -1, -1

    def _extract_sentence_context(
        self,
        text: str,
        char_start: int,
        char_end: int,
        entity_text: str,
        sentence_spans: list[tuple[int, int]],
        sentences_before: int = 2,
        sentences_after: int = 2,
    ) -> str:
        """Build context around an entity: at least `sentences_before` full
        sentences before it and `sentences_after` after (fewer only where a
        document boundary is closer than that), with the entity itself
        marked **entity** for emphasis - same marker convention as before,
        just sentence-anchored instead of a fixed character window.

        Args:
            text: Full document text
            char_start: Entity's start offset in text
            char_end: Entity's end offset in text
            entity_text: Entity's matched text (for the ** marker)
            sentence_spans: Document's sentence (start, end) spans, in order
                (see utils.split_sentences)
            sentences_before: Minimum sentences of context before the entity
            sentences_after: Minimum sentences of context after the entity

        Returns:
            Context text with the entity wrapped in ** markers
        """
        if not sentence_spans:
            # Sentence splitting found nothing usable (e.g. near-empty
            # text) - fall back to a fixed character window.
            context_start = max(0, char_start - 200)
            context_end = min(len(text), char_end + 200)
            context = text[context_start:context_end]
            entity_offset = char_start - context_start
            context = (
                context[:entity_offset] +
                "**" + entity_text + "**" +
                context[entity_offset + len(entity_text):]
            )
            return f"...{context}..."

        starts = [s for s, _ in sentence_spans]
        last_idx = len(sentence_spans) - 1

        # Sentence containing the entity's start, and the one containing
        # its end (usually the same sentence; only differs if the entity
        # text itself straddles a sentence boundary).
        idx_start = max(0, min(bisect.bisect_right(starts, char_start) - 1, last_idx))
        idx_end = max(idx_start, min(bisect.bisect_right(starts, max(char_end - 1, char_start)) - 1, last_idx))

        window_start_idx = max(0, idx_start - sentences_before)
        window_end_idx = min(last_idx, idx_end + sentences_after)

        context_start = sentence_spans[window_start_idx][0]
        context_end = sentence_spans[window_end_idx][1]

        context = text[context_start:context_end]
        entity_offset = char_start - context_start
        context = (
            context[:entity_offset] +
            "**" + entity_text + "**" +
            context[entity_offset + len(entity_text):]
        )
        return context

    def _link_to_actor(self, entity_text: str, label: str, gliner_config: GLiNERConfig) -> str | None:
        """Link an entity to a known actor if possible.

        Args:
            entity_text: Entity text
            label: Entity label
            gliner_config: GLiNER configuration

        Returns:
            Canonical actor name if linked, None otherwise
        """
        # Check if label is a dynamic actor label
        for actor in gliner_config.actors:
            if label == actor.gliner_label:
                return actor.canonical_name

            # Also check if entity matches actor name or aliases
            entity_lower = entity_text.lower()
            if entity_lower == actor.canonical_name.lower():
                return actor.canonical_name

            for alias in actor.aliases:
                if entity_lower == alias.lower():
                    return actor.canonical_name

        return None

    def _gazetteer_entities(
        self,
        text: str,
        doc_id: str,
        gliner_config: GLiNERConfig,
        existing_spans: list[tuple[int, int]],
        sentence_spans: list[tuple[int, int]],
        entity_id_counter: int,
        context_sentences_before: int = 2,
        context_sentences_after: int = 2,
        min_term_length: int = 3,
    ) -> tuple[list[Entity], int]:
        """Regex gazetteer pass: exact mentions of named actors GLiNER missed.

        GLiNER is threshold-based and can miss a known actor's name/alias
        even though it appears verbatim in the text. This is a deterministic
        recall backstop for that case - not a replacement for GLiNER, which
        remains the only source for generic/unnamed labels (temporal
        expression, monetary amount, an unidentified witness) that have no
        fixed string to search for.

        Named actors' canonical name and aliases are matched longest-first
        (case-insensitive, word-boundary) so e.g. "GOLDWELL NEW YORK" is
        matched whole rather than also spawning a nested "GOLDWELL" hit, and
        any match overlapping an already-detected span (from GLiNER or from
        a longer gazetteer match already accepted) is skipped.

        Args:
            text: Full document text
            doc_id: Document ID
            gliner_config: Stage 1's actor roster + labels
            existing_spans: (char_start, char_end) of already-detected
                entities in this document (from GLiNER)
            sentence_spans: Document's sentence spans, for context extraction
            entity_id_counter: Next entity_id sequence number to assign
            context_sentences_before: Sentences of context before each entity
            context_sentences_after: Sentences of context after each entity
            min_term_length: Skip names/aliases shorter than this, to cut
                noise from short acronyms/initials

        Returns:
            (new gazetteer-derived entities, updated entity_id_counter)
        """
        terms: list[tuple[str, Actor]] = []
        seen_terms: set[str] = set()
        for actor in gliner_config.actors:
            if not actor.is_named:
                continue
            for name in [actor.canonical_name, *actor.aliases]:
                name = name.strip()
                key = name.lower()
                if len(name) < min_term_length or key in seen_terms:
                    continue
                seen_terms.add(key)
                terms.append((name, actor))

        # Longest term first, so a longer match "claims" its span before any
        # shorter alias/name that happens to be a substring of it is tried.
        terms.sort(key=lambda t: len(t[0]), reverse=True)

        covered = list(existing_spans)

        def overlaps(start: int, end: int) -> bool:
            return any(start < c_end and end > c_start for c_start, c_end in covered)

        new_entities: list[Entity] = []
        for term, actor in terms:
            pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
            for match in pattern.finditer(text):
                start, end = match.start(), match.end()
                if overlaps(start, end):
                    continue
                covered.append((start, end))

                role_display = actor.role.replace("_", " ")
                label = actor.gliner_label or f"{role_display} ({actor.canonical_name})"

                context = self._extract_sentence_context(
                    text,
                    start,
                    end,
                    match.group(0),
                    sentence_spans,
                    sentences_before=context_sentences_before,
                    sentences_after=context_sentences_after,
                )

                new_entities.append(Entity(
                    entity_id=f"ent_{entity_id_counter:04d}",
                    text=match.group(0),
                    label=label,
                    score=1.0,
                    doc_id=doc_id,
                    char_start=start,
                    char_end=end,
                    linked_actor=actor.canonical_name,
                    context=context,
                    detection_method="gazetteer",
                ))
                entity_id_counter += 1

        return new_entities, entity_id_counter
