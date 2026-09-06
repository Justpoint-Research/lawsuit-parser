# NY Full Export Instructions

## Overview
This document describes how to export all 11,435 cases from the `courts_final.ny_cases_after_search` table.

## Preparation Completed
- ✅ All case IDs have been extracted and saved to `ny_case_ids.txt`
- ✅ Case exporter has been updated to use correct GCS bucket (`courts_crawl`)
- ✅ State prefix support added (`ny/` prepended to all blob paths)
- ✅ Multi-state table prefix support implemented

## Database Statistics
- **Total cases**: 11,435
- **Case ID range**: 1 to 12,459
- **Table**: `courts_final.ny_cases_after_search`
- **Output directory**: `data/cases/ny_after_search`

## Export Command

### Option 1: Using the case IDs file
```bash
case_ids=$(cat ny_case_ids.txt)
uv run python scripts/export_random_cases.py \
  --case-ids "$case_ids" \
  --output-dir data/cases/ny_after_search \
  --table-prefix ny_
```

### Option 2: Export in batches (recommended for large exports)
```bash
# Export first 1000 cases
head -c 5000 ny_case_ids.txt | uv run python scripts/export_random_cases.py \
  --case-ids "$(cat -)" \
  --output-dir data/cases/ny_after_search \
  --table-prefix ny_

# Or create a batch export script
python scripts/export_in_batches.py \
  --case-ids-file ny_case_ids.txt \
  --output-dir data/cases/ny_after_search \
  --batch-size 100
```

### Option 3: Run in background with nohup
```bash
nohup uv run python scripts/export_random_cases.py \
  --case-ids "$(cat ny_case_ids.txt)" \
  --output-dir data/cases/ny_after_search \
  --table-prefix ny_ \
  > ny_export.log 2>&1 &

# Monitor progress
tail -f ny_export.log
```

## Important Notes

### Storage Requirements
Based on the 5 sample cases:
- Case 95: 62 documents (largest sample)
- Average: ~21 documents per case
- Estimated total: ~240,000 PDF files (11,435 cases × 21 docs × 2 files per doc)
- Estimated size: ~50-100GB (assuming ~250KB average per PDF)

### Time Estimate
- Sample export: 5 cases took ~30 seconds
- Estimated time for 11,435 cases: **19-24 hours**
- Network/GCS rate limits may affect speed

### Recommendations
1. **Use batch processing** to avoid long-running single processes
2. **Monitor disk space** - ensure you have 150GB+ free
3. **Check GCS quotas** - you may hit API rate limits
4. **Run during off-hours** to avoid database load
5. **Enable progress logging** to track completion

## Files Generated
After export completes, you'll have:
```
data/cases/ny_after_search/
├── case_1/
│   ├── case_1.json
│   ├── documents/
│   │   └── *.pdf
│   └── confirmations/
│       └── *.pdf
├── case_2/
│   └── ...
└── case_12459/
    └── ...
```

## Resuming Failed Exports
The export script will skip cases that already exist (based on JSON file presence), so you can safely re-run the command to resume after failures:

```bash
# Re-running the same command will skip completed cases
uv run python scripts/export_random_cases.py \
  --case-ids "$(cat ny_case_ids.txt)" \
  --output-dir data/cases/ny_after_search \
  --table-prefix ny_
```

## Monitoring Progress
```bash
# Count exported cases
ls -1d data/cases/ny_after_search/case_* 2>/dev/null | wc -l

# Count total PDFs downloaded
find data/cases/ny_after_search -name "*.pdf" | wc -l

# Check disk usage
du -sh data/cases/ny_after_search
```

## Troubleshooting

### Out of disk space
- Export in smaller batches to a different location
- Delete temporary files: `find data/cases/ny_after_search -name "*.docling.json" -delete`

### GCS rate limits (429 errors)
- Add delays between requests (modify `CaseExporter._download_case_files`)
- Export in smaller batches with delays between batches

### Memory issues
- Reduce batch size
- Use the background export option

## Configuration Summary
- **GCS Bucket**: `courts_crawl`
- **State prefix**: `ny/`
- **Table prefix**: `ny_`
- **Schema**: `courts_final`
- **Database port**: 5433 (scrapping database)

## Next Steps for Other States
To export Florida, Illinois, or California cases:
```bash
# Florida
python scripts/get_all_case_ids_for_state.py --state fl
uv run python scripts/export_random_cases.py \
  --case-ids "$(cat fl_case_ids.txt)" \
  --output-dir data/cases/fl_after_search \
  --table-prefix fl_

# Illinois
python scripts/get_all_case_ids_for_state.py --state il
uv run python scripts/export_random_cases.py \
  --case-ids "$(cat il_case_ids.txt)" \
  --output-dir data/cases/il_after_search \
  --table-prefix il_

# California
python scripts/get_all_case_ids_for_state.py --state ca
uv run python scripts/export_random_cases.py \
  --case-ids "$(cat ca_case_ids.txt)" \
  --output-dir data/cases/ca_after_search \
  --table-prefix ca_
```
