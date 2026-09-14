#!/usr/bin/env python
"""
Download and Docling-parse the stratified classification sample built by
build_classification_sample.py.

For each selected case, this ALWAYS re-queries the live scrapping DB (via
CaseExporter.export_case_by_id, skip_if_exists=False - never a locally
cached copy) for its current metadata JSON, picks its earliest N documents
that currently have a GCS bucket link, then checks the local
documents/ dir and downloads only the ones actually missing on disk - an
existing PDF is left untouched, never re-verified or re-fetched, unless
--force deletes and re-downloads it too. Docling extraction then runs over
exactly those PDFs (already-present + newly-downloaded) into
data/extraction/ny_classification/case_<id>/docling/documents/.

Why live, not a snapshot: the crawler backfills document_bucket_link well
after a case is first listed - verified 2026-09-13 that ~12% of cases a
week-old data/cases/ny_after_search snapshot called "nothing downloadable"
already had real documents live, including cases already sitting in this
sample. data/classification_sample_doc_selection.json is therefore treated
as a derived report, not an input: it's fully recomputed from the live
per-case document list on every run and overwritten, never merged with what
was there before.

Refreshing all ~1150 sample cases via CaseExporter.export_case_by_id (2
sequential queries each) on every run is expensive and almost always
pointless: verified 2026-09-14 against the live DB that of the full sample,
only 13 cases actually had a different set of earliest-downloadable
documents than what was already on record - the other 1137 were unchanged.
So before doing any per-case refresh, find_changed_cases() runs ONE cheap
batched query for every sample case's current documents, recomputes each
one's earliest-downloadable selection, and diffs it against the previous
DOC_SELECTION_PATH. Only cases whose live selection actually differs (new
sample cases and a first-ever run count as "differs") go through the
expensive per-case export_case_by_id + download path; every unchanged case
reuses its already-downloaded PDFs with no further DB or GCS calls at all.
--force bypasses this diffing entirely (every case is treated as changed),
since it also needs to re-download PDFs whose selection didn't change.

Usage:
    uv run python scripts/export_classification_sample.py
    uv run python scripts/export_classification_sample.py --workers 8 --no-gpu

    # Already downloaded, e.g. from a prior run - just (re-)parse, no DB/GCS:
    uv run python scripts/export_classification_sample.py --skip-download --no-gpu
"""

import json
import multiprocessing
import sys
import time
from collections import defaultdict
from pathlib import Path

import click
from sqlalchemy import create_engine, text
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from build_classification_sample import has_bucket_link
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
from lawsuit_parser.utils.case_exporter import SCRAPPING_DB_PORT, _blob_path

CLASSIFICATION_DIR = Path("data/cases/ny_classification")
EXTRACTION_DIR = Path("data/extraction/ny_classification")
SAMPLE_IDS_PATH = Path("data/classification_sample_ids.json")
DOC_SELECTION_PATH = Path("data/classification_sample_doc_selection.json")
LABELS_PATH = Path("data/classification_labels.json")

# Matches the size of every doc_selection entry generated so far (max 5,
# fewer only when a case has fewer eligible documents) - see
# select_earliest_downloadable.
DOCS_PER_CASE = 5


def select_earliest_downloadable(documents: list[dict], n: int = DOCS_PER_CASE) -> list[str]:
    """Pick up to n document_doc_index values, earliest first, skipping any
    document with no document_bucket_link right now.

    `documents` must already be in chronological order - true of whatever
    export_case_by_id just wrote, since its docs_query is `ORDER BY id` and
    document rows are inserted in the order the crawler encountered them.
    """
    downloadable = [d for d in documents if has_bucket_link(d.get("document_bucket_link"))]
    return [d["document_doc_index"] for d in downloadable[:n]]


def make_exporter() -> CaseExporter:
    p = load_db_config()
    engine = create_engine(
        f"postgresql+psycopg://{p['user']}:{p['password']}"
        f"@{p['host']}:{SCRAPPING_DB_PORT}/{p.get('database', 'postgres')}"
    )
    return CaseExporter(engine=engine, output_dir=CLASSIFICATION_DIR, download_files=False)


def load_prev_doc_selection() -> dict:
    if not DOC_SELECTION_PATH.exists():
        return {}
    with open(DOC_SELECTION_PATH) as f:
        return json.load(f)


def find_changed_cases(engine, case_ids: list[int], prev_selection: dict) -> tuple[set[int], dict]:
    """One cheap batched query for every sample case's current documents,
    recomputing each one's earliest-downloadable selection and diffing it
    against prev_selection (the last run's DOC_SELECTION_PATH) - see module
    docstring for why this replaces refreshing all cases unconditionally.

    A case missing from prev_selection (new to the sample, or a first-ever
    run with no prior file) always counts as changed.

    Returns (set of case_ids whose live selection differs, {case_id:
    list[dict]} of live document rows for every case_id - reused for
    unchanged cases so they need no further DB call at all)."""
    query = text("""
        SELECT c.id AS case_id, d.id AS doc_row_id, d.document_doc_index, d.document_bucket_link
        FROM courts_final.ny_cases_after_search c
        JOIN courts_final.ny_docket_documents d ON d.docket_id = c.docket_id
        WHERE c.id = ANY(:case_ids)
        ORDER BY c.id, d.id
    """)
    with engine.connect() as conn:
        rows = conn.execute(query, {"case_ids": case_ids}).fetchall()

    docs_by_case = defaultdict(list)
    for r in rows:
        docs_by_case[r.case_id].append(
            {"document_doc_index": r.document_doc_index, "document_bucket_link": r.document_bucket_link}
        )

    changed_ids = set()
    for case_id in case_ids:
        live_indices = select_earliest_downloadable(docs_by_case.get(case_id, []))
        if prev_selection.get(str(case_id)) != live_indices:
            changed_ids.add(case_id)

    return changed_ids, docs_by_case


def download_selected_documents(
    exporter: CaseExporter, case_ids: list[int], force: bool = False
) -> tuple[list[Path], dict]:
    """Refresh only the cases whose live doc_selection actually changed
    (see find_changed_cases / module docstring; force=True refreshes every
    case instead), then download whichever of the wanted documents are
    still missing from disk. Existing PDFs are left untouched - never
    re-verified or re-fetched - unless force=True, which deletes and
    re-downloads them too.

    Returns (downloaded/verified PDF paths, doc_selection dict actually
    used - the fresh replacement for
    data/classification_sample_doc_selection.json)."""
    downloaded_paths = []
    failures = []
    doc_selection = {}
    n_already_on_disk = 0
    n_newly_downloaded = 0

    if force:
        changed_ids, docs_by_case = set(case_ids), {}
    else:
        prev_selection = load_prev_doc_selection()
        changed_ids, docs_by_case = find_changed_cases(exporter.engine, case_ids, prev_selection)
        print(
            f"{len(changed_ids)}/{len(case_ids)} case(s) have a different live doc "
            f"selection than last run - only these need a DB/GCS refresh"
        )

    for case_id in tqdm(case_ids, desc="Refreshing metadata + downloading PDFs", unit="case"):
        docs_dir = CLASSIFICATION_DIR / f"case_{case_id}" / "documents"

        if case_id in changed_ids:
            try:
                json_path, _ = exporter.export_case_by_id(case_id, skip_if_exists=False)
            except ValueError as e:
                # No documents at all (or case_id not found) - genuinely nothing
                # to download right now, not a transient failure.
                doc_selection[str(case_id)] = []
                failures.append((case_id, str(e)))
                continue

            with open(json_path) as f:
                case_data = json.load(f)
            documents = case_data.get("documents", [])
            docs_dir = json_path.parent / "documents"
        else:
            # Unchanged since last run: reuse the documents already fetched
            # by find_changed_cases's single batched query - no per-case
            # export_case_by_id/GCS call needed at all.
            documents = docs_by_case.get(case_id, [])

        wanted_indices = select_earliest_downloadable(documents)
        doc_selection[str(case_id)] = wanted_indices
        if not wanted_indices:
            # Nothing to download - and for an unchanged case with no prior
            # documents at all, case_<id>/ itself may never have been
            # created on disk (e.g. a past export_case_by_id ValueError), so
            # docs_dir.mkdir() below would fail on the missing parent.
            continue

        docs_dir.mkdir(parents=True, exist_ok=True)
        docs_by_index = {d["document_doc_index"]: d for d in documents}

        for doc_index in wanted_indices:
            doc = docs_by_index[doc_index]
            gcs_path = _blob_path(doc.get("document_bucket_link"))
            if not gcs_path:
                continue
            filename = exporter._extract_filename_from_gcs_path(gcs_path)
            local_path = docs_dir / filename

            already_present = local_path.exists()
            if already_present and force:
                local_path.unlink()
                already_present = False

            if already_present:
                n_already_on_disk += 1
                downloaded_paths.append(local_path)
                continue

            try:
                exporter.download_from_gcs_to_file(gcs_path, local_path)
                if local_path.exists():
                    n_newly_downloaded += 1
                    downloaded_paths.append(local_path)
            except Exception as e:
                failures.append((case_id, f"{doc_index}: {e}"))

    print(f"\n{n_already_on_disk} PDF(s) already on disk (left untouched), "
          f"{n_newly_downloaded} newly downloaded")

    if failures:
        print(f"\n{len(failures)} download failure(s):")
        for case_id, msg in failures[:20]:
            print(f"  case_{case_id}: {msg}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")

    return downloaded_paths, doc_selection


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
@click.option("--force", is_flag=True, help="Refresh every case's metadata live and re-download PDFs already on disk too, instead of only cases that changed since last run")
@click.option(
    "--stall-timeout",
    type=int,
    default=DEFAULT_STALL_TIMEOUT,
    help="If no file finishes within this many seconds while work is still "
    "outstanding, kill the worker pool and start a fresh one for the "
    f"remaining files instead of hanging forever (default: {DEFAULT_STALL_TIMEOUT}).",
)
def main(workers: int, threads_per_worker: int, no_gpu: bool, skip_download: bool, force: bool, stall_timeout: int):
    sample = json.load(open(SAMPLE_IDS_PATH))
    positive_ids = set(sample["positive_ids"])
    negative_ids = set(sample["negative_ids"])
    all_ids = sorted(positive_ids | negative_ids)
    print(f"Sample: {len(all_ids)} cases ({len(positive_ids)} positive / {len(negative_ids)} negative)")

    if not skip_download:
        exporter = make_exporter()
        t0 = time.monotonic()
        pdf_paths, doc_selection = download_selected_documents(exporter, all_ids, force=force)
        print(f"Downloaded/verified {len(pdf_paths)} PDFs in {time.monotonic() - t0:.0f}s")

        with open(DOC_SELECTION_PATH, "w") as f:
            json.dump(doc_selection, f, indent=2)
        n_empty = sum(1 for v in doc_selection.values() if not v)
        print(f"Wrote {len(doc_selection)} doc_selection entries to {DOC_SELECTION_PATH} "
              f"({n_empty} have zero downloadable documents right now)")
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
