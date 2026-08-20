"""Stage 2: GLiNER Entity Detection.

Runs GLiNER to detect entities using the configuration from Stage 1.
Produces entities.json artifact.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ...extraction.models import GlinerRunner
from ..base import BaseStage
from ..models import (
    EntitiesArtifact,
    Entity,
    FilesScan,
    GLiNERConfig,
    ModelConfig,
)


class Stage2GLiNER(BaseStage):
    """Stage 2: Extract entities using GLiNER.

    This stage:
    1. Loads GLiNER configuration from Stage 1
    2. Loads canonical text for each document
    3. Segments text into chunks for batch processing
    4. Runs GLiNER with both static and dynamic labels
    5. Links entities to known actors
    6. Saves entities.json artifact

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

            # Segment text for batch processing
            segments = self._segment_text(text, max_length=1000, overlap=100)
            print(f"  Created {len(segments)} segments")

            # Run GLiNER on segments
            print(f"  Running GLiNER...")

            with GlinerRunner(
                gliner_config.model,
                threshold=gliner_config.threshold,
                use_gpu=use_gpu
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

                        # Extract context (100 chars before and after)
                        context_start = max(0, char_start - 100)
                        context_end = min(len(text), char_end + 100)
                        context = text[context_start:context_end]
                        # Replace the entity with **entity** for emphasis
                        context_offset = char_start - context_start
                        context = (
                            context[:context_offset] +
                            "**" + pred.text + "**" +
                            context[context_offset + len(pred.text):]
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
                            context=f"...{context}...",
                        )

                        all_entities.append(entity)

                        # Update counts
                        entity_counts[pred.label] = entity_counts.get(pred.label, 0) + 1

            print(f"  ✓ Extracted {len([e for e in all_entities if e.doc_id == doc_id])} entities")

        print(f"\n→ Total entities extracted: {len(all_entities)}")
        print(f"→ Entity breakdown:")
        for label, count in sorted(entity_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"  {label}: {count}")

        # Create entities artifact
        entities_artifact = EntitiesArtifact(
            case_id=case_id,
            extraction_timestamp=datetime.now(),
            model_config=ModelConfig(
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

        # Try to load from parsed files
        case_dir = self.get_case_dir(case_id)

        # Try .pdf.json
        pdf_name = file_name.replace(".pdf", "")
        parsed_path = case_dir / f"{pdf_name}.pdf.json"
        if not parsed_path.exists():
            parsed_path = case_dir / f"{pdf_name}.json"

        if parsed_path.exists():
            try:
                parsed_data = self.load_json(parsed_path)
                if "raw_text" in parsed_data:
                    return parsed_data["raw_text"]
                if "paragraphs" in parsed_data:
                    return "\n\n".join(parsed_data["paragraphs"])
            except Exception as e:
                print(f"  Warning: Failed to load parsed JSON: {e}")

        # Try Docling JSON
        docling_path = case_dir / f"{pdf_name}.pdf.docling.json"
        if docling_path.exists():
            try:
                docling_data = self.load_json(docling_path)
                if "main-text" in docling_data:
                    texts = [item.get("text", "") for item in docling_data["main-text"]]
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
