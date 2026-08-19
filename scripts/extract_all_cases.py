#!/usr/bin/env python3
"""Extract events from all parsed lawsuit cases.

This script finds all cases with .docling.json documents and runs
the extraction pipeline (stages 0-4) on each one, showing a progress bar.

Usage:
    uv run scripts/extract_all_cases.py
    uv run scripts/extract_all_cases.py --force
    uv run scripts/extract_all_cases.py --data-root data/cases
"""

import subprocess
import sys
from pathlib import Path

import click
from tqdm import tqdm


def find_cases_with_documents(data_root: Path) -> list[str]:
    """Find all case directories that have .docling.json documents.

    Args:
        data_root: Root directory containing case folders

    Returns:
        Sorted list of case IDs
    """
    if not data_root.exists():
        return []

    cases = []
    for case_dir in data_root.iterdir():
        if not case_dir.is_dir():
            continue

        # Check documents/ subdirectory
        docs_dir = case_dir / "documents"
        has_docs = False

        if docs_dir.exists() and list(docs_dir.glob("*.docling.json")):
            has_docs = True
        # Check flat structure (legacy)
        elif list(case_dir.glob("*.docling.json")):
            has_docs = True

        if has_docs:
            cases.append(case_dir.name)

    return sorted(cases)


@click.command()
@click.option(
    "--data-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default="data/cases",
    help="Root directory containing case folders",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force re-run of existing extraction stages",
)
@click.option(
    "--stages",
    default="0-4",
    help="Stages to run (e.g., '0-4', '2', '1,3')",
)
def main(data_root: Path, force: bool, stages: str):
    """Extract events from all parsed lawsuit cases with a progress bar."""

    # Find all cases with documents
    click.echo(f"Searching for cases in {data_root}")
    cases = find_cases_with_documents(data_root)

    if not cases:
        click.echo(f"No cases with documents found in {data_root}", err=True)
        sys.exit(1)

    click.echo(f"Found {len(cases)} cases with documents")
    if force:
        click.echo("Force re-run enabled")
    click.echo()

    # Run extraction on each case
    failed = []

    for case_id in tqdm(cases, desc="Extracting cases", unit="case"):
        cmd = [
            "uv", "run", "scripts/extract.py",
            "--case-id", case_id,
            "--stages", stages,
        ]
        if force:
            cmd.append("--force")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            failed.append((case_id, result.stderr))

    # Summary
    click.echo()
    click.echo("=" * 80)

    if failed:
        click.echo(f"✗ {len(failed)} case(s) failed:", err=True)
        for case_id, error in failed:
            # Show first line of error
            error_line = error.split("\n")[0] if error else "Unknown error"
            click.echo(f"  ✗ {case_id}: {error_line[:80]}", err=True)
        click.echo(f"\n✓ {len(cases) - len(failed)} case(s) succeeded")
        sys.exit(1)
    else:
        click.echo(f"✓ All {len(cases)} cases extracted successfully")


if __name__ == "__main__":
    main()