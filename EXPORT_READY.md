# Export Ready - NY Full Case Export

## Quick Start

Everything is prepared and ready for the full NY export. **Do not run yet** - just use this guide when ready.

## Files Prepared

1. **ny_case_ids.txt** - All 11,435 case IDs (58KB)
2. **export_ny_all_cases.sh** - Ready-to-run export script
3. **clean_partial_export.sh** - Clean up partial exports (if needed)
4. **NY_FULL_EXPORT_INSTRUCTIONS.md** - Detailed documentation
5. **MULTISTATE_EXPORT_CHANGES.md** - Technical changes summary

## Current State

- **Cases already exported**: 107 cases (663 MB)
- **Remaining cases**: 11,328 cases
- **Total to export**: 11,435 cases

## Resume Capability

**The export script is fully resumable:**
- ✅ Skips cases where the JSON file already exists
- ✅ Skips PDFs that are already downloaded
- ✅ Safe to stop and restart at any time

This means you can:
- Stop the export with `Ctrl+C` or `pkill`
- Rerun the same command to continue where it left off
- No duplicate downloads or wasted work

## When Ready to Export

### Option 1: Resume Export (Recommended)
Will automatically skip the 107 already exported cases and continue:

```bash
bash export_ny_all_cases.sh
```

### Option 2: Start Fresh
Clean partial exports and start from scratch:

```bash
bash clean_partial_export.sh
bash export_ny_all_cases.sh
```

### Option 3: Manual Command
Run the export command directly:

```bash
nohup uv run python scripts/export_random_cases.py \
  --case-ids "$(cat ny_case_ids.txt)" \
  --output-dir data/cases/ny_after_search \
  --table-prefix ny_ \
  > ny_export.log 2>&1 &
```

## Monitor Progress

### Live Progress Bar (New!)

The export now shows a **tqdm progress bar with ETA**:

```
Exporting cases: 42%|████████████▌             | 4805/11435 [02:15<03:07, 35.3case/s] ✓4612 ⊘193 ✗0
```

Shows:
- **42%** - Progress percentage
- **4805/11435** - Current/Total cases
- **02:15<03:07** - Elapsed < Remaining time (ETA!)
- **35.3case/s** - Export speed
- **✓4612** - Successfully exported
- **⊘193** - Skipped (already existed)
- **✗0** - Failed

See **PROGRESS_BAR.md** for details.

### Manual Progress Check

While export is running:

```bash
# Count completed cases
ls -1d data/cases/ny_after_search/case_* | wc -l

# Check disk usage
du -sh data/cases/ny_after_search

# Count PDFs downloaded
find data/cases/ny_after_search -name "*.pdf" | wc -l
```

## Stop Export

If you need to stop:

```bash
pkill -f "export_random_cases.py"
```

## Estimates

- **Time**: 9-10 hours (based on current rate)
- **Size**: 50-100 GB
- **Files**: ~240,000 PDFs
- **Rate**: ~20 cases/minute

## Requirements

- **Disk space**: Ensure 150+ GB free
- **Database**: Cloud SQL Proxy running (port 5433)
- **GCS**: Authenticated with gcloud
- **Bucket**: courts_crawl (already configured)
- **State**: ny/ prefix (already configured)

## Export Structure

```
data/cases/ny_after_search/
├── case_1/
│   ├── case_1.json          # Metadata
│   ├── documents/            # Main PDFs
│   │   └── document_*.pdf
│   └── confirmations/        # E-filing confirmations
│       └── document_*.pdf
├── case_2/
...
└── case_12459/
```

## Multi-State Export

For other states (when ready):

```bash
# Florida
python scripts/get_all_case_ids.py --state fl
bash export_fl_all_cases.sh

# Illinois
python scripts/get_all_case_ids.py --state il
bash export_il_all_cases.sh

# California
python scripts/get_all_case_ids.py --state ca
bash export_ca_all_cases.sh
```

## Architecture Verified

- ✅ GCS bucket: `courts_crawl` (correct)
- ✅ State prefix: `ny/` (working)
- ✅ URL encoding: Preserved (correct)
- ✅ Database joins: Using `docket_id` (fixed)
- ✅ Sample export: 5 cases, 214 PDFs successful
- ✅ Partial export: 107 cases, 663 MB successful

## Support

For detailed information:
- See **NY_FULL_EXPORT_INSTRUCTIONS.md**
- See **MULTISTATE_EXPORT_CHANGES.md**
