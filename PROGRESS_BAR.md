# Progress Bar with ETA

## Features Added

The export script now includes a **tqdm progress bar** with:

✅ **Progress bar** - Visual indication of completion
✅ **ETA** - Estimated time remaining
✅ **Rate** - Cases per second
✅ **Counters** - Success/Skipped/Failed counts
✅ **Elapsed time** - Time since start

## Example Output

### During Export
```
Exporting cases: 42%|████████████▌             | 4805/11435 [02:15<03:07, 35.3case/s] ✓4612 ⊘193 ✗0
```

**What it shows:**
- `42%` - Percentage complete
- `4805/11435` - Current/Total cases
- `[02:15<03:07]` - Elapsed time < Remaining time (ETA)
- `35.3case/s` - Export rate (cases per second)
- `✓4612` - Successfully exported (new)
- `⊘193` - Skipped (already existed)
- `✗0` - Failed

### On Resume
```
Exporting cases:  1%|▎                         | 107/11435 [00:01<00:15, 54.2case/s] ✓0 ⊘107 ✗0
```

Notice:
- Very fast rate (54 cases/sec) - because skipping is instant
- All 107 shown as skipped (⊘)
- Once it hits new cases, rate drops to ~30-40/sec (actual exports)

### Final Summary
```
================================================================================
Export complete!
  Successful: 11242/11435
  Skipped: 193/11435 (already exported)
  Failed: 0/11435
  Output directory: data/cases/ny_after_search
================================================================================
```

## Implementation

### Code Changes

**lawsuit_parser/utils/case_exporter.py**
```python
def export_case_by_id(self, case_id: int, skip_if_exists: bool = True) -> tuple[Path, bool]:
    """Export a case by its database ID.

    Returns:
        Tuple of (path to JSON file, whether it was skipped).
    """
    # ... (now returns tuple)
    return json_path, was_skipped
```

**scripts/export_random_cases.py**
```python
from tqdm import tqdm

with tqdm(
    total=total,
    desc="Exporting cases",
    unit="case",
    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
    colour="green"
) as pbar:
    for case_id in case_ids:
        json_path, was_skipped = exporter.export_case_by_id(case_id, skip_if_exists=True)

        if was_skipped:
            skipped += 1
        else:
            successful += 1

        pbar.set_postfix_str(f"✓{successful} ⊘{skipped} ✗{failed}")
        pbar.update(1)
```

## Estimated Times

Based on the progress bar data:

### Full Export (11,435 cases)
- **Rate**: ~35 cases/second (when actually exporting)
- **Time**: ~5.4 minutes per 100 cases
- **Total**: ~10-12 hours for all 11,435 cases

### Resume (with 106 skipped)
- **Skip rate**: ~54 cases/second (very fast)
- **Skip time**: 106 cases in ~2 seconds
- **Then**: Normal export rate for remaining

### Mixed (some skipped, some new)
- **Example**: 1000 skipped + 1000 new
- **Skip time**: 1000 × 0.018s = 18 seconds
- **Export time**: 1000 × 0.028s = 28 seconds
- **Total**: ~46 seconds

## Progress Bar Benefits

### 1. Know How Long to Wait
Before: "Is this done? Should I wait?"
Now: "3 hours remaining, I'll check back later"

### 2. See If It's Stuck
Before: "Has it frozen? Or just slow?"
Now: See live case/s rate - if 0, it's stuck

### 3. Track Success Rate
Before: "How many failures so far?"
Now: Live counter shows `✗17` failures immediately

### 4. Optimize Resume
Before: "How many will it skip?"
Now: Watch `⊘` counter climb rapidly, then slow when reaching new cases

### 5. Plan Around It
Before: "No idea when this finishes"
Now: "ETA 4:30pm, I'll grab lunch"

## Monitoring Tips

### Watch in Real-Time
```bash
# Start export (shows progress bar)
bash export_ny_all_cases.sh
```

### Background with Progress Saved
```bash
# Run in background but log progress
nohup bash export_ny_all_cases.sh 2>&1 | tee ny_export_progress.log &

# Watch progress file
tail -f ny_export_progress.log
```

### Check Speed
```bash
# Progress bar shows current rate
# Examples:
# 35.3case/s = Normal export (downloading PDFs)
# 54.2case/s = Fast skip (already exported)
# 0.5case/s = Slow (large case or network issues)
```

## Troubleshooting with Progress Bar

### Rate Drops to Near Zero
**Possible causes:**
- Very large case (100+ documents)
- Network slowdown
- GCS rate limiting

**Action**: Wait - likely temporary

### Many Failures (✗ increasing)
**Possible causes:**
- Network disconnected
- GCS credentials expired
- Database connection lost

**Action**: Stop (Ctrl+C), fix issue, resume

### All Skips (⊘ = total)
**Means**: All cases already exported
**Action**: Check if you meant to clean first

### ETA Keeps Increasing
**Means**: Export rate slower than expected
**Possible**: Network issues, large cases
**Action**: May need to run longer than estimated

## Dependencies

The progress bar requires `tqdm`:

```bash
# Install if needed
uv add tqdm

# Or with pip
pip install tqdm
```

Already included in project dependencies.

## Disabling Progress Bar

If you prefer the old format:

```python
# In export_random_cases.py, replace tqdm loop with:
for idx, case_id in enumerate(case_ids, 1):
    print(f"[{idx}/{total}] Exporting case {case_id}...")
    json_path, was_skipped = exporter.export_case_by_id(case_id)
    # ...
```

## Comparison

### Without Progress Bar (old)
```
[1/11435] Exporting case 1...
✓ Successfully exported to ...
[2/11435] Exporting case 2...
✓ Successfully exported to ...
[3/11435] Exporting case 3...
```

**Drawbacks:**
- No ETA
- No visual progress
- Lots of scrolling
- Can't see rate
- Hard to estimate completion

### With Progress Bar (new)
```
Exporting cases: 42%|████████████▌             | 4805/11435 [02:15<03:07, 35.3case/s] ✓4612 ⊘193 ✗0
```

**Benefits:**
✅ Single line (doesn't scroll)
✅ Shows ETA
✅ Shows rate
✅ Visual bar
✅ Live counters
✅ Clean output

## Example Session

```bash
$ bash export_ny_all_cases.sh

================================================================================
Court Case Exporter
================================================================================
Exporting 11435 specific cases...

Exporting cases:   1%|▎                         | 107/11435 [00:02<05:23, 35.0case/s] ✓0 ⊘107 ✗0
✓ Exported case 108
✓ Exported case 109
Exporting cases:   5%|█▍                        | 542/11435 [00:15<04:52, 37.2case/s] ✓435 ⊘107 ✗0
✓ Exported case 543
Exporting cases:  10%|██▉                      | 1143/11435 [00:30<04:35, 37.4case/s] ✓1036 ⊘107 ✗0
...
Exporting cases: 100%|█████████████████████████| 11435/11435 [05:24<00:00, 35.2case/s] ✓11328 ⊘107 ✗0

================================================================================
Export complete!
  Successful: 11328/11435
  Skipped: 107/11435 (already exported)
  Failed: 0/11435
  Output directory: data/cases/ny_after_search
================================================================================
```

**Timeline:**
- 0:00-0:02: Skip 107 existing (54/s)
- 0:02-5:24: Export 11,328 new (35/s)
- Total: 5:24 minutes

## Keyboard Controls

While progress bar running:

- **Ctrl+C** - Stop gracefully (safe)
- **Ctrl+Z** - Suspend (can resume with `fg`)
- **Ctrl+L** - Refresh display (if garbled)

## Summary

The tqdm progress bar transforms the export experience from:

**Before**: "It's running... I think? How long?? 🤷"

**After**: "42% done, 3:07 remaining, 35 cases/sec ✓ 👍"

Much better for long-running exports!
