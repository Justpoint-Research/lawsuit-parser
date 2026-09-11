#!/usr/bin/env python
"""
Download and Docling-parse the stratified classification sample built by
build_classification_sample.py.

For each selected case, downloads only its earliest N documents (per
data/classification_sample_doc_selection.json) from GCS into
data/cases/ny_classification/case_<id>/documents/ (confirmations are
skipped - they're e-filing receipts, not case content, and add little
value for case-type classification), copies the case's full metadata JSON
in from data/cases/ny_after_search (already fetched for all 30k cases), and
then runs Docling extraction on exactly those downloaded PDFs into
data/extraction/ny_classification/case_<id>/docling/documents/.

Usage:
    uv run python scripts/export_classification_sample.py
    uv run python scripts/export_classification_sample.py --workers 8 --no-gpu
"""

import json
import multiprocessing
import shutil
import sys
import time
from pathlib import Path

import click
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.parsers.batch import (
    DEFAULT_MAX_TASKS_PER_CHILD,
    DEFAULT_NUM_THREADS,
    DEFAULT_STALL_TIMEOUT,
    DEFAULT_WORKERS,
    _init_worker,
    _parse_one_job,
    _run_pdf_pool,
)
from lawsuit_parser.parsers.pdf_parser import _ensure_cuda_libs_loadable
from lawsuit_parser.utils import CaseExporter, load_db_config
from lawsuit_parser.utils.case_exporter import _blob_path

AFTER_SEARCH_DIR = Path("data/cases/ny_after_search")
CLASSIFICATION_DIR = Path("data/cases/ny_classification")
EXTRACTION_DIR = Path("data/extraction/ny_classification")
SAMPLE_IDS_PATH = Path("data/classification_sample_ids.json")
DOC_SELECTION_PATH = Path("data/classification_sample_doc_selection.json")
LABELS_PATH = Path("data/classification_labels.json")


def make_exporter() -> CaseExporter:
    # engine is unused here (all metadata already lives on disk from the
    # ny_after_search export) - only the GCS client CaseExporter.__init__
    # sets up is needed. Construct one anyway so download_from_gcs_to_file
    # has the state-code-prefixing/bucket logic already implemented there.
    return CaseExporter(engine=None, output_dir=CLASSIFICATION_DIR, download_files=False)


def download_selected_documents(exporter: CaseExporter, case_ids: list[int], doc_selection: dict) -> list[Path]:
    """Download the selected main-document PDFs for each case. Returns the
    list of local PDF paths that exist on disk afterward (whether just
    downloaded or already present)."""
    downloaded_paths = []
    failures = []

    for case_id in tqdm(case_ids, desc="Downloading PDFs", unit="case"):
        after_search_json = AFTER_SEARCH_DIR / f"case_{case_id}" / f"case_{case_id}.json"
        if not after_search_json.exists():
            failures.append((case_id, "no ny_after_search metadata"))
            continue
        with open(after_search_json) as f:
            meta = json.load(f)

        case_dir = CLASSIFICATION_DIR / f"case_{case_id}"
        case_dir.mkdir(parents=True, exist_ok=True)
        docs_dir = case_dir / "documents"
        docs_dir.mkdir(exist_ok=True)

        # Bring the full metadata JSON along too (same convention as every
        # other case in ny_classification: the JSON lists every docketed
        # document even when only a subset was actually downloaded).
        case_json_path = case_dir / f"case_{case_id}.json"
        if not case_json_path.exists():
            shutil.copy(after_search_json, case_json_path)

        wanted_indices = set(doc_selection.get(str(case_id), []))
        docs_by_index = {d["document_doc_index"]: d for d in meta.get("documents", [])}

        for doc_index in wanted_indices:
            doc = docs_by_index.get(doc_index)
            if doc is None:
                continue
            gcs_path = _blob_path(doc.get("document_bucket_link"))
            if not gcs_path:
                continue
            filename = exporter._extract_filename_from_gcs_path(gcs_path)
            local_path = docs_dir / filename
            try:
                exporter.download_from_gcs_to_file(gcs_path, local_path)
                if local_path.exists():
                    downloaded_paths.append(local_path)
            except Exception as e:
                failures.append((case_id, f"{doc_index}: {e}"))

    if failures:
        print(f"\n{len(failures)} download failure(s):")
        for case_id, msg in failures[:20]:
            print(f"  case_{case_id}: {msg}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")

    return downloaded_paths


def parse_pdfs(
    pdf_paths: list[Path],
    workers: int,
    use_gpu: bool,
    threads_per_worker: int,
    stall_timeout: float = DEFAULT_STALL_TIMEOUT,
) -> dict:
    data_root = Path("data/cases")
    output_root = Path("data/extraction")

    if use_gpu:
        _ensure_cuda_libs_loadable()
    _init_worker(threads_per_worker)

    stats = {"total": len(pdf_paths), "success": 0, "failed": 0, "skipped": 0}
    failures = []

    def make_job(pdf_path: Path) -> tuple:
        return (pdf_path, data_root, output_root, True, use_gpu, threads_per_worker)

    def record(pdf_path: Path, success: bool, message: str, duration: float) -> None:
        if "Skipped" in message:
            stats["skipped"] += 1
        elif success:
            stats["success"] += 1
        else:
            stats["failed"] += 1
            failures.append((str(pdf_path), message))

    def update_postfix(pbar: tqdm) -> None:
        pbar.set_postfix(success=stats["success"], failed=stats["failed"], skipped=stats["skipped"])

    with tqdm(total=len(pdf_paths), desc="Parsing PDFs", unit="file") as pbar:
        if workers <= 1:
            for pdf_path in pdf_paths:
                _, success, message, duration = _parse_one_job(make_job(pdf_path))
                record(pdf_path, success, message, duration)
                update_postfix(pbar)
                pbar.update(1)
        else:
            # Same pool logic as parse_all_pdfs.py (see batch._run_pdf_pool):
            # "spawn" context (safe with torch/CUDA already loaded), worker
            # recycling via max_tasks_per_child, and a stall watchdog that
            # kills and restarts the pool if workers die and
            # ProcessPoolExecutor fails to notice/replace them - the plain
            # ProcessPoolExecutor + as_completed loop this used to run had
            # none of that and would hang forever once workers died.
            executor_kwargs = {
                "max_workers": workers,
                "initializer": _init_worker,
                "initargs": (threads_per_worker,),
                "mp_context": multiprocessing.get_context("spawn"),
                "max_tasks_per_child": DEFAULT_MAX_TASKS_PER_CHILD,
            }
            _run_pdf_pool(pdf_paths, make_job, record, update_postfix, pbar, executor_kwargs, stall_timeout)

    stats["failures"] = failures
    return stats


@click.command()
@click.option("--workers", type=int, default=8, help="Docling parse workers (default: 8)")
@click.option("--threads-per-worker", type=int, default=4, help="CPU threads per worker (default: 4)")
@click.option("--no-gpu", is_flag=True, help="Disable GPU for Docling parsing")
@click.option("--skip-download", is_flag=True, help="Skip the GCS download phase, only parse what's already on disk for the sample")
@click.option(
    "--stall-timeout",
    type=int,
    default=DEFAULT_STALL_TIMEOUT,
    help="If no file finishes within this many seconds while work is still "
    "outstanding, kill the worker pool and start a fresh one for the "
    f"remaining files instead of hanging forever (default: {DEFAULT_STALL_TIMEOUT}).",
)
def main(workers: int, threads_per_worker: int, no_gpu: bool, skip_download: bool, stall_timeout: int):
    sample = json.load(open(SAMPLE_IDS_PATH))
    doc_selection = json.load(open(DOC_SELECTION_PATH))
    positive_ids = set(sample["positive_ids"])
    negative_ids = set(sample["negative_ids"])
    all_ids = sorted(positive_ids | negative_ids)
    print(f"Sample: {len(all_ids)} cases ({len(positive_ids)} positive / {len(negative_ids)} negative)")

    if not skip_download:
        exporter = make_exporter()
        t0 = time.monotonic()
        pdf_paths = download_selected_documents(exporter, all_ids, doc_selection)
        print(f"Downloaded/verified {len(pdf_paths)} PDFs in {time.monotonic() - t0:.0f}s")
    else:
        pdf_paths = []
        for case_id in all_ids:
            docs_dir = CLASSIFICATION_DIR / f"case_{case_id}" / "documents"
            if docs_dir.exists():
                pdf_paths.extend(docs_dir.glob("*.pdf"))
        print(f"Found {len(pdf_paths)} PDFs already on disk for this sample")

    t0 = time.monotonic()
    stats = parse_pdfs(
        pdf_paths,
        workers=workers,
        use_gpu=not no_gpu,
        threads_per_worker=threads_per_worker,
        stall_timeout=stall_timeout,
    )
    elapsed = time.monotonic() - t0
    print(
        f"Parsed {stats['success']}/{stats['total']} files "
        f"({stats['skipped']} skipped, {stats['failed']} failed) in {elapsed:.0f}s"
    )
    for pdf_path, msg in stats["failures"][:10]:
        print(f"  FAILED: {pdf_path}: {msg}")

    # Write final label map for the classifier.
    labels = {}
    for case_id in positive_ids:
        labels[f"case_{case_id}"] = 1
    for case_id in negative_ids:
        labels[f"case_{case_id}"] = 0
    with open(LABELS_PATH, "w") as f:
        json.dump(labels, f, indent=2)
    print(f"Wrote {len(labels)} labels to {LABELS_PATH}")


if __name__ == "__main__":
    main()
