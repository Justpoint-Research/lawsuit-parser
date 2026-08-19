#!/usr/bin/env python3
"""Extract events from lawsuit documents.

Usage:
    uv run scripts/extract.py \
        --case-id 1-19-cv-01234 \
        --stages 0-4 \
        [--force] \
        [--config config/extraction.toml]
"""

import argparse
import logging
import sys
import tomli
from pathlib import Path

# Add parent directory to path to import lawsuit_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.extraction.store import ArtifactStore, compute_config_hash
from lawsuit_parser.extraction.schemas import SegmentsArtifact, MetadataArtifact, RegistryArtifact, SpansArtifact, ProtoEventsArtifact
from lawsuit_parser.extraction.segments import build_segments
from lawsuit_parser.extraction.metadata import extract_metadata
from lawsuit_parser.extraction.registry import build_registry
from lawsuit_parser.extraction.spans import sweep_spans, build_dynamic_labels
from lawsuit_parser.extraction.protoevents import build_proto_events
from lawsuit_parser.extraction.models import (
    NuExtractClient,
    GlinerRunner,
    CorefRunner,
    DisabledRelexRunner,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_stages(stages_str: str) -> list[int]:
    """Parse stage specification.

    Examples:
        "0-4" -> [0, 1, 2, 3, 4]
        "2" -> [2]
        "1,3" -> [1, 3]

    Args:
        stages_str: Stage specification

    Returns:
        List of stage numbers
    """
    if "-" in stages_str:
        start, end = stages_str.split("-")
        return list(range(int(start), int(end) + 1))
    elif "," in stages_str:
        return [int(s.strip()) for s in stages_str.split(",")]
    else:
        return [int(stages_str)]


def load_config(config_path: Path) -> dict:
    """Load configuration from TOML file.

    Args:
        config_path: Path to config file

    Returns:
        Configuration dictionary
    """
    with open(config_path, "rb") as f:
        return tomli.load(f)


def load_docling_documents(case_id: str, data_root: Path) -> list:
    """Load Docling documents for a case.

    Args:
        case_id: Case identifier
        data_root: Root data directory

    Returns:
        List of Docling Document objects
    """
    from docling_core.types.doc import DoclingDocument

    case_dir = data_root / case_id

    # Try documents/ subdirectory first (parse_all_pdfs.py output structure)
    documents_dir = case_dir / "documents"
    if documents_dir.exists():
        docling_files = sorted(documents_dir.glob("*.docling.json"))
    else:
        # Fall back to flat structure (legacy)
        docling_files = sorted(case_dir.glob("*.docling.json"))

    if not docling_files:
        raise FileNotFoundError(
            f"No .docling.json files found in {case_dir} or {documents_dir}. "
            f"Run the parser first to generate Docling documents."
        )

    documents = []
    for docling_file in docling_files:
        logger.info(f"Loading {docling_file}")
        doc = DoclingDocument.model_validate_json(docling_file.read_text())
        documents.append(doc)

    return documents


def validate_all_spans(store: ArtifactStore, stages_run: list[int]) -> bool:
    """Validate that all spans are valid across all artifacts.

    This is the global span validity assertion.

    Args:
        store: Artifact store
        stages_run: List of stages that were run

    Returns:
        True if all spans are valid, False otherwise
    """
    logger.info("Running global span validity assertion...")

    errors = []

    # Stage 0: segments
    if 0 in stages_run:
        segments = store.read_stage("00_segments", SegmentsArtifact)
        for segment in segments.segments:
            canonical_text = store.read_canonical_text(segment.doc_id)
            extracted = canonical_text[segment.char_start : segment.char_end]
            if not extracted:
                errors.append(
                    f"Empty segment {segment.seg_id} at "
                    f"[{segment.char_start}:{segment.char_end}]"
                )

    # Stage 1: metadata spans
    if 1 in stages_run:
        metadata = store.read_stage("01_metadata", MetadataArtifact)
        for doc_meta in metadata.documents:
            canonical_text = store.read_canonical_text(doc_meta.doc_id)
            for field, span in doc_meta.source_spans.items():
                extracted = canonical_text[span.char_start : span.char_end]
                if extracted != span.text:
                    errors.append(
                        f"Span mismatch in {doc_meta.doc_id}/{field}: "
                        f"canonical[{span.char_start}:{span.char_end}]='{extracted}' "
                        f"!= span.text='{span.text}'"
                    )

    # Stage 2: registry mentions
    if 2 in stages_run:
        registry = store.read_stage("02_registry", RegistryArtifact)
        for mention in registry.mentions:
            canonical_text = store.read_canonical_text(mention.span.doc_id)
            extracted = canonical_text[
                mention.span.char_start : mention.span.char_end
            ]
            if extracted != mention.span.text:
                errors.append(
                    f"Span mismatch in mention: "
                    f"canonical[{mention.span.char_start}:{mention.span.char_end}]='{extracted}' "
                    f"!= span.text='{mention.span.text}'"
                )

        for span in registry.unresolved:
            canonical_text = store.read_canonical_text(span.doc_id)
            extracted = canonical_text[span.char_start : span.char_end]
            if extracted != span.text:
                errors.append(
                    f"Span mismatch in unresolved: "
                    f"canonical[{span.char_start}:{span.char_end}]='{extracted}' "
                    f"!= span.text='{span.text}'"
                )

    # Stage 3: GLiNER spans
    if 3 in stages_run:
        spans_artifact = store.read_stage("03_spans", SpansArtifact)
        for gliner_span in spans_artifact.spans:
            canonical_text = store.read_canonical_text(gliner_span.span.doc_id)
            extracted = canonical_text[
                gliner_span.span.char_start : gliner_span.span.char_end
            ]
            if extracted != gliner_span.span.text:
                errors.append(
                    f"Span mismatch in GLiNER span: "
                    f"canonical[{gliner_span.span.char_start}:{gliner_span.span.char_end}]='{extracted}' "
                    f"!= span.text='{gliner_span.span.text}'"
                )

    # Stage 4: proto-event spans
    if 4 in stages_run:
        proto_events = store.read_stage("04_protoevents", ProtoEventsArtifact)
        for proto_event in proto_events.proto_events:
            canonical_text = store.read_canonical_text(proto_event.predicate.doc_id)
            extracted = canonical_text[
                proto_event.predicate.char_start : proto_event.predicate.char_end
            ]
            if extracted != proto_event.predicate.text:
                errors.append(
                    f"Span mismatch in proto-event predicate: "
                    f"canonical[{proto_event.predicate.char_start}:{proto_event.predicate.char_end}]='{extracted}' "
                    f"!= span.text='{proto_event.predicate.text}'"
                )

            for edge in proto_event.edges:
                extracted = canonical_text[
                    edge.target.char_start : edge.target.char_end
                ]
                if extracted != edge.target.text:
                    errors.append(
                        f"Span mismatch in proto-event edge: "
                        f"canonical[{edge.target.char_start}:{edge.target.char_end}]='{extracted}' "
                        f"!= span.text='{edge.target.text}'"
                    )

    if errors:
        logger.error(f"Global span validity check FAILED with {len(errors)} errors:")
        for error in errors[:10]:  # Show first 10
            logger.error(f"  {error}")
        if len(errors) > 10:
            logger.error(f"  ... and {len(errors) - 10} more errors")
        return False
    else:
        logger.info("Global span validity check PASSED")
        return True


def main():
    parser = argparse.ArgumentParser(description="Extract events from lawsuit documents")
    parser.add_argument("--case-id", required=True, help="Case identifier")
    parser.add_argument(
        "--stages", default="0-4", help="Stages to run (e.g., '0-4', '2', '1,3')"
    )
    parser.add_argument(
        "--force", action="store_true", help="Force re-run of existing stages"
    )
    parser.add_argument(
        "--config",
        default="config/extraction.toml",
        help="Path to configuration file",
    )

    args = parser.parse_args()

    # Load configuration
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    config = load_config(config_path)
    config_hash = compute_config_hash(config)

    # Parse stages
    stages_to_run = parse_stages(args.stages)
    logger.info(f"Running stages: {stages_to_run}")

    # Initialize store
    data_root = Path(config["paths"]["data_root"])
    store = ArtifactStore(args.case_id, data_root)

    # Track which stages actually ran
    stages_run = []

    # Stage 0: Segmentation
    if 0 in stages_to_run:
        if not args.force and store.has_stage("00_segments"):
            logger.info("Stage 0: Skipping (already exists, use --force to re-run)")
        else:
            logger.info("Stage 0: Building segments...")
            docling_docs = load_docling_documents(args.case_id, data_root)
            artifact = build_segments(args.case_id, docling_docs, store)
            store.write_stage("00_segments", artifact)
            store.write_run_metadata("00_segments", config_hash, {}, {})
            logger.info(f"Stage 0 complete: {len(artifact.segments)} segments")
            stages_run.append(0)

    # Stage 1: Metadata
    if 1 in stages_to_run:
        if not args.force and store.has_stage("01_metadata"):
            logger.info("Stage 1: Skipping (already exists, use --force to re-run)")
        else:
            logger.info("Stage 1: Extracting metadata...")
            segments = store.read_stage("00_segments", SegmentsArtifact)
            client = NuExtractClient(
                base_url=config["nuextract"]["base_url"],
                model=config["nuextract"]["model"],
                temperature=config["nuextract"]["temperature"],
                max_retries=config["nuextract"]["max_retries"],
                timeout_s=config["nuextract"]["timeout_s"],
            )
            artifact = extract_metadata(args.case_id, segments, client, store)
            store.write_stage("01_metadata", artifact)
            store.write_run_metadata(
                "01_metadata",
                config_hash,
                {"nuextract": config["nuextract"]["model"]},
                {},
            )
            logger.info(f"Stage 1 complete: {len(artifact.documents)} documents")
            stages_run.append(1)

    # Stage 2: Registry
    if 2 in stages_to_run:
        if not args.force and store.has_stage("02_registry"):
            logger.info("Stage 2: Skipping (already exists, use --force to re-run)")
        else:
            logger.info("Stage 2: Building party registry...")
            segments = store.read_stage("00_segments", SegmentsArtifact)
            metadata = store.read_stage("01_metadata", MetadataArtifact)
            client = NuExtractClient(
                base_url=config["nuextract"]["base_url"],
                model=config["nuextract"]["model"],
                temperature=config["nuextract"]["temperature"],
                max_retries=config["nuextract"]["max_retries"],
                timeout_s=config["nuextract"]["timeout_s"],
            )

            with CorefRunner(config["maverick"]["model"]) as coref:
                artifact = build_registry(
                    args.case_id,
                    segments,
                    metadata,
                    client,
                    coref,
                    store,
                    config["registry"]["fuzzy_match_threshold"],
                )

            store.write_stage("02_registry", artifact)
            store.write_run_metadata(
                "02_registry",
                config_hash,
                {
                    "nuextract": config["nuextract"]["model"],
                    "maverick": config["maverick"]["model"],
                },
                {},
            )
            logger.info(
                f"Stage 2 complete: {len(artifact.parties)} parties, "
                f"{len(artifact.mentions)} mentions"
            )
            stages_run.append(2)

    # Stage 3: Spans
    if 3 in stages_to_run:
        if not args.force and store.has_stage("03_spans"):
            logger.info("Stage 3: Skipping (already exists, use --force to re-run)")
        else:
            logger.info("Stage 3: Sweeping spans with GLiNER...")
            segments = store.read_stage("00_segments", SegmentsArtifact)

            case_caption = store.read_case_caption()
            dynamic_labels = build_dynamic_labels(config["gliner"]["labels"], case_caption)
            extra_label_passes = [dynamic_labels] if dynamic_labels else None
            if dynamic_labels:
                logger.info(
                    f"Stage 3: running extra GLiNER pass with case-specific labels: "
                    f"{dynamic_labels}"
                )

            with GlinerRunner(
                config["gliner"]["model"], config["gliner"]["threshold"]
            ) as gliner:
                artifact = sweep_spans(
                    args.case_id,
                    segments,
                    gliner,
                    store,
                    config["gliner"]["labels"],
                    config["gliner"]["threshold"],
                    config["gliner"]["batch_size"],
                    extra_label_passes=extra_label_passes,
                )

            store.write_stage("03_spans", artifact)
            store.write_run_metadata(
                "03_spans",
                config_hash,
                {"gliner": config["gliner"]["model"]},
                {},
            )
            logger.info(f"Stage 3 complete: {len(artifact.spans)} spans")
            stages_run.append(3)

    # Stage 4: Proto-events
    if 4 in stages_to_run:
        if not args.force and store.has_stage("04_protoevents"):
            logger.info("Stage 4: Skipping (already exists, use --force to re-run)")
        else:
            logger.info("Stage 4: Building proto-events...")
            segments = store.read_stage("00_segments", SegmentsArtifact)
            spans = store.read_stage("03_spans", SpansArtifact)
            registry = store.read_stage("02_registry", RegistryArtifact)

            # Use disabled runner for now (Relex not yet installed)
            relex = DisabledRelexRunner()

            artifact = build_proto_events(
                args.case_id,
                segments,
                spans,
                registry,
                relex,
                store,
                enabled=config["relex"]["enabled"],
                relations=config["relex"]["relations"],
            )

            store.write_stage("04_protoevents", artifact)
            store.write_run_metadata(
                "04_protoevents",
                config_hash,
                {"relex": "disabled"},
                {},
            )
            logger.info(
                f"Stage 4 complete: {len(artifact.priority_segments)} priority segments, "
                f"{len(artifact.proto_events)} proto-events"
            )
            stages_run.append(4)

    # Global validation
    logger.info("\n" + "=" * 60)
    logger.info("Running global validation...")
    logger.info("=" * 60)

    if stages_run:
        valid = validate_all_spans(store, stages_run)
        if not valid:
            logger.error("Validation FAILED")
            sys.exit(1)
        else:
            logger.info("Validation PASSED")

    logger.info("\n" + "=" * 60)
    logger.info("Extraction complete!")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
