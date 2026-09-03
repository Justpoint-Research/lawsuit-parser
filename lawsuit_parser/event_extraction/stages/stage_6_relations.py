"""Stage 6: Relation Extraction.

Extracts relationships between entities, focusing on lawyer-client
representation relationships. Uses pattern matching on entity contexts
to identify phrases like "representing the plaintiffs/defendants".

Produces relations.json artifact.
"""

import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from tqdm import tqdm

from ..base import BaseStage
from ..models import (
    ActorsArtifact,
    EntitiesArtifact,
    Relation,
    RelationsArtifact,
)

logger = logging.getLogger(__name__)

# Patterns for identifying representation relationships
REPRESENTATION_PATTERNS = [
    # "representing the plaintiffs/defendants"
    (r'\b(?:representing|counsel\s+for|attorney\s+for|lawyer\s+for)\s+(?:the\s+)?(?P<party>plaintiff(?:s)?|defendant(?:s)?)\b', 'represents'),
    # "on behalf of the plaintiffs/defendants"
    (r'\b(?:on\s+behalf\s+of|for)\s+(?:the\s+)?(?P<party>plaintiff(?:s)?|defendant(?:s)?)\b', 'represents'),
    # "plaintiffs' counsel" or "defendants' attorney"
    (r"\b(?P<party>plaintiff(?:s)?|defendant(?:s)?)['\u2019]?\s+(?:counsel|attorney|lawyer|representative)(?:s)?\b", 'represents'),
]


class Stage6Relations(BaseStage):
    """Stage 6: Extract relationships between entities.

    This stage:
    1. Loads entities from Stage 2
    2. Loads actors from Stage 1
    3. Analyzes entity contexts for relationship patterns
    4. Focuses on lawyer-client representation relationships
    5. Saves relations.json artifact

    Outputs:
    - relations.json: All extracted relationships with evidence
    """

    stage_number = 6
    stage_name = "relations"

    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute Stage 6 for a given case.

        Args:
            case_id: Case identifier
            config: Stage-specific configuration
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 6: Relation Extraction - {case_id}")
        logger.info(f"{'='*60}\n")

        # Load Stage 1 and Stage 2 artifacts
        logger.info("→ Loading artifacts...")
        actors_artifact = self.load_artifact(case_id, "actors.json", ActorsArtifact)
        entities_artifact = self.load_artifact(case_id, "entities.json", EntitiesArtifact)

        logger.info(f"  Loaded {len(actors_artifact.actors)} actors")
        logger.info(f"  Loaded {len(entities_artifact.entities)} entities")

        # Build lookup maps
        actor_map = {actor.canonical_name: actor for actor in actors_artifact.actors}

        # Filter entities to only counsel/attorney entities
        counsel_entities = [
            e for e in entities_artifact.entities
            if e.linked_actor and any(
                role in actor_map.get(e.linked_actor, type('', (), {'role': ''})()).role.lower()
                for role in ['counsel', 'attorney', 'lawyer']
            )
        ]

        logger.info(f"  Found {len(counsel_entities)} counsel entities with context")

        # Extract relations
        relations = []
        relation_counts: dict[str, int] = {}
        relation_id_counter = 0

        # Get configuration parameters
        min_confidence = config.get("min_confidence", 0.7)

        logger.info(f"\n→ Extracting representation relationships...")
        pbar = tqdm(counsel_entities, desc="Stage 6: relations", unit="entity", file=sys.__stderr__)

        for entity in pbar:
            if not entity.context:
                continue

            # Try to find representation patterns in the context
            for pattern, relation_type in REPRESENTATION_PATTERNS:
                matches = list(re.finditer(pattern, entity.context, re.IGNORECASE))

                for match in matches:
                    party_text = match.group('party').lower()

                    # Normalize party role
                    if 'plaintiff' in party_text:
                        target_role = 'plaintiff'
                    elif 'defendant' in party_text:
                        target_role = 'defendant'
                    else:
                        continue

                    # Find all actors with the matching role
                    target_actors = [
                        actor for actor in actors_artifact.actors
                        if target_role in actor.role.lower()
                    ]

                    # Get the source actor (the lawyer)
                    source_actor = actor_map.get(entity.linked_actor)
                    if not source_actor:
                        continue

                    # Create a relation for each matching target actor
                    # This handles cases where there are multiple plaintiffs/defendants
                    for target_actor in target_actors:
                        # Calculate confidence based on pattern strength
                        confidence = self._calculate_confidence(pattern, entity.context)

                        if confidence < min_confidence:
                            continue

                        # Extract evidence text (the sentence containing the match)
                        evidence = self._extract_evidence(entity.context, match.start(), match.end())

                        relation = Relation(
                            relation_id=f"rel_{relation_id_counter:04d}",
                            relation_type=relation_type,
                            source_entity=source_actor.canonical_name,
                            source_role=source_actor.role,
                            target_entity=target_actor.canonical_name,
                            target_role=target_actor.role,
                            confidence=confidence,
                            evidence=evidence,
                            doc_id=entity.doc_id,
                            char_start=entity.char_start,
                            char_end=entity.char_end,
                            extraction_method="pattern",
                        )

                        relations.append(relation)
                        relation_counts[relation_type] = relation_counts.get(relation_type, 0) + 1
                        relation_id_counter += 1

        # Deduplicate relations (same source + target + type)
        relations = self._deduplicate_relations(relations)

        logger.info(f"\n→ Total relations extracted: {len(relations)}")
        logger.info(f"→ Relation breakdown:")
        for rel_type, count in sorted(relation_counts.items(), key=lambda x: -x[1]):
            logger.info(f"  {rel_type}: {count}")

        # Create relations artifact
        relations_artifact = RelationsArtifact(
            case_id=case_id,
            extraction_timestamp=datetime.now(),
            relations=relations,
            relation_counts=relation_counts,
        )

        self.save_artifact(case_id, "relations.json", relations_artifact)

        logger.info(f"\n{'='*60}")
        logger.info(f"Stage 6 Complete!")
        logger.info(f"{'='*60}\n")

    def validate_inputs(self, case_id: str) -> bool:
        """Check if Stage 1 and Stage 2 outputs are available.

        Args:
            case_id: Case identifier

        Returns:
            True if required inputs exist
        """
        required_files = ["actors.json", "entities.json"]

        for filename in required_files:
            if not self.artifact_exists(case_id, filename):
                logger.error(f"Error: Required artifact not found: {filename}")
                logger.info(f"       Run Stages 1 and 2 first!")
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
        return [events_dir / "relations.json"]

    def _calculate_confidence(self, pattern: str, context: str) -> float:
        """Calculate confidence score for a relation based on pattern strength.

        Args:
            pattern: The regex pattern that matched
            context: The full context text

        Returns:
            Confidence score between 0.0 and 1.0
        """
        # Stronger patterns get higher confidence
        if 'representing' in pattern:
            return 0.95
        elif 'counsel for' in pattern or 'attorney for' in pattern:
            return 0.90
        elif 'on behalf of' in pattern:
            return 0.85
        else:
            return 0.75

    def _extract_evidence(self, context: str, match_start: int, match_end: int) -> str:
        """Extract the sentence containing the matched pattern as evidence.

        Args:
            context: Full context text
            match_start: Start position of the match in context
            match_end: End position of the match in context

        Returns:
            The sentence containing the match
        """
        # Find sentence boundaries around the match
        # Simple approach: split on periods, question marks, exclamation marks
        sentences = re.split(r'[.!?]+', context)

        current_pos = 0
        for sentence in sentences:
            sentence_start = current_pos
            sentence_end = current_pos + len(sentence)

            if sentence_start <= match_start < sentence_end:
                return sentence.strip()

            current_pos = sentence_end + 1  # +1 for the delimiter

        # Fallback: return a window around the match
        window_start = max(0, match_start - 100)
        window_end = min(len(context), match_end + 100)
        return context[window_start:window_end].strip()

    def _deduplicate_relations(self, relations: list[Relation]) -> list[Relation]:
        """Remove duplicate relations (same source, target, and type).

        Keep the one with highest confidence.

        Args:
            relations: List of relations to deduplicate

        Returns:
            Deduplicated list of relations
        """
        relation_map: dict[tuple[str, str, str], Relation] = {}

        for relation in relations:
            key = (relation.source_entity, relation.target_entity, relation.relation_type)

            if key not in relation_map or relation.confidence > relation_map[key].confidence:
                relation_map[key] = relation

        return list(relation_map.values())
