"""Event extraction pipeline orchestrator."""

from pathlib import Path
from typing import Any

from .base import BaseStage
from .config import EventExtractionConfig, get_config_dict, load_config
from .stages import STAGES


class EventExtractionPipeline:
    """Main pipeline orchestrator for event extraction.

    Manages stage registration, dependency validation, and execution.
    """

    def __init__(
        self,
        config_path: Path | None = None,
        data_root: Path | None = None,
        output_root: Path | None = None,
    ):
        """Initialize the pipeline.

        Args:
            config_path: Path to configuration file (optional)
            data_root: Root directory for source case data (optional, overrides config)
            output_root: Root directory for pipeline-generated artifacts (optional,
                overrides config). Kept separate from data_root so a run's outputs
                can be wiped and regenerated without touching source data.
        """
        # Load configuration
        self.config = load_config(config_path)

        # Override roots if provided
        if data_root:
            self.config.paths.data_root = str(data_root)
        if output_root:
            self.config.paths.output_root = str(output_root)

        # Initialize stages
        self.stages: dict[int, BaseStage] = {}
        self._register_stages()

    def _register_stages(self) -> None:
        """Register all available stages."""
        data_root = Path(self.config.paths.data_root)
        output_root = Path(self.config.paths.output_root)

        for stage_class in STAGES:
            stage = stage_class(data_root, output_root)
            self.stages[stage.stage_number] = stage

        print(f"Registered {len(self.stages)} stages:")
        for stage_num in sorted(self.stages.keys()):
            stage = self.stages[stage_num]
            print(f"  Stage {stage_num}: {stage.stage_name}")

    def run_stage(self, case_id: str, stage_number: int, force: bool = False) -> bool:
        """Run a specific stage for a case.

        Args:
            case_id: Case identifier
            stage_number: Stage number to run
            force: If True, run even if outputs already exist

        Returns:
            True if stage completed successfully

        Raises:
            ValueError: If stage number is invalid
        """
        if stage_number not in self.stages:
            raise ValueError(f"Invalid stage number: {stage_number}")

        stage = self.stages[stage_number]

        # Check if outputs already exist
        if not force:
            outputs_exist = all(p.exists() for p in stage.get_outputs(case_id))
            if outputs_exist:
                print(f"\nStage {stage_number} outputs already exist. Use --force to re-run.")
                return True

        # Validate inputs
        print(f"\nValidating inputs for Stage {stage_number}...")
        if not stage.validate_inputs(case_id):
            print(f"Stage {stage_number} validation failed!")
            return False

        # Get stage-specific configuration
        try:
            stage_config_key = f"stage_{stage_number}"
            stage_config = get_config_dict(self.config, stage_config_key)
        except ValueError:
            # No specific config for this stage, use empty dict
            stage_config = {}

        # Add global config items that stages might need
        if stage_number == 1:
            # Stage 1 needs to know about stage 2 config for GLiNER labels
            try:
                stage_2_config = get_config_dict(self.config, "stage_2")
                stage_config.update({
                    "model": stage_2_config.get("model"),
                    "threshold": stage_2_config.get("threshold"),
                    "batch_size": stage_2_config.get("batch_size"),
                    "static_labels": stage_2_config.get("static_labels"),
                })
            except ValueError:
                pass

        # Run the stage
        try:
            stage.run(case_id, stage_config)
            return True
        except Exception as e:
            print(f"\n{'='*60}")
            print(f"Stage {stage_number} FAILED: {e}")
            print(f"{'='*60}\n")
            import traceback
            traceback.print_exc()
            return False

    def run_all_stages(self, case_id: str, force: bool = False) -> bool:
        """Run all stages in sequence for a case.

        Args:
            case_id: Case identifier
            force: If True, re-run stages even if outputs exist

        Returns:
            True if all stages completed successfully
        """
        print(f"\n{'='*60}")
        print(f"Event Extraction Pipeline")
        print(f"Case: {case_id}")
        print(f"Data root: {self.config.paths.data_root}")
        print(f"Output root: {self.config.paths.output_root}")
        print(f"{'='*60}\n")

        for stage_num in sorted(self.stages.keys()):
            success = self.run_stage(case_id, stage_num, force=force)
            if not success:
                print(f"\nPipeline stopped due to Stage {stage_num} failure.")
                return False

        print(f"\n{'='*60}")
        print(f"Pipeline Complete!")
        print(f"All stages executed successfully.")
        print(f"{'='*60}\n")
        return True

    def run_stages(
        self,
        case_id: str,
        stages: list[int] | None = None,
        force: bool = False
    ) -> bool:
        """Run specific stages for a case.

        Args:
            case_id: Case identifier
            stages: List of stage numbers to run (None = all stages)
            force: If True, re-run stages even if outputs exist

        Returns:
            True if all requested stages completed successfully
        """
        if stages is None:
            return self.run_all_stages(case_id, force=force)

        print(f"\n{'='*60}")
        print(f"Event Extraction Pipeline")
        print(f"Case: {case_id}")
        print(f"Stages: {stages}")
        print(f"Data root: {self.config.paths.data_root}")
        print(f"Output root: {self.config.paths.output_root}")
        print(f"{'='*60}\n")

        for stage_num in sorted(stages):
            if stage_num not in self.stages:
                print(f"Warning: Invalid stage number {stage_num}, skipping")
                continue

            success = self.run_stage(case_id, stage_num, force=force)
            if not success:
                print(f"\nExecution stopped due to Stage {stage_num} failure.")
                return False

        print(f"\n{'='*60}")
        print(f"Requested Stages Complete!")
        print(f"{'='*60}\n")
        return True

    def get_stage_status(self, case_id: str) -> dict[int, dict[str, Any]]:
        """Get the status of all stages for a case.

        Args:
            case_id: Case identifier

        Returns:
            Dictionary mapping stage number to status info
        """
        status = {}

        for stage_num in sorted(self.stages.keys()):
            stage = self.stages[stage_num]

            # Check which outputs exist
            outputs = stage.get_outputs(case_id)
            outputs_exist = [p.exists() for p in outputs]

            # Check if inputs are valid
            inputs_valid = stage.validate_inputs(case_id)

            status[stage_num] = {
                "name": stage.stage_name,
                "outputs": [str(p) for p in outputs],
                "outputs_exist": outputs_exist,
                "all_outputs_exist": all(outputs_exist),
                "inputs_valid": inputs_valid,
                "can_run": inputs_valid,
            }

        return status

    def print_status(self, case_id: str) -> None:
        """Print pipeline status for a case.

        Args:
            case_id: Case identifier
        """
        print(f"\n{'='*60}")
        print(f"Pipeline Status: {case_id}")
        print(f"{'='*60}\n")

        status = self.get_stage_status(case_id)

        for stage_num in sorted(status.keys()):
            stage_status = status[stage_num]

            # Status symbol
            if stage_status["all_outputs_exist"]:
                symbol = "✓"
                status_text = "COMPLETE"
            elif stage_status["can_run"]:
                symbol = "○"
                status_text = "READY"
            else:
                symbol = "✗"
                status_text = "BLOCKED"

            print(f"{symbol} Stage {stage_num}: {stage_status['name']} [{status_text}]")

            # Show outputs
            for i, (output_path, exists) in enumerate(zip(
                stage_status["outputs"],
                stage_status["outputs_exist"]
            )):
                exists_symbol = "✓" if exists else "✗"
                print(f"    {exists_symbol} {output_path}")

        print()
