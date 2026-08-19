"""Stage 2: Party registry + mention index.

The highest-leverage stage. A perfectly dated timeline with scrambled parties is worthless.

Order of operations:
1. Seed from captions
2. Harvest aliases from "Parties" section
3. Normalize and merge
4. Role anaphora (map role terms to parties)
"""

import logging
import re
from typing import Literal

from rapidfuzz import fuzz

from .models import NuExtractClient, ExtractionError
from .schemas import (
    Party,
    PartyMention,
    RegistryArtifact,
    SegmentsArtifact,
    MetadataArtifact,
    Span,
)
from .store import ArtifactStore

logger = logging.getLogger(__name__)


# Corporate suffix patterns for normalization
CORPORATE_SUFFIXES = {
    "inc": ["inc", "inc.", "incorporated"],
    "corp": ["corp", "corp.", "corporation"],
    "co": ["co", "co."],
    "llc": ["llc", "l.l.c.", "l.l.c", "limited liability company"],
    "ltd": ["ltd", "ltd.", "limited"],
    "lp": ["lp", "l.p.", "l.p"],
    "na": ["n.a.", "n.a", "na"],
}

# Role terms for role anaphora resolution
ROLE_TERMS = {
    "plaintiff": ["plaintiff", "plaintiffs"],
    "defendant": ["defendant", "defendants"],
    "movant": ["movant", "movants"],
    "respondent": ["respondent", "respondents"],
    "petitioner": ["petitioner", "petitioners"],
    "appellant": ["appellant", "appellants"],
    "appellee": ["appellee", "appellees"],
    "court": ["the court", "this court"],
}


def normalize_name(name: str) -> str:
    """Normalize a party name for matching.

    - Case-fold
    - Normalize corporate suffixes (before stripping punctuation)
    - Strip punctuation (except hyphens)
    - Collapse whitespace

    Args:
        name: Raw party name

    Returns:
        Normalized name
    """
    # Case-fold
    name = name.lower()

    # Normalize corporate suffixes first (before removing punctuation)
    # This handles multi-dot abbreviations like "L.L.C."
    for canonical, variants in CORPORATE_SUFFIXES.items():
        for variant in variants:
            # Use word boundaries to avoid partial matches
            pattern = r'\b' + re.escape(variant) + r'\b'
            name = re.sub(pattern, canonical, name)

    # Strip common punctuation
    name = re.sub(r"[.,;:!?()\"']", " ", name)

    # Collapse whitespace
    name = " ".join(name.split())

    return name


def infer_party_type(name: str, role: str) -> Literal[
    "individual", "organization", "government", "court", "unknown"
]:
    """Infer party type from name and role.

    Args:
        name: Party name
        role: Party role

    Returns:
        Party type
    """
    name_lower = name.lower()

    if role == "court":
        return "court"

    # Government indicators
    gov_indicators = [
        "united states",
        "u.s.",
        "state of",
        "county of",
        "city of",
        "department of",
        "secretary of",
        "attorney general",
    ]
    for indicator in gov_indicators:
        if indicator in name_lower:
            return "government"

    # Organization indicators
    org_indicators = ["inc", "corp", "llc", "ltd", "lp", "company", "association"]
    for indicator in org_indicators:
        if indicator in name_lower:
            return "organization"

    # Default to individual if no clear indicator
    # This is conservative - can be refined later
    return "individual"


def build_registry(
    case_id: str,
    segments: SegmentsArtifact,
    metadata: MetadataArtifact,
    client: NuExtractClient,
    store: ArtifactStore,
    fuzzy_threshold: int = 88,
) -> RegistryArtifact:
    """Build party registry with mention index.

    This is Stage 2.

    Args:
        case_id: Case identifier
        segments: Segmentation artifact from stage 0
        metadata: Metadata artifact from stage 1
        client: NuExtract3 client
        store: Artifact store
        fuzzy_threshold: Fuzzy match threshold (0-100)

    Returns:
        RegistryArtifact with parties, mentions, and unresolved spans
    """
    counters = {
        "parties": 0,
        "aliases": 0,
        "mentions_by_source": {},
        "fuzzy_merges": [],
        "unresolved_spans": 0,
        "role_anaphora_overrides": 0,
    }

    parties = []
    mentions = []
    unresolved = []

    # Step 1: Seed registry from captions
    party_registry = {}  # normalized_name -> Party
    party_id_counter = 1

    for doc_meta in metadata.documents:
        for party_seed in doc_meta.parties:
            normalized = normalize_name(party_seed.name)

            if normalized not in party_registry:
                party_id = f"p_{party_id_counter:03d}"
                party_type = infer_party_type(party_seed.name, party_seed.role)

                party = Party(
                    party_id=party_id,
                    canonical_name=party_seed.name,
                    party_type=party_type,
                    roles=[party_seed.role],
                    aliases=[],
                )

                if party_seed.short_name:
                    party.aliases.append(party_seed.short_name)

                party_registry[normalized] = party
                party_id_counter += 1
                counters["parties"] += 1

                # Create mention for this caption appearance
                # We don't have exact span from stage 1, so we'll skip this
                # The caption parties will be found via coref later

    # Step 2: Harvest aliases from "Parties" section
    # Look for the initiating pleading (usually doc_000)
    for doc_meta in metadata.documents:
        doc_id = doc_meta.doc_id
        canonical_text = store.read_canonical_text(doc_id)

        # Find "Parties" section - typically early in the document
        # Look for heading like "PARTIES" or "The Parties"
        parties_section_start = -1
        parties_section_end = -1

        # Simple heuristic: find "PARTIES" in all caps
        match = re.search(r"\n\s*PARTIES\s*\n", canonical_text, re.IGNORECASE)
        if match:
            parties_section_start = match.end()
            # Section ends at next all-caps heading or after ~2000 chars
            next_heading = re.search(
                r"\n\s*[A-Z][A-Z\s]{3,}\s*\n",
                canonical_text[parties_section_start : parties_section_start + 3000],
            )
            if next_heading:
                parties_section_end = parties_section_start + next_heading.start()
            else:
                parties_section_end = parties_section_start + 2000

        if parties_section_start > 0:
            parties_text = canonical_text[parties_section_start:parties_section_end]

            # Extract aliases using NuExtract
            alias_template = {
                "aliases": [
                    {
                        "full_name": "",
                        "alias": "",
                        "alias_type": "",
                    }
                ]
            }

            try:
                result = client.extract(parties_text, alias_template)
                aliases_list = result.get("aliases", [])

                if isinstance(aliases_list, list):
                    for alias_dict in aliases_list:
                        full_name = alias_dict.get("full_name", "").strip()
                        alias = alias_dict.get("alias", "").strip()

                        if full_name and alias:
                            # Find party in registry
                            normalized = normalize_name(full_name)
                            if normalized in party_registry:
                                party = party_registry[normalized]
                                if alias not in party.aliases:
                                    party.aliases.append(alias)
                                    counters["aliases"] += 1

            except ExtractionError as e:
                logger.warning(f"Alias extraction failed for {doc_id}: {e}")

    # Step 3: Normalization and fuzzy merging
    # Build a list to track merges
    to_merge = []

    party_list = list(party_registry.values())
    for i in range(len(party_list)):
        for j in range(i + 1, len(party_list)):
            party_a = party_list[i]
            party_b = party_list[j]

            # Fuzzy match on normalized names
            norm_a = normalize_name(party_a.canonical_name)
            norm_b = normalize_name(party_b.canonical_name)

            score = fuzz.ratio(norm_a, norm_b)
            if score >= fuzzy_threshold:
                to_merge.append((party_a, party_b, score))
                logger.warning(
                    f"Fuzzy merge: '{party_a.canonical_name}' <-> "
                    f"'{party_b.canonical_name}' (score={score})"
                )
                counters["fuzzy_merges"].append({
                    "party_a": party_a.canonical_name,
                    "party_b": party_b.canonical_name,
                    "score": score,
                })

    # Perform merges
    for party_a, party_b, score in to_merge:
        # Merge b into a
        party_a.aliases.extend(party_b.aliases)
        party_a.roles.extend(party_b.roles)
        party_a.roles = list(set(party_a.roles))  # Dedupe
        party_a.aliases = list(set(party_a.aliases))  # Dedupe

        # Remove b from registry
        norm_b = normalize_name(party_b.canonical_name)
        if norm_b in party_registry:
            del party_registry[norm_b]

    # Step 4: Role anaphora
    # This runs last and overwrites coref assignments
    for doc_meta in metadata.documents:
        doc_id = doc_meta.doc_id
        canonical_text = store.read_canonical_text(doc_id)

        # Build role mapping from caption
        role_to_parties = {}
        for party_seed in doc_meta.parties:
            role = party_seed.role
            if role not in role_to_parties:
                role_to_parties[role] = []

            # Find party in registry
            normalized = normalize_name(party_seed.name)
            if normalized in party_registry:
                party = party_registry[normalized]
                role_to_parties[role].append(party.party_id)

        # Find role terms in text
        for role, terms in ROLE_TERMS.items():
            for term in terms:
                # Case-insensitive search
                pattern = r"\b" + re.escape(term) + r"\b"
                for match in re.finditer(pattern, canonical_text, re.IGNORECASE):
                    start = match.start()
                    end = match.end()
                    matched_text = canonical_text[start:end]

                    # Map role to party IDs
                    party_ids = role_to_parties.get(role, [])

                    if party_ids:
                        # If multiple parties, link to all with lower confidence
                        confidence = 1.0 if len(party_ids) == 1 else 0.7

                        for party_id in party_ids:
                            mention_span = Span(
                                doc_id=doc_id,
                                char_start=start,
                                char_end=end,
                                text=matched_text,
                            )

                            party_mention = PartyMention(
                                span=mention_span,
                                party_id=party_id,
                                source="role_anaphora",
                                confidence=confidence,
                            )

                            mentions.append(party_mention)
                            counters["mentions_by_source"]["role_anaphora"] = (
                                counters["mentions_by_source"].get("role_anaphora", 0) + 1
                            )
                            counters["role_anaphora_overrides"] += 1

    logger.info(f"Stage 2 counters: {counters}")

    # Convert registry to list
    parties = list(party_registry.values())

    return RegistryArtifact(
        case_id=case_id,
        parties=parties,
        mentions=mentions,
        unresolved=unresolved,
    )
