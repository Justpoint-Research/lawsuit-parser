"""Google Cloud Storage utilities for downloading court documents."""

from google.cloud import storage
from google.auth import default
from urllib.parse import urlparse, unquote


def get_storage_client() -> storage.Client:
    """Get a Google Cloud Storage client.

    Returns:
        Authenticated GCS client.

    Raises:
        Exception: If authentication fails.
    """
    return storage.Client()


def extract_blob_name(gcs_path: str) -> str | None:
    """Extract blob name from various GCS path formats.

    Handles:
    - document_link/document_...
    - confirmation/document_...
    - gs://bucket-name/path/to/file
    - https://storage.googleapis.com/bucket-name/path/to/file

    Args:
        gcs_path: GCS path in various formats.

    Returns:
        Blob name or None if not extractable.
    """
    if not gcs_path:
        return None

    # Handle gs:// URLs
    if gcs_path.startswith("gs://"):
        # Remove gs://bucket-name/ prefix
        parts = gcs_path[5:].split("/", 1)
        if len(parts) > 1:
            return parts[1]
        return None

    # Handle https://storage.googleapis.com URLs
    if "storage.googleapis.com" in gcs_path:
        parsed = urlparse(gcs_path)
        # Path is /bucket-name/blob-path
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) > 1:
            return path_parts[1]
        return None

    # Handle relative paths (document_link/..., confirmation/...)
    # Database stores paths exactly as they appear in GCS (URL-encoded)
    return gcs_path


def download_from_gcs(
    bucket_name: str, gcs_path: str, client: storage.Client | None = None
) -> bytes:
    """Download a file from GCS and return as bytes.

    Args:
        bucket_name: Name of the GCS bucket.
        gcs_path: GCS path (can be URL, gs:// path, or relative path).
        client: Optional GCS client. If not provided, a new one will be created.

    Returns:
        File contents as bytes.

    Raises:
        FileNotFoundError: If the file does not exist in GCS.
        Exception: For other GCS errors.
    """
    if client is None:
        client = get_storage_client()

    # Extract blob name from GCS path
    blob_name = extract_blob_name(gcs_path)

    if not blob_name:
        raise ValueError(f"Could not extract blob name from {gcs_path}")

    # Get bucket and blob
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    # Check if blob exists
    if not blob.exists():
        raise FileNotFoundError(
            f"File not found in GCS: gs://{bucket_name}/{blob_name}"
        )

    # Download and return bytes
    return blob.download_as_bytes()


def try_download_from_buckets(
    gcs_path: str, bucket_names: list[str], client: storage.Client | None = None
) -> tuple[bytes | None, str | None, str | None]:
    """Try downloading a file from multiple bucket names.

    This is useful when the bucket name is unknown but you have a list of
    candidates to try.

    Args:
        gcs_path: GCS path (relative path or full URL).
        bucket_names: List of bucket names to try.
        client: Optional GCS client. If not provided, a new one will be created.

    Returns:
        Tuple of (file_bytes, successful_bucket_name, error_message).
        If successful, file_bytes and successful_bucket_name will be set, error_message will be None.
        If failed, file_bytes and successful_bucket_name will be None, error_message will contain the last error.
    """
    if client is None:
        client = get_storage_client()

    last_error = None

    for bucket_name in bucket_names:
        try:
            file_bytes = download_from_gcs(bucket_name, gcs_path, client)
            return file_bytes, bucket_name, None
        except FileNotFoundError as e:
            last_error = str(e)
            continue
        except Exception as e:
            last_error = str(e)
            continue

    return None, None, last_error or "File not found in any of the specified buckets"


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
