#!/usr/bin/env python3
"""Run the event extraction pipeline on a case.

This script orchestrates the event extraction pipeline, which extracts
legal events and timelines from parsed Docling documents.

Usage:
    # Run all stages for a case
    python scripts/run_event_extraction.py case_67

    # Run specific stages
    python scripts/run_event_extraction.py case_67 --stages 1 2

    # Check pipeline status
    python scripts/run_event_extraction.py case_67 --status

    # Force re-run (overwrite existing outputs)
    python scripts/run_event_extraction.py case_67 --force
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lawsuit_parser.event_extraction import EventExtractionPipeline


def main():
    parser = argparse.ArgumentParser(
        description="Run event extraction pipeline on a case",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run all stages
  python scripts/run_event_extraction.py case_67

  # Run only Stage 1 (metadata extraction)
  python scripts/run_event_extraction.py case_67 --stages 1

  # Run Stages 1 and 2
  python scripts/run_event_extraction.py case_67 --stages 1 2

  # Check pipeline status
  python scripts/run_event_extraction.py case_67 --status

  # Force re-run all stages
  python scripts/run_event_extraction.py case_67 --force

  # Use custom config file
  python scripts/run_event_extraction.py case_67 --config my_config.toml

  # Use custom data root
  python scripts/run_event_extraction.py case_67 --data-root /path/to/cases
        """
    )

    parser.add_argument(
        "case_id",
        help="Case identifier (e.g., case_67)"
    )

    parser.add_argument(
        "--stages",
        type=int,
        nargs="+",
        help="Specific stages to run (e.g., --stages 1 2). If not provided, runs all stages."
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-run stages even if outputs already exist"
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="Show pipeline status without running"
    )

    parser.add_argument(
        "--config",
        type=Path,
        help="Path to custom configuration file"
    )

    parser.add_argument(
        "--data-root",
        type=Path,
        help="Root directory for case data (overrides config)"
    )

    args = parser.parse_args()

    # Initialize pipeline
    try:
        pipeline = EventExtractionPipeline(
            config_path=args.config,
            data_root=args.data_root
        )
    except Exception as e:
        print(f"Error initializing pipeline: {e}")
        return 1

    # Show status if requested
    if args.status:
        pipeline.print_status(args.case_id)
        return 0

    # Run pipeline
    try:
        if args.stages:
            success = pipeline.run_stages(
                args.case_id,
                stages=args.stages,
                force=args.force
            )
        else:
            success = pipeline.run_all_stages(
                args.case_id,
                force=args.force
            )

        if success:
            print("\n✓ Pipeline completed successfully!")
            print(f"\nOutputs saved to: data/cases/{args.case_id}/events/")
            return 0
        else:
            print("\n✗ Pipeline failed!")
            return 1

    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user.")
        return 130
    except Exception as e:
        print(f"\nPipeline error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
