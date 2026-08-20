"""Base classes and protocols for event extraction pipeline stages."""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StageProtocol(Protocol):
    """Protocol defining the interface for pipeline stages."""

    stage_number: int
    stage_name: str

    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute the stage for a given case."""
        ...

    def validate_inputs(self, case_id: str) -> bool:
        """Check if all required inputs for this stage are available."""
        ...

    def get_outputs(self, case_id: str) -> list[Path]:
        """Return paths to all outputs produced by this stage."""
        ...


class BaseStage(ABC):
    """Abstract base class for all pipeline stages.

    Provides common functionality for:
    - Path management
    - Reading/writing JSON artifacts
    - Input validation
    - Logging
    """

    stage_number: int
    stage_name: str

    def __init__(self, data_root: Path):
        """Initialize stage with data root directory.

        Args:
            data_root: Root directory for case data (e.g., data/cases)
        """
        self.data_root = Path(data_root)

    def get_case_dir(self, case_id: str) -> Path:
        """Get the case directory path."""
        return self.data_root / case_id

    def get_events_dir(self, case_id: str) -> Path:
        """Get the events directory for a case."""
        events_dir = self.get_case_dir(case_id) / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        return events_dir

    def get_documents_dir(self, case_id: str) -> Path:
        """Get the documents directory for a case."""
        return self.get_case_dir(case_id) / "documents"

    def get_stages_dir(self, case_id: str) -> Path:
        """Get the stages directory for existing pipeline artifacts."""
        return self.get_case_dir(case_id) / "stages"

    def save_artifact(self, case_id: str, filename: str, data: BaseModel) -> Path:
        """Save a Pydantic model as JSON artifact.

        Args:
            case_id: Case identifier
            filename: Output filename (e.g., 'files_scan.json')
            data: Pydantic model to serialize

        Returns:
            Path to saved file
        """
        output_path = self.get_events_dir(case_id) / filename

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(
                data.model_dump(mode="json"),
                f,
                indent=2,
                ensure_ascii=False,
                sort_keys=True
            )

        print(f"✓ Saved {filename} to {output_path}")
        return output_path

    def load_artifact(self, case_id: str, filename: str, model_class: type[T]) -> T:
        """Load a JSON artifact as a Pydantic model.

        Args:
            case_id: Case identifier
            filename: Artifact filename
            model_class: Pydantic model class to deserialize into

        Returns:
            Loaded and validated Pydantic model

        Raises:
            FileNotFoundError: If artifact doesn't exist
            ValueError: If JSON is invalid or fails validation
        """
        artifact_path = self.get_events_dir(case_id) / filename

        if not artifact_path.exists():
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        with open(artifact_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return model_class.model_validate(data)

    def artifact_exists(self, case_id: str, filename: str) -> bool:
        """Check if an artifact exists."""
        return (self.get_events_dir(case_id) / filename).exists()

    def load_json(self, path: Path) -> dict[str, Any]:
        """Load a JSON file as a dictionary.

        Args:
            path: Path to JSON file

        Returns:
            Parsed JSON data

        Raises:
            FileNotFoundError: If file doesn't exist
        """
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_text(self, path: Path) -> str:
        """Load a text file.

        Args:
            path: Path to text file

        Returns:
            File contents as string

        Raises:
            FileNotFoundError: If file doesn't exist
        """
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @abstractmethod
    def run(self, case_id: str, config: dict[str, Any]) -> None:
        """Execute the stage for a given case.

        Args:
            case_id: Case identifier
            config: Stage-specific configuration
        """
        ...

    @abstractmethod
    def validate_inputs(self, case_id: str) -> bool:
        """Check if all required inputs for this stage are available.

        Args:
            case_id: Case identifier

        Returns:
            True if all inputs are available, False otherwise
        """
        ...

    @abstractmethod
    def get_outputs(self, case_id: str) -> list[Path]:
        """Return paths to all outputs produced by this stage.

        Args:
            case_id: Case identifier

        Returns:
            List of output file paths
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} stage={self.stage_number} name={self.stage_name}>"
