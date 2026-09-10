#!/usr/bin/env python
"""
Batch process all PDF documents in the data directory.

This script:
1. Finds all PDF files in data/cases/
2. Parses each PDF using Docling for structured extraction, saving
   .docling.json/.md next to each PDF's case-level docling/ directory
   (a case's own documents/ files) - plus a parsed-JSON sidecar for
   confirmations/ PDFs only (e-filing confirmation notices), which Stage 1
   of the event extraction pipeline still reads for filer/judge/timestamp
   metadata. A case's main documents/ files do NOT get that sidecar - the
   pipeline reads those via Docling only (see
   BaseStage.load_document_text's docstring for why: the sidecar's
   paragraph reconstruction can silently drop entire pages Docling's own
   flat text export still captures).
3. Shows progress with tqdm
4. Handles errors gracefully and logs failures

All log output (ours and the noisy third-party libraries Docling pulls in,
e.g. torch/transformers) is written to pdf_parsing.log. The console only
shows the tqdm progress bar and a final success/failure summary.

Usage:
    python scripts/parse_all_pdfs.py
    python scripts/parse_all_pdfs.py --case-id 104
    python scripts/parse_all_pdfs.py --skip-existing
"""

import contextlib
import logging
import os
import sys
from pathlib import Path

import click

# Add parent directory to path to import lawsuit_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.parsers import parse_all_pdfs

LOG_FILE = Path("pdf_parsing.log")


def configure_logging(log_path: Path) -> None:
    """Send all logging (ours and third-party libraries') to the log file only."""
    logging.captureWarnings(True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    root.addHandler(handler)


@contextlib.contextmanager
def quiet_console(log_path: Path):
    """
    Redirect stdout/stderr, including raw output from libraries that don't
    go through Python logging (torch, docling model downloads, etc.), to
    the log file for the duration of the block.

    Yields a stream still attached to the real console, for output (like
    the tqdm progress bar) that should stay visible.

    The yielded stream is opened on /dev/tty rather than dup'd from fd 2:
    a dup of fd 2 is only as reliable as fd 2 itself being the terminal at
    that moment, and in practice (background/nohup launches, wrapper
    scripts, etc.) fd 2 can already be pointing somewhere else, which
    silently sent the progress bar into this same log file instead of the
    terminal. /dev/tty always refers to the process's controlling
    terminal regardless of what fd 1/2 have been redirected to. Falls back
    to the fd-2-dup behavior if there's no controlling terminal at all
    (e.g. run under cron/CI with no tty).
    """
    log_file = open(log_path, 'a', buffering=1, encoding='utf-8')

    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)
    try:
        console = open('/dev/tty', 'w', buffering=1, encoding='utf-8')
    except OSError:
        console = os.fdopen(os.dup(2), 'w', buffering=1, encoding='utf-8')

    try:
        os.dup2(log_file.fileno(), 1)
        os.dup2(log_file.fileno(), 2)
        yield console
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout_fd, 1)
        os.dup2(saved_stderr_fd, 2)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)
        console.close()
        log_file.close()


@click.command()
@click.option(
    '--data-dir',
    type=str,
    default='data',
    help='Root data directory (default: data)'
)
@click.option(
    '--case-id',
    type=str,
    default=None,
    help='Process only a specific case directory: a bare case number (e.g. 104, '
         'implies case_104), or a full directory name (e.g. case_104, mdl-1358)'
)
@click.option(
    '--skip-existing',
    is_flag=True,
    help='Skip files that have already been processed'
)
@click.option(
    '--no-gpu',
    is_flag=True,
    help='Disable GPU acceleration'
)
@click.option(
    '--workers',
    type=int,
    default=8,
    help='Number of worker processes parsing PDFs concurrently (default: 8). '
         'Aim for workers * threads-per-worker near the physical core count. '
         '--workers 1 parses sequentially in-process (no pool).'
)
@click.option(
    '--threads-per-worker',
    type=int,
    default=4,
    help='CPU threads each worker\'s Docling model stages may use (default: 4).'
)
@click.option(
    '--max-tasks-per-child',
    type=int,
    default=200,
    help='Recycle a worker process after this many parses to reclaim leaked '
         'memory / reset a bad CUDA context (default: 200; 0 disables).'
)
def main(
    data_dir: str,
    case_id: str | None,
    skip_existing: bool,
    no_gpu: bool,
    workers: int,
    threads_per_worker: int,
    max_tasks_per_child: int,
):
    """Parse all PDF documents and extract structured content.

    Examples:

      # Parse all PDFs in data directory

      python scripts/parse_all_pdfs.py

      # Parse only case 104

      python scripts/parse_all_pdfs.py --case-id 104

      # Skip already processed files

      python scripts/parse_all_pdfs.py --skip-existing

      # Disable GPU acceleration

      python scripts/parse_all_pdfs.py --no-gpu

      # Parse sequentially (concurrency is on by default, --workers 8)

      python scripts/parse_all_pdfs.py --workers 1

      # Saturate a 32-core box (CPU-only), recycling workers often

      python scripts/parse_all_pdfs.py --no-gpu --workers 8 --threads-per-worker 4
    """
    # Convert to Path objects
    data_dir_path = Path(data_dir)

    configure_logging(LOG_FILE)

    with quiet_console(LOG_FILE) as console:
        stats = parse_all_pdfs(
            data_dir=data_dir_path,
            case_id=case_id,
            skip_existing=skip_existing,
            use_gpu=not no_gpu,
            progress_file=console,
            max_workers=workers,
            num_threads=threads_per_worker,
            max_tasks_per_child=max_tasks_per_child or None,
        )

    # Back on the real console: report the outcome.
    click.echo(
        f"Parsed {stats['success']}/{stats['total']} files "
        f"({stats['skipped']} skipped, {stats['failed']} failed). "
        f"Full log: {LOG_FILE}"
    )

    failures = stats.get("failures", [])
    for pdf_path, error in failures[:10]:
        click.echo(f"  FAILED: {pdf_path}: {error}", err=True)
    if len(failures) > 10:
        click.echo(f"  ... and {len(failures) - 10} more (see {LOG_FILE})", err=True)

    # Exit with error code if any failures
    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()