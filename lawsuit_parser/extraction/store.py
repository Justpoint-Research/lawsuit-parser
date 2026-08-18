"""Artifact storage layer for the extraction pipeline.

Artifacts are stored on disk as JSON files, one per stage per case.
This is the poor man's provenance layer and makes tuning tractable.
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar, Type

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ArtifactStore:
    """Manages storage of extraction artifacts for a single case."""

    def __init__(self, case_id: str, root: Path):
        """Initialize artifact store for a case.

        Args:
            case_id: The case identifier
            root: Root directory for all cases (typically data/cases)
        """
        self.case_id = case_id
        self.root = Path(root)
        self.case_dir = self.root / case_id
        self.documents_dir = self.case_dir / "documents"
        self.stages_dir = self.case_dir / "stages"
        self.errors_dir = self.case_dir / "errors"

        # Create directories if they don't exist
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.stages_dir.mkdir(parents=True, exist_ok=True)
        self.errors_dir.mkdir(parents=True, exist_ok=True)

    def write_stage(self, stage: str, model: BaseModel) -> None:
        """Write a stage artifact to disk.

        Args:
            stage: Stage identifier (e.g., "00_segments", "01_metadata")
            model: Pydantic model to serialize

        The artifact is written with indent=2 and sorted keys for human readability
        and clean diffs between runs.
        """
        artifact_path = self.stages_dir / f"{stage}.json"
        data = model.model_dump(mode="json")

        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)

    def read_stage(self, stage: str, model_cls: Type[T]) -> T:
        """Read a stage artifact from disk.

        Args:
            stage: Stage identifier (e.g., "00_segments", "01_metadata")
            model_cls: Pydantic model class to deserialize into

        Returns:
            Instance of model_cls

        Raises:
            FileNotFoundError: If the artifact doesn't exist
            ValidationError: If the artifact doesn't match the schema
        """
        artifact_path = self.stages_dir / f"{stage}.json"

        with open(artifact_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return model_cls.model_validate(data)

    def has_stage(self, stage: str) -> bool:
        """Check if a stage artifact exists.

        Args:
            stage: Stage identifier (e.g., "00_segments", "01_metadata")

        Returns:
            True if the artifact exists
        """
        artifact_path = self.stages_dir / f"{stage}.json"
        return artifact_path.exists()

    def write_canonical_text(self, doc_id: str, text: str) -> None:
        """Write canonical text for a document.

        Args:
            doc_id: Document identifier
            text: The canonical text

        This text becomes the single source of truth for all offsets.
        It is written once in stage 0 and never modified.
        """
        text_path = self.documents_dir / f"{doc_id}.txt"

        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text)

    def read_canonical_text(self, doc_id: str) -> str:
        """Read canonical text for a document.

        Args:
            doc_id: Document identifier

        Returns:
            The canonical text

        Raises:
            FileNotFoundError: If the canonical text doesn't exist
        """
        text_path = self.documents_dir / f"{doc_id}.txt"

        with open(text_path, "r", encoding="utf-8") as f:
            return f.read()

    def has_canonical_text(self, doc_id: str) -> bool:
        """Check if canonical text exists for a document.

        Args:
            doc_id: Document identifier

        Returns:
            True if the canonical text exists
        """
        text_path = self.documents_dir / f"{doc_id}.txt"
        return text_path.exists()

    def read_case_caption(self) -> str | None:
        """Read the short docket caption (e.g. "X v. Y") from the case's
        DB-exported JSON (written by scripts/export_case.py and friends).

        This is case-level metadata from the scraping database, not a stage
        artifact - it's the one place the extraction pipeline can reach
        DB-known plaintiff/defendant identity before running any model.

        Returns:
            The caption string, or None if the case JSON or caption is missing.
        """
        case_json_path = self.case_dir / f"{self.case_id}.json"
        if not case_json_path.exists():
            return None

        with open(case_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data.get("case_info", {}).get("caption")

    def write_run_metadata(
        self,
        stage: str,
        config_hash: str,
        model_info: dict[str, str],
        counters: dict[str, int | float | str | list],
    ) -> None:
        """Write or update run metadata for a stage.

        Args:
            stage: Stage identifier
            config_hash: Hash of the configuration used
            model_info: Model identifiers and versions
            counters: Stage-specific counters and metrics

        This records provenance information for reproducibility and tuning.
        """
        run_path = self.case_dir / "run.json"

        # Load existing run metadata if it exists
        if run_path.exists():
            with open(run_path, "r", encoding="utf-8") as f:
                run_data = json.load(f)
        else:
            run_data = {"case_id": self.case_id, "stages": {}}

        # Update stage metadata
        run_data["stages"][stage] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config_hash": config_hash,
            "models": model_info,
            "counters": counters,
        }

        # Write back
        with open(run_path, "w", encoding="utf-8") as f:
            json.dump(run_data, f, indent=2, sort_keys=True, ensure_ascii=False)

    def read_run_metadata(self) -> dict:
        """Read run metadata.

        Returns:
            Run metadata dictionary

        Raises:
            FileNotFoundError: If run metadata doesn't exist
        """
        run_path = self.case_dir / "run.json"

        with open(run_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def log_error(self, stage: str, error_type: str, data: dict | str) -> None:
        """Log an error to the errors directory.

        Args:
            stage: Stage identifier
            error_type: Type of error (e.g., "schema_validation", "extraction_failure")
            data: Error data (will be JSON serialized if dict)

        Errors are logged with timestamps to help debug issues during tuning.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        error_file = self.errors_dir / f"{stage}_{error_type}_{timestamp}.json"

        error_data = {
            "stage": stage,
            "error_type": error_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }

        with open(error_file, "w", encoding="utf-8") as f:
            json.dump(error_data, f, indent=2, ensure_ascii=False)


def compute_config_hash(config: dict) -> str:
    """Compute a stable hash of a configuration dictionary.

    Args:
        config: Configuration dictionary

    Returns:
        SHA256 hex digest (first 16 characters)
    """
    # Sort keys for stability
    config_str = json.dumps(config, sort_keys=True)
    return hashlib.sha256(config_str.encode()).hexdigest()[:16]
