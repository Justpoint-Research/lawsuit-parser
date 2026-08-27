"""Base classes and protocols for event extraction pipeline stages."""

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..parsers.batch import get_docling_dir

logger = logging.getLogger(__name__)

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

    def __init__(self, data_root: Path, output_root: Path | None = None):
        """Initialize stage with data root and output root directories.

        Args:
            data_root: Root directory for source case data (e.g., data/cases) -
                documents, confirmations, and Docling outputs are read from here
            output_root: Root directory for pipeline-generated artifacts (e.g.,
                data/extraction). Kept separate from data_root so a run's
                events/stages outputs can be wiped and regenerated without
                touching source data. Defaults to data_root if not given.
        """
        self.data_root = Path(data_root)
        self.output_root = Path(output_root) if output_root is not None else self.data_root

    def get_case_dir(self, case_id: str) -> Path:
        """Get the source case directory path (documents, confirmations, docling)."""
        return self.data_root / case_id

    def get_output_case_dir(self, case_id: str) -> Path:
        """Get the case directory under output_root for generated artifacts."""
        output_case_dir = self.output_root / case_id
        output_case_dir.mkdir(parents=True, exist_ok=True)
        return output_case_dir

    def get_events_dir(self, case_id: str) -> Path:
        """Get the events directory for a case."""
        events_dir = self.get_output_case_dir(case_id) / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        return events_dir

    def get_documents_dir(self, case_id: str) -> Path:
        """Get the documents directory for a case."""
        return self.get_case_dir(case_id) / "documents"

    def get_confirmations_dir(self, case_id: str) -> Path:
        """Get the confirmations directory for a case.

        Confirmations are the e-filing acknowledgement notices - same file
        names as their counterparts in documents/, different content (who
        filed what, with whom, and when) - see lawsuit_parser.parsers.batch.
        """
        return self.get_case_dir(case_id) / "confirmations"

    def get_stages_dir(self, case_id: str) -> Path:
        """Get the stages directory for existing pipeline artifacts."""
        return self.get_output_case_dir(case_id) / "stages"

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

        logger.info(f"✓ Saved {filename} to {output_path}")
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

    def load_document_text(self, case_id: str, doc_id: str, file_name: str) -> str:
        """Load canonical text for a document: a cached <doc_id>.txt if one
        exists, else a legacy parsed JSON sidecar next to the PDF, else
        Docling's own parsed JSON. Shared by every stage that needs a
        document's full text (Stage 2/3/4) - previously three near-identical
        private copies of this same lookup.

        Args:
            case_id: Case identifier
            doc_id: Document ID
            file_name: Original file name

        Returns:
            Document text, or "" if none of the sources are available
        """
        text_path = self.get_documents_dir(case_id) / f"{doc_id}.txt"
        if text_path.exists():
            return self.load_text(text_path)

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
                logger.warning(f"  Warning: Failed to load parsed JSON: {e}")

        docling_path = get_docling_dir(pdf_path) / f"{pdf_path.stem}.docling.json"
        if docling_path.exists():
            try:
                docling_data = self.load_json(docling_path)
                if "texts" in docling_data:
                    texts = [item.get("text", "") for item in docling_data["texts"]]
                    return "\n".join(texts)
            except Exception as e:
                logger.warning(f"  Warning: Failed to load Docling JSON: {e}")

        return ""

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
