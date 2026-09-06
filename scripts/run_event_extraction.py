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
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

# Add parent directory to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lawsuit_parser.event_extraction import EventExtractionPipeline

# tqdm progress bars are pinned to the real terminal (not this stream), so
# they stay visible even while it is redirected to the log file below.
CONSOLE = sys.stderr


def setup_logging(log_file: Path) -> None:
    """Route all pipeline logging (and captured stdlib warnings) to a log
    file only, so the console stays free for the tqdm progress bar and the
    handful of print() calls in this script.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_file)],
    )
    logging.captureWarnings(True)

    # Noisy third-party libraries: keep their routine chatter out of the
    # log file too, only their errors are worth keeping.
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


class RedirectStderrToLog:
    """Redirect raw (non-logging) stderr output - stray library prints,
    C-extension warnings - into the log file for the duration of a run,
    without touching stdout or the real terminal tqdm writes to."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_file = None
        self.old_stderr = None

    def __enter__(self):
        self.log_file = open(self.log_path, 'a')
        self.old_stderr = sys.stderr
        sys.stderr = self.log_file
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
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

  # Use custom output root (default: data/extraction) - e.g. to keep
  # iteration output separate from a data root shared across branches
  python scripts/run_event_extraction.py case_67 --output-root /tmp/extraction
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
        help="Root directory for source case data (overrides config)"
    )

    parser.add_argument(
        "--output-root",
        type=Path,
        help="Root directory for pipeline-generated artifacts (overrides config, "
             "default data/extraction). Kept separate from --data-root so a run's "
             "outputs can be wiped and regenerated without touching source data."
    )

    args = parser.parse_args()

    # Set up logging
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"extraction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    setup_logging(log_file)

    print(f"Logging to: {log_file}", file=CONSOLE)

    # Initialize pipeline
    try:
        with RedirectStderrToLog(log_file):
            pipeline = EventExtractionPipeline(
                config_path=args.config,
                data_root=args.data_root,
                output_root=args.output_root,
            )
    except Exception as e:
        print(f"Error initializing pipeline: {e}", file=CONSOLE)
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
            print(f"Error: Cases directory not found: {cases_dir}", file=CONSOLE)
            return 1

        # Find all case directories (recursively, preserving nested structure)
        case_ids = []
        for item in cases_dir.rglob("*"):
            if item.is_dir():
                # Skip docling directories (they mirror the structure but aren't case dirs)
                if "docling" in item.parts:
                    continue
                # Check if this directory has documents or confirmations subdirectories
                if (item / "documents").exists() or (item / "confirmations").exists():
                    # Use relative path from cases_dir as case_id
                    relative_path = item.relative_to(cases_dir)
                    case_ids.append(str(relative_path))

        if not case_ids:
            print(f"No cases found in {cases_dir}", file=CONSOLE)
            return 1

        case_ids.sort()
        print(f"Found {len(case_ids)} cases to process", file=CONSOLE)

    # Show status if requested
    if args.status:
        for case_id in case_ids:
            print(f"\n{'='*60}", file=CONSOLE)
            print(f"Status for {case_id}", file=CONSOLE)
            print('='*60, file=CONSOLE)
            pipeline.print_status(case_id)
        return 0

    # Run pipeline for each case
    successes = []
    failures = []

    try:
        pbar = tqdm(case_ids, desc="Cases", unit="case", file=CONSOLE)

        for case_id in pbar:
            pbar.set_postfix_str(case_id)

            try:
                with RedirectStderrToLog(log_file):
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
                    tqdm.write(
                        f"✓ {case_id} completed successfully -> "
                        f"{pipeline.config.paths.output_root}/{case_id}/events/",
                        file=CONSOLE,
                    )
                else:
                    failures.append(case_id)
                    tqdm.write(f"✗ {case_id} failed - see {log_file}", file=CONSOLE)

            except Exception as e:
                failures.append(case_id)
                logging.getLogger(__name__).exception(f"{case_id} raised an unhandled exception")
                tqdm.write(f"✗ {case_id} error: {e} - see {log_file}", file=CONSOLE)

        pbar.close()

        # Print summary
        print(f"\n{'='*60}", file=CONSOLE)
        print("Summary", file=CONSOLE)
        print('='*60, file=CONSOLE)
        print(f"Successful: {len(successes)}/{len(case_ids)}", file=CONSOLE)
        if successes:
            print(f"  {', '.join(successes)}", file=CONSOLE)
        print(f"Failed: {len(failures)}/{len(case_ids)}", file=CONSOLE)
        if failures:
            print(f"  {', '.join(failures)}", file=CONSOLE)
        print(f"\nFull log available at: {log_file}", file=CONSOLE)

        return 0 if not failures else 1

    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user.", file=CONSOLE)
        return 130


if __name__ == "__main__":
    sys.exit(main())
