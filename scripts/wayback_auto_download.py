#!/usr/bin/env python3
"""
Automated Wayback Machine File Retriever
Automatically downloads archived files and updates the source file with local paths.
"""

import json
import time
import sys
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote
from urllib.request import urlopen, Request, urlretrieve
from urllib.error import URLError, HTTPError
from datetime import datetime

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    print("Note: Install tqdm for progress bars: pip install tqdm")
    print()


class WaybackAutoDownloader:
    def __init__(self, source_file, download_dir="archived_files", delay=2.0, resume=True, use_tqdm=True):
        """
        Initialize the auto-downloader.

        Args:
            source_file: Path to the file containing URLs
            download_dir: Directory to save downloaded files
            delay: Delay in seconds between API requests
            resume: Whether to resume from previous progress
            use_tqdm: Whether to use tqdm progress bars
        """
        self.source_file = Path(source_file)
        self.download_dir = Path(download_dir)
        self.delay = delay
        self.api_url = "http://archive.org/wayback/available"
        self.use_tqdm = use_tqdm and HAS_TQDM

        # Create download directory
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Progress tracking
        self.progress_file = self.download_dir / ".progress.json"
        self.log_file = self.download_dir / "download_log.txt"
        self.resume = resume

        # Statistics
        self.stats = {
            'total_urls': 0,
            'checked': 0,
            'found': 0,
            'downloaded': 0,
            'not_found': 0,
            'errors': 0
        }

        # Progress bar
        self.pbar = None

        # Load previous progress if resuming
        self.processed_urls = set()
        if resume and self.progress_file.exists():
            with open(self.progress_file, 'r') as f:
                data = json.load(f)
                self.processed_urls = set(data.get('processed_urls', []))
                self.stats.update(data.get('stats', {}))
                self.log(f"Resuming from previous session. Already processed: {len(self.processed_urls)} URLs")

    def log(self, message, also_print=True):
        """Log a message to the log file and optionally print it."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        with open(self.log_file, 'a') as f:
            f.write(log_message + "\n")

        if also_print and not self.use_tqdm:
            print(log_message)
        elif also_print and self.pbar:
            self.pbar.write(log_message)

    def save_progress(self):
        """Save current progress to a file."""
        with open(self.progress_file, 'w') as f:
            json.dump({
                'processed_urls': list(self.processed_urls),
                'stats': self.stats,
                'last_updated': datetime.now().isoformat()
            }, f, indent=2)

    def check_archive(self, url):
        """
        Check if a URL is available in the Wayback Machine.

        Args:
            url: The URL to check

        Returns:
            dict with 'available' (bool), 'archive_url' (str), and 'timestamp' (str)
        """
        try:
            # Clean up double-encoded URLs
            clean_url = url.replace('%2520', '%20')

            api_request = f"{self.api_url}?url={quote(clean_url, safe=':/')}"

            with urlopen(api_request, timeout=15) as response:
                data = json.loads(response.read().decode('utf-8'))

            if 'archived_snapshots' in data and 'closest' in data['archived_snapshots']:
                snapshot = data['archived_snapshots']['closest']
                return {
                    'available': snapshot.get('available', False),
                    'archive_url': snapshot.get('url', ''),
                    'timestamp': snapshot.get('timestamp', ''),
                    'status': snapshot.get('status', '')
                }
            else:
                return {'available': False, 'archive_url': '', 'timestamp': '', 'status': ''}

        except HTTPError as e:
            if e.code == 429:
                self.log(f"⚠️  Rate limit hit, waiting 10 seconds...")
                time.sleep(10)
                return self.check_archive(url)  # Retry
            else:
                self.log(f"HTTP Error {e.code}: {url}")
                return {'available': False, 'error': str(e)}
        except Exception as e:
            self.log(f"Error checking {url}: {e}")
            return {'available': False, 'error': str(e)}

    def sanitize_filename(self, url):
        """
        Extract and sanitize filename from URL.

        Args:
            url: The original URL

        Returns:
            A safe filename
        """
        # Extract filename from URL
        filename = unquote(url.split('/')[-1])

        # Remove or replace invalid characters
        filename = re.sub(r'[<>:"|?*]', '_', filename)

        # Ensure it ends with .pdf if it doesn't already
        if not filename.lower().endswith('.pdf'):
            filename += '.pdf'

        return filename

    def download_file(self, archive_url, filename):
        """
        Download a file from the Wayback Machine.

        Args:
            archive_url: The Wayback Machine URL
            filename: Filename to save as

        Returns:
            Path to downloaded file or None if failed
        """
        try:
            output_path = self.download_dir / filename

            # Skip if already exists
            if output_path.exists():
                self.log(f"✓ File already exists: {filename}", also_print=False)
                return output_path

            self.log(f"  Downloading: {filename}...", also_print=False)
            urlretrieve(archive_url, output_path)

            # Verify file was downloaded
            if output_path.exists() and output_path.stat().st_size > 0:
                size_kb = output_path.stat().st_size / 1024
                self.log(f"  ✓ Downloaded: {filename} ({size_kb:.1f} KB)", also_print=False)
                return output_path
            else:
                self.log(f"  ✗ Download failed (empty file): {filename}")
                if output_path.exists():
                    output_path.unlink()
                return None

        except Exception as e:
            self.log(f"  ✗ Error downloading {filename}: {e}")
            return None

    def process_file(self):
        """
        Main processing function. Reads file, checks archives, downloads, and updates file.
        """
        # Read the entire file
        with open(self.source_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Count total URLs to process
        urls_to_process = [line for line in lines if line.strip().startswith('https://mdl.law.uga.edu')]
        self.stats['total_urls'] = len(urls_to_process)

        self.log(f"\n{'='*80}")
        self.log(f"Starting Wayback Machine Auto-Download")
        self.log(f"{'='*80}")
        self.log(f"Source file: {self.source_file}")
        self.log(f"Download directory: {self.download_dir}")
        self.log(f"Total URLs to process: {self.stats['total_urls']}")
        self.log(f"Already processed: {len(self.processed_urls)}")
        self.log(f"Delay between requests: {self.delay}s")
        self.log(f"{'='*80}\n")

        # URL to local path mapping
        url_replacements = {}

        # Calculate remaining URLs to process
        remaining_urls = [url.strip() for url in lines
                         if url.strip().startswith('https://mdl.law.uga.edu')
                         and url.strip() not in self.processed_urls]

        # Create progress bar
        if self.use_tqdm:
            self.pbar = tqdm(
                total=len(remaining_urls),
                desc="Processing URLs",
                unit="url",
                postfix={'found': 0, 'downloaded': 0}
            )

        # Process each line
        updated_lines = []
        for i, line in enumerate(lines, 1):
            url = line.strip()

            # If it's not a URL or already processed, keep as-is
            if not url.startswith('https://mdl.law.uga.edu'):
                updated_lines.append(line)
                continue

            # Skip if already processed
            if url in self.processed_urls:
                # Check if we already have a replacement for this URL
                if url in url_replacements:
                    updated_lines.append(url_replacements[url] + '\n')
                else:
                    updated_lines.append(line)
                continue

            self.stats['checked'] += 1
            progress = f"[{self.stats['checked']}/{self.stats['total_urls']}]"

            if not self.use_tqdm:
                self.log(f"\n{progress} Processing URL {self.stats['checked']}:")
                self.log(f"  URL: {url[:80]}{'...' if len(url) > 80 else ''}")

            # Update progress bar description
            if self.pbar:
                filename_short = url.split('/')[-1][:40]
                self.pbar.set_description(f"Processing: {filename_short}...")

            # Check Wayback Machine
            result = self.check_archive(url)

            if result.get('available'):
                self.stats['found'] += 1
                timestamp = result['timestamp']
                formatted_date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"

                if not self.use_tqdm:
                    self.log(f"  ✓ Found in archive (date: {formatted_date})")

                # Download the file
                filename = self.sanitize_filename(url)
                local_path = self.download_file(result['archive_url'], filename)

                if local_path:
                    self.stats['downloaded'] += 1

                    # Update progress bar postfix
                    if self.pbar:
                        self.pbar.set_postfix(found=self.stats['found'], downloaded=self.stats['downloaded'])
                    # Create relative path from source file location
                    try:
                        rel_path = os.path.relpath(local_path, self.source_file.parent)
                        url_replacements[url] = rel_path
                        updated_lines.append(rel_path + '\n')
                        if not self.use_tqdm:
                            self.log(f"  ✓ Updated to local path: {rel_path}")
                    except ValueError:
                        # If relative path fails, use absolute path
                        url_replacements[url] = str(local_path)
                        updated_lines.append(str(local_path) + '\n')
                        if not self.use_tqdm:
                            self.log(f"  ✓ Updated to absolute path: {local_path}")
                else:
                    self.stats['errors'] += 1
                    updated_lines.append(line)  # Keep original URL if download failed
                    if not self.use_tqdm:
                        self.log(f"  ✗ Download failed, keeping original URL")
            else:
                self.stats['not_found'] += 1
                updated_lines.append(line)  # Keep original URL
                error_msg = result.get('error', 'Not found in archive')
                if not self.use_tqdm:
                    self.log(f"  ✗ {error_msg}")

            # Mark as processed
            self.processed_urls.add(url)

            # Update progress bar
            if self.pbar:
                self.pbar.update(1)

            # Save progress periodically
            if self.stats['checked'] % 10 == 0:
                self.save_progress()
                # Save intermediate file update
                backup_file = self.source_file.with_suffix('.txt.backup')
                with open(backup_file, 'w', encoding='utf-8') as f:
                    f.writelines(updated_lines)

            # Progress summary (only if not using tqdm)
            if self.stats['checked'] % 20 == 0 and not self.use_tqdm:
                self.print_stats()

            # Be respectful to the API
            if self.stats['checked'] < self.stats['total_urls']:
                time.sleep(self.delay)

        # Close progress bar
        if self.pbar:
            self.pbar.close()

        # Write updated file
        self.log(f"\n{'='*80}")
        self.log(f"Updating source file...")

        # Create backup of original file
        backup_file = self.source_file.with_suffix('.txt.backup')
        if not backup_file.exists():
            with open(self.source_file, 'r', encoding='utf-8') as f_in:
                with open(backup_file, 'w', encoding='utf-8') as f_out:
                    f_out.write(f_in.read())
            self.log(f"✓ Created backup: {backup_file}")

        # Write updated file
        with open(self.source_file, 'w', encoding='utf-8') as f:
            f.writelines(updated_lines)

        self.log(f"✓ Updated source file: {self.source_file}")
        self.save_progress()

        # Final statistics
        self.log(f"\n{'='*80}")
        self.log(f"FINAL REPORT")
        self.log(f"{'='*80}")
        self.print_stats()
        self.log(f"{'='*80}")
        self.log(f"\nFiles saved to: {self.download_dir}")
        self.log(f"Log file: {self.log_file}")
        self.log(f"Original file backed up to: {backup_file}")

    def print_stats(self):
        """Print current statistics."""
        self.log(f"Total URLs:      {self.stats['total_urls']}")
        self.log(f"Checked:         {self.stats['checked']}")
        self.log(f"Found:           {self.stats['found']} ({self.stats['found']/max(1,self.stats['checked'])*100:.1f}%)")
        self.log(f"Downloaded:      {self.stats['downloaded']}")
        self.log(f"Not found:       {self.stats['not_found']}")
        self.log(f"Errors:          {self.stats['errors']}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Automatically download files from Wayback Machine and update source file',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python wayback_auto_download.py -f MDL_summaries.txt -d archived_files --delay 2.5

This will:
  1. Read MDL_summaries.txt
  2. Find all https://mdl.law.uga.edu URLs
  3. Check each URL in the Wayback Machine
  4. Download available files to archived_files/
  5. Update MDL_summaries.txt with local file paths
  6. Create a backup of the original file
  7. Generate a detailed log

Features:
  - Automatic resume if interrupted (use --no-resume to start fresh)
  - Progress saved every 10 URLs
  - Rate limiting to respect Wayback Machine API
  - Detailed logging
  - Original file backup
        """
    )

    parser.add_argument('-f', '--file', required=True, help='Source file with URLs')
    parser.add_argument('-d', '--download-dir', default='archived_files', help='Directory for downloaded files (default: archived_files)')
    parser.add_argument('--delay', type=float, default=2.0, help='Delay between API requests in seconds (default: 2.0)')
    parser.add_argument('--no-resume', action='store_true', help='Start fresh instead of resuming')
    parser.add_argument('--no-progress-bar', action='store_true', help='Disable tqdm progress bar')

    args = parser.parse_args()

    # Validate source file exists
    if not Path(args.file).exists():
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    # Create and run downloader
    downloader = WaybackAutoDownloader(
        source_file=args.file,
        download_dir=args.download_dir,
        delay=args.delay,
        resume=not args.no_resume,
        use_tqdm=not args.no_progress_bar
    )

    try:
        downloader.process_file()
        print(f"\n✓ Process completed successfully!")
        print(f"  Downloaded files: {downloader.download_dir}")
        print(f"  Log file: {downloader.log_file}")
    except KeyboardInterrupt:
        print(f"\n\n⚠️  Process interrupted by user")
        print(f"Progress has been saved. Run the script again to resume.")
        downloader.save_progress()
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        downloader.log(f"Fatal error: {e}")
        downloader.save_progress()
        sys.exit(1)


if __name__ == '__main__':
    main()
