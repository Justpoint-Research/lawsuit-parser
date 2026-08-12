#!/usr/bin/env python
"""
Parse a single PDF document and display structured content.

This is a simple example script demonstrating the PDF parsing functionality.

Usage:
    python scripts/parse_single_pdf.py path/to/document.pdf
    python scripts/parse_single_pdf.py path/to/document.pdf --output output.json
    python scripts/parse_single_pdf.py path/to/document.pdf --no-gpu
"""

import sys
from pathlib import Path

# Add parent directory to path to import lawsuit_parser
sys.path.insert(0, str(Path(__file__).parent.parent))

import click
from lawsuit_parser.parsers import parse_pdf_document, save_parsed_document


@click.command()
@click.argument('pdf_path', type=click.Path(exists=True))
@click.option(
    '--output', '-o',
    type=str,
    default=None,
    help='Output JSON file path (default: same as PDF with .json extension)'
)
@click.option(
    '--no-gpu',
    is_flag=True,
    help='Disable GPU acceleration'
)
@click.option(
    '--no-tables',
    is_flag=True,
    help='Disable table extraction'
)
@click.option(
    '--print-content',
    is_flag=True,
    help='Print extracted content to console'
)
def main(pdf_path: str, output: str | None, no_gpu: bool, no_tables: bool, print_content: bool):
    """Parse a single PDF document and extract structured content."""
    # Convert to Path objects
    pdf_path = Path(pdf_path)
    output_path = Path(output) if output else pdf_path.with_suffix('.json')

    try:
        click.echo(f"Parsing PDF: {pdf_path}")
        click.echo(f"Using GPU: {not no_gpu}")
        click.echo()

        # Parse the document
        parsed = parse_pdf_document(
            pdf_path,
            use_gpu=not no_gpu,
            extract_tables=not no_tables,
            extract_images=False,
        )

        # Print content if requested
        if print_content:
            click.echo("="*60)
            click.echo("EXTRACTED CONTENT")
            click.echo("="*60)
            click.echo()

            if parsed.title:
                click.echo(f"TITLE: {parsed.title}")
                click.echo()

            click.echo(f"PAGE COUNT: {parsed.page_count}")
            click.echo(f"PARAGRAPHS: {len(parsed.paragraphs)}")
            click.echo(f"TABLES: {len(parsed.tables)}")
            click.echo()

            if parsed.paragraphs:
                click.echo("-"*60)
                click.echo("PARAGRAPHS")
                click.echo("-"*60)
                for i, para in enumerate(parsed.paragraphs[:5], 1):
                    click.echo(f"\n[{i}] {para[:200]}{'...' if len(para) > 200 else ''}")

                if len(parsed.paragraphs) > 5:
                    click.echo(f"\n... and {len(parsed.paragraphs) - 5} more paragraphs")

            if parsed.tables:
                click.echo()
                click.echo("-"*60)
                click.echo(f"TABLES: {len(parsed.tables)} extracted")
                click.echo("-"*60)

            click.echo()

        # Save to JSON
        save_parsed_document(parsed, output_path, indent=2)
        click.echo(f"✓ Saved parsed content to: {output_path}")

        # Print summary
        click.echo()
        click.echo("Summary:")
        click.echo(f"  - Title: {parsed.title or 'N/A'}")
        click.echo(f"  - Pages: {parsed.page_count}")
        click.echo(f"  - Paragraphs: {len(parsed.paragraphs)}")
        click.echo(f"  - Tables: {len(parsed.tables)}")
        click.echo(f"  - Total text length: {len(parsed.raw_text)} characters")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
