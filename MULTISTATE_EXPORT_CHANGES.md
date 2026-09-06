# Multi-State Export System - Changes Summary

## Date: 2026-09-04

## Problem Discovered
Initially all PDF downloads were failing with 404 errors because:
1. **Wrong GCS bucket**: Code was using `court-docs` but PDFs are in `courts_crawl`
2. **Missing state prefix**: PDFs are stored with state prefixes (e.g., `ny/`, `fl/`, `ca/`)
3. **URL encoding confusion**: Initially thought we needed to decode paths, but GCS stores them URL-encoded

## Solutions Implemented

### 1. Updated GCS Bucket Configuration
**Files modified**:
- `lawsuit_parser/utils/case_exporter.py`
- `scripts/export_case.py`
- `scripts/export_random_cases.py`

**Changes**:
- Changed default bucket from `court-docs` to `courts_crawl`
- GCS bucket structure: `gs://courts_crawl/{state}/document_link/*.pdf`

### 2. Added State Prefix Support
**File**: `lawsuit_parser/utils/case_exporter.py`

**Changes**:
- Added `self.state_code` extraction from `table_prefix` (e.g., `"ny_"` → `"ny"`)
- Modified `download_from_gcs_to_file()` to prepend state code to blob paths
- Modified `download_from_gcs_to_bytes()` to prepend state code to blob paths

**Code added**:
```python
# Extract state code from table_prefix (e.g., "ny_" -> "ny")
self.state_code = table_prefix.rstrip("_") if table_prefix else ""

# In download methods:
if self.state_code:
    blob_name = f"{self.state_code}/{blob_name}"
```

### 3. Fixed Database Join Bug
**File**: `scripts/export_random_cases.py`

**Issue**: Query was joining on `case_id` instead of `docket_id`
- `case_id` is NOT unique across courts (e.g., "622075/2025" can exist in multiple counties)
- `docket_id` is the unique identifier per case

**Fixed**:
```python
# BEFORE (WRONG):
WHERE cd.case_id = cc.case_id

# AFTER (CORRECT):
WHERE cd.docket_id = cc.docket_id
```

### 4. Added State Prefix Support to Utility Scripts
**Files modified**:
- `scripts/check_additional_metadata.py`
- `scripts/show_case_urls.py`

**Changes**:
- Added `--table-prefix` command-line argument
- Made scripts work with any state (ny, fl, il, ca, etc.)

### 5. URL Encoding Handling
**File**: `lawsuit_parser/utils/gcs.py`

**Decision**: Keep paths URL-encoded (DO NOT decode)
- Database stores: `document_link/document_HhcQURsBeCTrTeIyWJZk1g%3D%3D.pdf`
- GCS stores: `ny/document_link/document_HhcQURsBeCTrTeIyWJZk1g%3D%3D.pdf`
- Both use URL encoding (`%3D%3D` = `==`)

## Testing Results

### Sample Export (5 cases)
Successfully exported all PDFs:
- **Case 95**: 62 documents + 62 confirmations = 124 PDFs ✅
- **Case 227**: 3 documents + 3 confirmations = 6 PDFs ✅
- **Case 309**: 28 documents + 28 confirmations = 56 PDFs ✅
- **Case 377**: 6 documents + 6 confirmations = 12 PDFs ✅
- **Case 2303**: 8 documents + 8 confirmations = 16 PDFs ✅

**Total**: 214 PDFs successfully downloaded (0 failures)

### Database Statistics
- **NY Total Cases**: 11,435 cases in `courts_final.ny_cases_after_search`
- **Case ID Range**: 1 to 12,459
- **Case IDs File**: `ny_case_ids.txt` (58KB)

## Multi-State Support

### Supported States
The system now supports all states with the following prefixes:
- **New York**: `ny_` → GCS path: `gs://courts_crawl/ny/`
- **Florida**: `fl_` → GCS path: `gs://courts_crawl/fl/`
- **Illinois**: `il_` → GCS path: `gs://courts_crawl/il/`
- **California**: `ca_` → GCS path: `gs://courts_crawl/ca/`

### Usage Examples

#### Export NY cases
```bash
uv run python scripts/export_case.py 227 \
  --output-dir data/cases/ny_sample \
  --table-prefix ny_
```

#### Export FL cases
```bash
uv run python scripts/export_case.py 100 \
  --output-dir data/cases/fl_sample \
  --table-prefix fl_
```

#### Export IL cases
```bash
uv run python scripts/export_random_cases.py \
  --count 50 \
  --output-dir data/cases/il_sample \
  --table-prefix il_
```

## Architecture

### Database Schema
```
courts_final.{state_prefix}cases_after_search
courts_final.{state_prefix}docket_documents
courts_final.{state_prefix}cases (history)
courts_final.{state_prefix}docket_documents_transcriptions
```

### GCS Structure
```
gs://courts_crawl/
├── ny/
│   ├── document_link/
│   │   └── document_{encoded_id}.pdf
│   └── confirmation/
│       └── document_{encoded_id}.pdf
├── fl/
│   ├── document_link/
│   └── confirmation/
├── il/
│   ├── document_link/
│   └── confirmation/
└── ca/
    ├── document_link/
    └── confirmation/
```

### Local Export Structure
```
data/cases/{state}_after_search/
└── case_{id}/
    ├── case_{id}.json          # Metadata + document info
    ├── documents/               # Main PDFs
    │   └── document_*.pdf
    └── confirmations/           # E-filing confirmations
        └── document_*.pdf
```

## Files Created/Modified

### New Files
- ✅ `scripts/get_all_case_ids.py` - Extract all case IDs from database
- ✅ `ny_case_ids.txt` - All 11,435 NY case IDs
- ✅ `NY_FULL_EXPORT_INSTRUCTIONS.md` - Full export documentation
- ✅ `MULTISTATE_EXPORT_CHANGES.md` - This summary document

### Modified Files
- ✅ `lawsuit_parser/utils/case_exporter.py` - Added state prefix support
- ✅ `lawsuit_parser/utils/gcs.py` - URL encoding handling
- ✅ `scripts/export_case.py` - Updated bucket default
- ✅ `scripts/export_random_cases.py` - Fixed join bug, updated bucket
- ✅ `scripts/check_additional_metadata.py` - Added state prefix support
- ✅ `scripts/show_case_urls.py` - Added state prefix support

## Backward Compatibility
✅ All changes are backward compatible:
- Default `table_prefix="ny_"` maintains existing behavior
- Default `gcs_bucket_name="courts_crawl"` is correct
- Scripts can still be run without `--table-prefix` flag (defaults to NY)

## Next Steps

### Ready to Execute
1. **Full NY Export**: See `NY_FULL_EXPORT_INSTRUCTIONS.md`
2. **Other States**: Modify `get_all_case_ids.py` for other states
3. **Batch Processing**: Consider implementing batch export for large datasets

### Future Enhancements
1. Add progress tracking with database persistence
2. Implement parallel downloads for faster exports
3. Add GCS rate limiting and retry logic
4. Create unified export script for all states
5. Add checksum validation for downloaded PDFs

## Performance Estimates

### Single Case Export
- Time: ~5-10 seconds per case
- Network: ~1-5 MB per case (varies widely)

### Full NY Export (11,435 cases)
- Estimated time: **19-24 hours**
- Estimated size: **50-100 GB**
- Estimated files: **~240,000 PDFs**

### Recommendations
- Run exports during off-peak hours
- Monitor disk space (need 150GB+ free)
- Use batch processing for large exports
- Consider rate limiting to avoid GCS throttling
