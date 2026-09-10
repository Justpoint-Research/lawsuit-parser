"""Batch processing of PDF documents in the case data directory."""

import logging
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from tqdm import tqdm

from lawsuit_parser.parsers.pdf_parser import (
    _ensure_cuda_libs_loadable,
    parse_pdf_document,
    save_parsed_document,
)

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 8
DEFAULT_NUM_THREADS = 4
# Recycle each worker process after this many parses. Docling's ONNX/CUDA
# sessions leak a little memory per document, and a fresh process is the
# reliable way to reclaim it (and to recover a worker whose CUDA context
# has gone bad - see the TableFormer "CUDA error: out of memory" storms in
# pdf_parsing.log). None disables recycling.
DEFAULT_MAX_TASKS_PER_CHILD = 200


def find_all_pdfs(data_dir: Path, case_id: str | None = None) -> list[Path]:
    """
    Find all PDF files in the data directory.

    Args:
        data_dir: Root data directory
        case_id: Optional case directory to filter to - either a full
            directory name (e.g. "case_104", "mdl-1358") or a bare case
            number (e.g. "104", kept for backward compatibility - implies
            "case_104")

    Returns:
        List of PDF file paths
    """
    cases_dir = data_dir / "cases"

    if not cases_dir.exists():
        raise FileNotFoundError(f"Cases directory not found: {cases_dir}")

    # Find PDF files. No case_id filter processes every case directory
    # (case_* NYSCEF exports and mdl-* MDL docket scrapes alike), not just
    # case_*.
    if case_id:
        dir_name = case_id if (cases_dir / case_id).is_dir() else f"case_{case_id}"
        pattern = f"{dir_name}/**/*.pdf"
    else:
        pattern = "*/**/*.pdf"

    pdfs = sorted(cases_dir.glob(pattern))

    # Filter out .venv and other non-data directories
    pdfs = [p for p in pdfs if '.venv' not in str(p)]

    logger.info(f"Found {len(pdfs)} PDF files")
    return pdfs


def _find_case_dir(pdf_path: Path) -> Path:
    """Walk up from a PDF to find its case directory (the one containing
    `documents/` or `confirmations/`)."""
    current = pdf_path.parent
    while current.parent != current:  # Stop at root
        if (current / "documents").exists() or (current / "confirmations").exists():
            return current
        current = current.parent

    # Fallback to old behavior if we can't find the case directory
    return pdf_path.parents[1]


def get_docling_dir(pdf_path: Path, data_root: Path, output_root: Path) -> Path:
    """
    Determine the directory to save a PDF's Docling outputs
    (.docling.json, .md) into.

    Docling output lives under output_root (e.g. data/extraction), not next
    to the source PDF under data_root (e.g. data/cases) - this mirrors the
    event-extraction pipeline's own data_root/output_root split (see
    BaseStage.__init__): source case data stays untouched, pipeline-
    generated artifacts (including this expensive-to-regenerate Docling
    parse) live in their own tree that can be wiped/rebuilt independently.

    A case directory holds PDFs of the same name under multiple source
    subdirectories (e.g. `documents/` and `confirmations/` can each contain
    a `document_<id>.pdf` that are different files), so within the case's
    output directory, Docling output is further split by mirroring that
    source subdirectory (`docling/documents/`, `docling/confirmations/`) to
    avoid name collisions between them.

    Args:
        pdf_path: Path to PDF file, somewhere under data_root (e.g.
            data_root/case_104/documents/foo.pdf or
            data_root/ny_sample/case_104/documents/foo.pdf)
        data_root: Root directory PDFs are read from (e.g. data/cases).
        output_root: Root directory to write Docling output under (e.g.
            data/extraction) - the case's path relative to data_root is
            reproduced under output_root.

    Returns:
        Directory to save Docling outputs into (e.g.
        data/extraction/case_104/docling/documents or
        data/extraction/ny_sample/case_104/docling/documents)
    """
    case_dir = _find_case_dir(pdf_path)
    relative_case_dir = case_dir.relative_to(data_root)
    return output_root / relative_case_dir / "docling" / pdf_path.parent.name


def _docling_path(pdf_path: Path, data_root: Path, output_root: Path) -> Path:
    """Where a PDF's Docling output (.docling.json) would live, whether or
    not it's been parsed yet."""
    return get_docling_dir(pdf_path, data_root, output_root) / f"{pdf_path.stem}.docling.json"


def parse_and_save_pdf(
    pdf_path: Path,
    data_root: Path,
    output_root: Path,
    skip_existing: bool = False,
    use_gpu: bool = True,
    num_threads: int = DEFAULT_NUM_THREADS,
) -> tuple[bool, str]:
    """
    Parse a single PDF and save Docling's output (.docling.json, .md).

    A confirmations/ PDF (an e-filing confirmation notice) also gets a
    parsed-JSON sidecar saved next to it - Stage 1's confirmation-metadata
    extraction (extract_confirmation_details) still reads that sidecar's
    "paragraphs". This sidecar stays next to the source PDF under
    data_root (not under output_root like the Docling output below) since
    Stage 1 reads it via get_confirmations_dir, a data_root path. A
    documents/ PDF (a case's main filings) does NOT get one: the event
    extraction pipeline reads those via Docling only now (see
    BaseStage.load_document_text's docstring) - the sidecar's paragraph
    reconstruction (walking Docling's hierarchical reading-order tree)
    could silently drop entire pages that Docling's own flat text export
    still captures, confirmed on a dense deposition transcript where it
    lost 88% of the document.

    Args:
        pdf_path: Path to PDF file, somewhere under data_root.
        data_root: Root directory PDFs are read from (e.g. data/cases).
        output_root: Root directory to write Docling output under (e.g.
            data/extraction) - see get_docling_dir.
        skip_existing: Skip if Docling output already exists
        use_gpu: Use GPU acceleration
        num_threads: CPU threads each Docling model stage may use.

    Returns:
        Tuple of (success: bool, message: str)
    """
    docling_dir = get_docling_dir(pdf_path, data_root, output_root)

    try:
        # Check if already processed. Redundant with parse_all_pdfs's own
        # upfront filtering when called from there, but kept here too as a
        # safety net for direct callers and for a file that got parsed by
        # a concurrent run between that filtering pass and this call.
        if skip_existing and _docling_path(pdf_path, data_root, output_root).exists():
            return True, "Skipped (already exists)"

        # Parse the PDF (saves .docling.json/.md as a side effect)
        parsed = parse_pdf_document(
            pdf_path,
            use_gpu=use_gpu,
            extract_tables=True,
            extract_images=False,
            docling_dir=docling_dir,
            num_threads=num_threads,
        )

        if pdf_path.parent.name != "documents":
            save_parsed_document(parsed, pdf_path.with_suffix(".json"))

        return True, "Success"

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to parse {pdf_path}: {error_msg}")
        return False, f"Error: {error_msg[:100]}"


# --- Process-pool worker plumbing ---------------------------------------
#
# parse_all_pdfs fans PDFs out to a ProcessPoolExecutor rather than a
# ThreadPoolExecutor: Docling's per-file work is a mix of native ONNX/torch
# calls (which drop the GIL) and a substantial amount of pure-Python
# document assembly + JSON serialization (which does not), so beyond a
# handful of threads the GIL, not the CPU, is the ceiling. Separate
# processes each get their own interpreter, their own Docling converter,
# and their own CUDA context, and they can be recycled to reclaim leaked
# GPU/host memory. The trade-off is that the worker callable and its
# arguments must be picklable - hence a module-level function taking a
# plain tuple, instead of parse_all_pdfs's former local closure.


def _init_worker(num_threads: int) -> None:
    """Runs once per worker process (and again after each recycle).

    Caps the BLAS/OpenMP thread pools that some of Docling's incidental
    numpy/OpenCV work spins up. Docling's own model stages are capped
    separately via AcceleratorOptions.num_threads (passed through
    parse_pdf_document); this just stops the non-Docling code in each
    process from each grabbing the whole machine.
    """
    for var in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        os.environ[var] = str(num_threads)


def _parse_one_job(job: tuple) -> tuple[str, bool, str, float]:
    """Picklable worker entry point: parse one PDF, return a timed result.

    Mirrors the accounting the old in-process `timed_parse` closure did -
    wall time spent, so parse_all_pdfs can keep a skips-excluded average.
    """
    pdf_path, data_root, output_root, skip_existing, use_gpu, num_threads = job
    start = time.monotonic()
    try:
        success, message = parse_and_save_pdf(
            pdf_path,
            data_root,
            output_root,
            skip_existing=skip_existing,
            use_gpu=use_gpu,
            num_threads=num_threads,
        )
    except Exception as e:  # defensive: never let a worker die on one file
        success, message = False, f"Error: {str(e)[:100]}"
    return str(pdf_path), success, message, time.monotonic() - start


def parse_all_pdfs(
    data_dir: Path = Path("data"),
    case_id: str | None = None,
    skip_existing: bool = False,
    use_gpu: bool = True,
    progress_file: Any = None,
    max_workers: int = DEFAULT_WORKERS,
    output_root: Path | None = None,
    num_threads: int = DEFAULT_NUM_THREADS,
    max_tasks_per_child: int | None = DEFAULT_MAX_TASKS_PER_CHILD,
) -> dict[str, Any]:
    """
    Parse all PDFs in the data directory.

    Args:
        data_dir: Root data directory
        case_id: Optional case ID to filter
        skip_existing: Skip files that have already been processed
        use_gpu: Use GPU acceleration
        progress_file: Stream the tqdm progress bar is written to
            (default: sys.stderr). Useful when stderr has been redirected
            elsewhere and the progress bar still needs to reach a console.
        max_workers: Number of worker processes parsing PDFs concurrently
            (ProcessPoolExecutor). Each worker builds its own Docling
            converter on first use. Set to 1 for sequential, in-process
            parsing (no pool). Defaults to 8. Aim for
            ``max_workers * num_threads`` roughly equal to the physical
            core count.
        output_root: Root directory to write Docling output under (see
            get_docling_dir). Defaults to data_dir/"extraction", the
            sibling of data_dir/"cases" that the event-extraction pipeline
            itself reads/writes pipeline-generated artifacts under.
        num_threads: CPU threads each worker's Docling model stages may
            use (layout ONNX, TableFormer, OCR). Defaults to 4.
        max_tasks_per_child: Recycle a worker process after this many
            parses to reclaim leaked memory / reset a bad CUDA context.
            None disables recycling. Ignored when max_workers <= 1.

    Returns:
        Dictionary with summary statistics, including a "failures" list of
        (pdf_path, error_message) tuples.
    """
    data_root = data_dir / "cases"
    if output_root is None:
        output_root = data_dir / "extraction"

    # Find all PDFs
    pdfs = find_all_pdfs(data_dir, case_id)

    if not pdfs:
        logger.warning("No PDF files found")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "failures": []}

    stats = {"total": len(pdfs), "success": 0, "failed": 0, "skipped": 0}
    failures = []

    # Filter out already-parsed files upfront rather than submitting them
    # to the thread pool and letting parse_and_save_pdf's own skip_existing
    # check short-circuit them one by one: that made tqdm's bar (and its
    # rate/ETA) start from every already-parsed file racing past as a
    # near-instant "skip", which read as the run being stuck once real
    # parsing began right after - the counters looked frozen for as long
    # as the first real (10-30s) GPU parse took to complete. Filtering
    # first means the bar only ever tracks real work.
    if skip_existing:
        pdfs = [p for p in pdfs if not _docling_path(p, data_root, output_root).exists()]
        stats["skipped"] = stats["total"] - len(pdfs)

    if not pdfs:
        logger.info("All files already parsed")
        stats["failures"] = []
        return stats

    max_workers = min(max_workers, len(pdfs))

    if use_gpu and max_workers > 2:
        # Each worker process builds its own Docling converter and its own
        # CUDA context. Several of those running TableFormer on one GPU at
        # once is what fills VRAM ("CUDA error: out of memory" in the log,
        # after which that stage silently degrades). For a wide fan-out,
        # run CPU-only (use_gpu=False) or keep max_workers small.
        logger.warning(
            "use_gpu=True with max_workers=%d: %d concurrent CUDA contexts "
            "may exhaust GPU memory. Consider --no-gpu for wide parallelism.",
            max_workers,
            max_workers,
        )

    # onnxruntime's CUDAExecutionProvider needs CUDA runtime libs on
    # LD_LIBRARY_PATH, which glibc only reads at process start - so this
    # may re-exec the whole script once. Do it here, in the parent, before
    # any worker process is started: otherwise every worker would try to
    # re-exec *itself* on its first parse. No-op when GPU is off or the
    # libs are already on the path. Must run before the pool is created
    # (and before this process touches CUDA).
    if use_gpu:
        _ensure_cuda_libs_loadable()

    # Cap incidental BLAS/OpenMP parallelism in the parent's environment so
    # spawned workers inherit it at startup (those libs read these vars
    # once, at import). Docling's own model stages are capped separately
    # via num_threads; this covers the numpy/OpenCV code around them.
    _init_worker(num_threads)

    # Process each PDF with progress bar
    # Seconds actually spent parsing (excludes near-instant skips), summed
    # and counted separately from tqdm's own rate - see the docstring note
    # on `smoothing` below for why this exists.
    real_time_total = 0.0
    real_count = 0

    def make_job(pdf_path: Path) -> tuple:
        return (pdf_path, data_root, output_root, skip_existing, use_gpu, num_threads)

    def record(pdf_path: Path, success: bool, message: str, duration: float) -> None:
        nonlocal real_time_total, real_count
        if "Skipped" in message:
            stats["skipped"] += 1
            return
        # Real work happened (attempted parse, whether it succeeded or
        # failed) - count its time, unlike a skip's near-zero duration.
        real_time_total += duration
        real_count += 1
        if success:
            stats["success"] += 1
        else:
            stats["failed"] += 1
            failures.append((str(pdf_path), message))

    def update_postfix(pbar: tqdm) -> None:
        postfix = dict(success=stats["success"], failed=stats["failed"], skipped=stats["skipped"])
        if real_count:
            # Average seconds per actually-parsed file (skips excluded) -
            # the number that matters for estimating remaining runtime,
            # since a batch's skip/parse mix at the start (e.g. mostly
            # already-parsed files) would otherwise skew a blended rate.
            postfix["avg_s"] = f"{real_time_total / real_count:.1f}"
        pbar.set_postfix(postfix)

    # smoothing=0 makes tqdm report a cumulative (n / total_elapsed) rate
    # and ETA instead of its default exponential-moving-average one, which
    # reacts to only the last few iterations - on this workload, where most
    # files take 2-10s but occasional scanned/OCR-heavy ones take 90s+, the
    # default smoothing made the displayed rate/ETA swing wildly right
    # after each outlier. A cumulative average is far steadier, at the cost
    # of reacting more slowly to a genuine sustained speed change.
    with tqdm(total=len(pdfs), desc="Parsing PDFs", unit="file", file=progress_file, smoothing=0) as pbar:
        if max_workers <= 1:
            # Sequential: parse in this process, no pool (thread caps were
            # already applied to this process's env above).
            for pdf_path in pdfs:
                _, success, message, duration = _parse_one_job(make_job(pdf_path))
                record(pdf_path, success, message, duration)
                update_postfix(pbar)
                pbar.update(1)
        else:
            # "spawn", not the Linux default "fork": these workers import
            # torch / onnxruntime / CUDA, and forking a process that has
            # already loaded those is a known source of hangs and duplicated
            # CUDA contexts. "spawn" is also what max_tasks_per_child
            # requires. Costs a few seconds of interpreter+model startup per
            # (re)spawned worker - negligible against a multi-hour batch.
            executor_kwargs: dict[str, Any] = {
                "max_workers": max_workers,
                "initializer": _init_worker,
                "initargs": (num_threads,),
                "mp_context": multiprocessing.get_context("spawn"),
            }
            if max_tasks_per_child is not None:
                executor_kwargs["max_tasks_per_child"] = max_tasks_per_child
            with ProcessPoolExecutor(**executor_kwargs) as executor:
                future_to_pdf = {
                    executor.submit(_parse_one_job, make_job(pdf_path)): pdf_path
                    for pdf_path in pdfs
                }
                for future in as_completed(future_to_pdf):
                    pdf_path = future_to_pdf[future]
                    _, success, message, duration = future.result()
                    record(pdf_path, success, message, duration)
                    update_postfix(pbar)
                    pbar.update(1)

    # Log summary
    logger.info("\n" + "="*60)
    logger.info("PARSING SUMMARY")
    logger.info("="*60)
    logger.info(f"Total files: {stats['total']}")
    logger.info(f"Successfully parsed: {stats['success']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"Skipped: {stats['skipped']}")

    if failures:
        logger.info("\nFailed files:")
        for pdf_path, error in failures[:10]:  # Show first 10 failures
            logger.info(f"  - {pdf_path}: {error}")
        if len(failures) > 10:
            logger.info(f"  ... and {len(failures) - 10} more")

    stats["failures"] = failures
    return stats