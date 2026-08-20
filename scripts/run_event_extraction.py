#!/usr/bin/env python3
"""Run the event extraction pipeline on a case.

This script orchestrates the event extraction pipeline, which extracts
legal events and timelines from parsed Docling documents.

Usage:
    # Run all stages for all cases in data/cases
    python scripts/run_event_extraction.py

    # Run all stages for a specific case
    python scripts/run_event_extraction.py case_67

    # Run specific stages
    python scripts/run_event_extraction.py case_67 --stages 1 2

    # Check pipeline status
    python scripts/run_event_extraction.py case_67 --status

    # Force re-run (overwrite existing outputs)
    python scripts/run_event_extraction.py case_67 --force
"""

import argparse
import logging
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

# Add parent directory to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lawsuit_parser.event_extraction import EventExtractionPipeline


def setup_logging_suppression(log_file: Path):
    """Suppress noisy library output and redirect to log file."""
    # Configure logging to suppress verbose libraries
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
        ]
    )

    # Suppress specific noisy loggers
    for logger_name in [
        'transformers',
        'torch',
        'tensorflow',
        'gliner',
        'huggingface_hub',
        'urllib3',
        'filelock',
    ]:
        logging.getLogger(logger_name).setLevel(logging.ERROR)

    # Set environment variables to reduce library verbosity
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    os.environ['TRANSFORMERS_VERBOSITY'] = 'error'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'


class LogFile:
    """Context manager to redirect stdout/stderr to a log file while preserving console output."""

    def __init__(self, log_path: Path, console_output=True):
        self.log_path = log_path
        self.console_output = console_output
        self.log_file = None
        self.old_stdout = None
        self.old_stderr = None

    def __enter__(self):
        self.log_file = open(self.log_path, 'a')
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr

        # Create a writer that writes to both log file and console
        class TeeWriter:
            def __init__(self, log_file, console, write_console):
                self.log_file = log_file
                self.console = console
                self.write_console = write_console

            def write(self, message):
                self.log_file.write(message)
                self.log_file.flush()
                if self.write_console:
                    self.console.write(message)
                    self.console.flush()

            def flush(self):
                self.log_file.flush()
                if self.write_console:
                    self.console.flush()

        # Redirect stderr to log file only (library warnings/errors)
        sys.stderr = TeeWriter(self.log_file, self.old_stderr, False)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr
        if self.log_file:
            self.log_file.close()


def main():
    parser = argparse.ArgumentParser(
        description="Run event extraction pipeline on one or all cases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run all stages on all cases in data/cases
  python scripts/run_event_extraction.py

  # Run all stages for a specific case
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
        nargs="?",
        help="Case identifier (e.g., case_67). If not provided, processes all cases in data/cases folder."
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

    # Set up logging
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"extraction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    setup_logging_suppression(log_file)

    print(f"Library output will be logged to: {log_file}")
    print()

    # Initialize pipeline
    try:
        with LogFile(log_file):
            pipeline = EventExtractionPipeline(
                config_path=args.config,
                data_root=args.data_root
            )
    except Exception as e:
        print(f"Error initializing pipeline: {e}")
        return 1

    # Determine which cases to process
    if args.case_id:
        # Process single case
        case_ids = [args.case_id]
    else:
        # Process all cases in data/cases folder
        data_root = args.data_root if args.data_root else REPO_ROOT / "data"
        cases_dir = data_root / "cases"

        if not cases_dir.exists():
            print(f"Error: Cases directory not found: {cases_dir}")
            return 1

        # Find all case directories
        case_ids = [d.name for d in cases_dir.iterdir() if d.is_dir() and d.name.startswith("case_")]

        if not case_ids:
            print(f"No cases found in {cases_dir}")
            return 1

        case_ids.sort()
        print(f"Found {len(case_ids)} cases to process: {', '.join(case_ids)}\n")

    # Show status if requested
    if args.status:
        for case_id in case_ids:
            print(f"\n{'='*60}")
            print(f"Status for {case_id}")
            print('='*60)
            pipeline.print_status(case_id)
        return 0

    # Run pipeline for each case
    successes = []
    failures = []

    try:
        # Use tqdm for progress bar when processing multiple cases
        pbar = tqdm(case_ids, desc="Processing cases", unit="case") if len(case_ids) > 1 else case_ids

        for case_id in pbar:
            if len(case_ids) > 1:
                pbar.set_description(f"Processing {case_id}")

            try:
                # Redirect library output to log file
                with LogFile(log_file):
                    if args.stages:
                        success = pipeline.run_stages(
                            case_id,
                            stages=args.stages,
                            force=args.force
                        )
                    else:
                        success = pipeline.run_all_stages(
                            case_id,
                            force=args.force
                        )

                if success:
                    successes.append(case_id)
                    msg = f"✓ {case_id} completed successfully"
                    if len(case_ids) > 1:
                        tqdm.write(msg)
                    else:
                        print(f"\n{msg}")
                        print(f"Outputs saved to: data/cases/{case_id}/events/")
                else:
                    failures.append(case_id)
                    msg = f"✗ {case_id} failed"
                    if len(case_ids) > 1:
                        tqdm.write(msg)
                    else:
                        print(f"\n{msg}")

            except Exception as e:
                failures.append(case_id)
                msg = f"✗ {case_id} error: {e}"
                if len(case_ids) > 1:
                    tqdm.write(msg)
                else:
                    print(f"\n{msg}")
                    import traceback
                    traceback.print_exc()

        # Close progress bar
        if len(case_ids) > 1 and hasattr(pbar, 'close'):
            pbar.close()

        # Print summary if processing multiple cases
        if len(case_ids) > 1:
            print(f"\n{'='*60}")
            print("Summary")
            print('='*60)
            print(f"Successful: {len(successes)}/{len(case_ids)}")
            if successes:
                print(f"  {', '.join(successes)}")
            print(f"Failed: {len(failures)}/{len(case_ids)}")
            if failures:
                print(f"  {', '.join(failures)}")
            print(f"\nFull log available at: {log_file}")

        return 0 if not failures else 1

    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
