#!/usr/bin/env python3
"""Get the total size of a GCS bucket."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lawsuit_parser.utils.gcs import get_bucket_size

if __name__ == "__main__":
    get_bucket_size("court-docs")
