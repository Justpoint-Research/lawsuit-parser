#!/usr/bin/env python3
"""Export a random sample of court cases with their documents.

This script connects to the scrapping database (port 5433), selects a random
sample of court cases, and exports them to denormalized JSON files along with
downloading their associated PDF documents from Google Cloud Storage.

Usage:
    python scripts/export_cases.py --count 100 --output-dir data/cases

Requirements:
    - Cloud SQL Proxy must be running (make run-proxy)
    - Google Cloud authentication configured (gcloud auth application-default login)
"""

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils import CaseExporter, load_db_config


def create_scrapping_engine():
    """Create SQLAlchemy engine for the scrapping database (port 5433).

    Returns:
        SQLAlchemy engine connected to scrapping database.
    """
    # Load config but override port to 5433 for scrapping database
    config = load_db_config()
    config["port"] = 5433  # Scrapping database port

    from sqlalchemy.engine import URL

    url = URL.create(
        drivername="postgresql+psycopg",
        username=config["user"],
        password=config["password"],
        host=config["host"],
        port=int(config["port"]),
        database=config["database"],
    )
    return create_engine(url)


def get_random_case_ids(
    engine,
    count: int = 100,
    schema: str = "courts_final",
    table_prefix: str = "ny_",
) -> list[int]:
    """Get random case IDs from the cases table.

    Args:
        engine: SQLAlchemy engine.
        count: Number of random cases to sample.
        schema: Postgres schema holding the crawl tables.
        table_prefix: Per-state table prefix, e.g. "ny_" or "fl_".

    Returns:
        List of case IDs.
    """
    cases_table = f"{schema}.{table_prefix}cases_after_search"
    documents_table = f"{schema}.{table_prefix}docket_documents"
    query = text(f"""
        SELECT id
        FROM {cases_table} cc
        WHERE cc.case_id IS NOT NULL  -- Filter out cases without case_id
          AND EXISTS (
              SELECT 1 FROM {documents_table} cd
              WHERE cd.docket_id = cc.docket_id
          )  -- Filter out cases without documents
        ORDER BY RANDOM()
        LIMIT :count
    """)

    with engine.connect() as conn:
        result = conn.execute(query, {"count": count})
        case_ids = [row[0] for row in result]

    return case_ids


def export_cases(
    case_ids: list[int],
    output_dir: Path,
    bucket_name: str = "courts_crawl",
    schema: str = "courts_final",
    table_prefix: str = "ny_",
    extract_text: bool = False,
    use_gpu: bool = True,
    download_files: bool = False,
    extraction_dir: Path = Path("data/extraction"),
    overwrite: bool = False,
):
    """Export multiple cases via bulk queries (4 queries total, not 4 per case).

    Args:
        case_ids: List of case IDs to export.
        output_dir: Output directory for exported cases.
        bucket_name: GCS bucket name where documents are stored.
        schema: Postgres schema holding the crawl tables.
        table_prefix: Per-state table prefix, e.g. "ny_" or "fl_".
        extract_text: Also run Docling over every downloaded PDF and save
            a .txt counterpart (slow, GPU/CPU-heavy - off by default).
            Ignored when download_files=False.
        use_gpu: Whether Docling should use GPU acceleration when
            extract_text=True.
        download_files: If False (the default), skip downloading
            PDFs/confirmations from GCS entirely - only the DB-sourced JSON
            metadata is written. Pass True to also download files.
        extraction_dir: Root directory for Docling output when
            extract_text=True (default: data/extraction). Keep this
            parallel to output_dir - e.g. output_dir=data/cases/ny_sample
            should pair with extraction_dir=data/extraction/ny_sample.
        overwrite: If True, re-fetch and overwrite cases whose JSON already
            exists instead of skipping them - use this to refresh a stale
            export against the DB's current state (e.g. document_bucket_link
            values backfilled after the original export ran).
    """
    engine = create_scrapping_engine()

    try:
        exporter = CaseExporter(
            engine=engine,
            output_dir=output_dir,
            gcs_bucket_name=bucket_name,
            schema=schema,
            table_prefix=table_prefix,
            extract_text=extract_text,
            use_gpu=use_gpu,
            download_files=download_files,
            extraction_root=extraction_dir,
        )

        print(f"Exporting {len(case_ids)} cases (download_files={download_files})...")
        stats = exporter.export_cases_bulk(
            case_ids, skip_if_exists=not overwrite, force_refresh=overwrite
        )

        print("\n" + "=" * 80)
        print(f"Export complete!")
        print(f"  Successful: {stats['successful']}/{stats['total']}")
        print(f"  Skipped: {stats['skipped']}/{stats['total']} (already exported)")
        print(f"  No documents: {stats['no_documents']}/{stats['total']}")
        print(f"  Failed: {stats['failed']}/{stats['total']}")
        print(f"  Output directory: {output_dir}")
        print("=" * 80)

    finally:
        engine.dispose()


def main():
    """Main entry point."""
    logging.basicConfig(
        filename="case_export.log",
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Export random court cases with documents from the scrapping database."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=100,
        help="Number of random cases to export (default: 100)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/cases"),
        help="Output directory for exported cases (default: data/cases)",
    )
    parser.add_argument(
        "--bucket",
        type=str,
        default="courts_crawl",
        help="GCS bucket name (default: courts_crawl)",
    )
    parser.add_argument(
        "--case-ids",
        type=str,
        help="Comma-separated list of specific case IDs to export (skips random sampling)",
    )
    parser.add_argument(
        "--case-ids-file",
        type=Path,
        help="Path to a file containing comma- or newline-separated case IDs to export "
        "(skips random sampling). Use this instead of --case-ids when the list is too "
        "long for the shell's argument limit.",
    )
    parser.add_argument(
        "--schema",
        type=str,
        default="courts_final",
        help="Postgres schema holding the crawl tables (default: courts_final)",
    )
    parser.add_argument(
        "--table-prefix",
        type=str,
        default="ny_",
        help="Per-state table prefix, e.g. 'ny_' or 'fl_' (default: ny_)",
    )
    parser.add_argument(
        "--extract-text",
        action="store_true",
        help="Also run Docling over every downloaded PDF and save a .txt "
        "counterpart (slow, GPU/CPU-heavy - off by default)",
    )
    parser.add_argument(
        "--no-gpu",
        action="store_true",
        help="Disable GPU acceleration for --extract-text",
    )
    parser.add_argument(
        "--download-files",
        action="store_true",
        help="Also download PDFs/confirmations from GCS (off by default - "
        "only the DB-sourced JSON metadata is written)",
    )
    parser.add_argument(
        "--extraction-dir",
        type=Path,
        default=Path("data/extraction"),
        help="Root directory for Docling output when --extract-text is set "
        "(default: data/extraction). Keep this parallel to --output-dir - "
        "e.g. --output-dir data/cases/ny_sample should pair with "
        "--extraction-dir data/extraction/ny_sample.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-fetch and overwrite cases whose JSON already exists instead "
        "of skipping them (default: skip). Use this to refresh a stale "
        "export against the DB's current state.",
    )

    args = parser.parse_args()

    print("=" * 80)
    print("Court Case Exporter")
    print("=" * 80)

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Get case IDs
    if args.case_ids_file:
        # Parse specific case IDs from a file (comma- or newline-separated)
        raw = args.case_ids_file.read_text()
        case_ids = [int(x.strip()) for x in raw.replace("\n", ",").split(",") if x.strip()]
        print(f"Exporting {len(case_ids)} specific cases from {args.case_ids_file}...")
    elif args.case_ids:
        # Parse specific case IDs
        case_ids = [int(x.strip()) for x in args.case_ids.split(",")]
        print(f"Exporting {len(case_ids)} specific cases...")
    else:
        # Get random sample
        print(f"Sampling {args.count} random cases from the database...")
        engine = create_scrapping_engine()
        try:
            case_ids = get_random_case_ids(
                engine, args.count, schema=args.schema, table_prefix=args.table_prefix
            )
        finally:
            engine.dispose()

        if not case_ids:
            print("Error: No cases found in the database.")
            sys.exit(1)

        print(f"Selected {len(case_ids)} cases for export.")

    # Export cases
    export_cases(
        case_ids,
        args.output_dir,
        args.bucket,
        schema=args.schema,
        table_prefix=args.table_prefix,
        extract_text=args.extract_text,
        use_gpu=not args.no_gpu,
        download_files=args.download_files,
        extraction_dir=args.extraction_dir,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()