#!/usr/bin/env python3
"""Get the total size of a GCS bucket."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.cloud import storage
from google.auth import default

def get_bucket_size(bucket_name: str):
    """Calculate total size of all objects in a bucket."""
    try:
        credentials, project = default()
        client = storage.Client(credentials=credentials, project=project)

        bucket = client.bucket(bucket_name)

        print(f"Analyzing bucket: gs://{bucket_name}")
        print("=" * 80)

        total_size = 0
        file_count = 0

        # Count by prefix
        prefixes = {}

        print("\nScanning all objects...")
        blobs = bucket.list_blobs()

        for blob in blobs:
            total_size += blob.size
            file_count += 1

            # Track size by prefix (directory)
            prefix = blob.name.split('/')[0] if '/' in blob.name else 'root'
            if prefix not in prefixes:
                prefixes[prefix] = {'size': 0, 'count': 0}
            prefixes[prefix]['size'] += blob.size
            prefixes[prefix]['count'] += 1

            if file_count % 1000 == 0:
                print(f"  Processed {file_count:,} files... ({total_size / (1024**3):.2f} GB)")

        print("\n" + "=" * 80)
        print(f"TOTAL FILES: {file_count:,}")
        print(f"TOTAL SIZE:  {total_size / (1024**3):.2f} GB ({total_size / (1024**2):.2f} MB)")
        print("=" * 80)

        print("\nBreakdown by directory:")
        print("-" * 80)
        for prefix, stats in sorted(prefixes.items(), key=lambda x: x[1]['size'], reverse=True):
            size_gb = stats['size'] / (1024**3)
            size_mb = stats['size'] / (1024**2)
            if size_gb > 1:
                print(f"  {prefix:30} {stats['count']:>8,} files  {size_gb:>8.2f} GB")
            else:
                print(f"  {prefix:30} {stats['count']:>8,} files  {size_mb:>8.2f} MB")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    get_bucket_size("court-docs")
