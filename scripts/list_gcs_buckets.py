#!/usr/bin/env python3
"""List GCS buckets to help identify the correct bucket for court documents.

Usage:
    python scripts/list_gcs_buckets.py
    python scripts/list_gcs_buckets.py --bucket BUCKET_NAME --list-files
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click
from google.cloud import storage
from google.auth import default


@click.group()
def cli():
    """GCS bucket utilities for lawsuit-parser."""
    pass


@cli.command()
def list_buckets():
    """List all GCS buckets in the project."""
    try:
        credentials, project = default()
        client = storage.Client(credentials=credentials, project=project)

        click.echo(f"Project: {project}")
        click.echo("\nAvailable buckets:")
        click.echo("-" * 80)

        buckets = list(client.list_buckets())

        if not buckets:
            click.echo("No buckets found.")
            return

        for bucket in buckets:
            click.echo(f"  📦 {bucket.name}")
            click.echo(f"     Created: {bucket.time_created}")
            click.echo(f"     Location: {bucket.location}")
            click.echo()

        click.echo(f"\nTotal: {len(buckets)} buckets")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        click.echo("\n💡 Make sure you're authenticated:", err=True)
        click.echo("   gcloud auth application-default login", err=True)
        sys.exit(1)


@cli.command()
@click.option('--bucket', '-b', required=True, help='Bucket name')
@click.option('--prefix', '-p', default='', help='Filter by prefix')
@click.option('--limit', '-l', default=20, help='Max files to list')
def list_files(bucket: str, prefix: str, limit: int):
    """List files in a GCS bucket."""
    try:
        credentials, project = default()
        client = storage.Client(credentials=credentials, project=project)

        click.echo(f"Listing files in gs://{bucket}/{prefix or ''}")
        click.echo("-" * 80)

        bucket_obj = client.bucket(bucket)
        blobs = bucket_obj.list_blobs(prefix=prefix, max_results=limit)

        count = 0
        for blob in blobs:
            count += 1
            size_mb = blob.size / (1024 * 1024)
            click.echo(f"{count:3}. {blob.name}")
            click.echo(f"      Size: {size_mb:.2f} MB | Updated: {blob.updated}")

        if count == 0:
            click.echo("No files found.")
        else:
            click.echo(f"\nShowing {count} files")
            if count >= limit:
                click.echo(f"(Limited to {limit}. Use --limit to see more)")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option('--bucket', '-b', required=True, help='Bucket name')
@click.option('--search', '-s', required=True, help='Search term in file path')
def find_files(bucket: str, search: str):
    """Find files matching a search term."""
    try:
        credentials, project = default()
        client = storage.Client(credentials=credentials, project=project)

        click.echo(f"Searching for '{search}' in gs://{bucket}/")
        click.echo("-" * 80)

        bucket_obj = client.bucket(bucket)
        blobs = bucket_obj.list_blobs()

        count = 0
        for blob in blobs:
            if search.lower() in blob.name.lower():
                count += 1
                size_mb = blob.size / (1024 * 1024)
                click.echo(f"{count}. gs://{bucket}/{blob.name}")
                click.echo(f"   Size: {size_mb:.2f} MB | Updated: {blob.updated}")

        if count == 0:
            click.echo(f"No files found matching '{search}'")
        else:
            click.echo(f"\nFound {count} matching files")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
def check_auth():
    """Check GCS authentication status."""
    try:
        credentials, project = default()
        client = storage.Client(credentials=credentials, project=project)

        click.echo("✓ GCS Authentication successful!")
        click.echo(f"  Project: {project}")
        click.echo(f"  Credentials: {type(credentials).__name__}")

        # Try to list one bucket to verify access
        buckets = list(client.list_buckets(max_results=1))
        if buckets:
            click.echo(f"  Access verified (can list buckets)")
        else:
            click.echo(f"  No buckets found in project")

    except Exception as e:
        click.echo("✗ GCS Authentication failed", err=True)
        click.echo(f"  Error: {e}", err=True)
        click.echo("\n💡 To authenticate, run:", err=True)
        click.echo("   gcloud auth application-default login", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
