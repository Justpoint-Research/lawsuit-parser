#!/bin/bash
# Export all 11,435 NY cases from courts_final.ny_cases_after_search
# This script will skip already exported cases (107 cases currently exist)

echo "========================================"
echo "NY Full Export - All 11,435 Cases"
echo "========================================"
echo ""
echo "Output directory: data/cases/ny_after_search"
echo "Estimated time: 9-10 hours"
echo "Estimated size: 50-100 GB"
echo ""
echo "Note: Script will skip already exported cases."
echo "Currently exported: 107 cases"
echo ""
echo "To monitor progress:"
echo "  tail -f ny_export.log"
echo ""
echo "To check progress:"
echo "  ls -1d data/cases/ny_after_search/case_* | wc -l"
echo ""
echo "========================================"
echo ""

# Option 1: Resume export (skip already exported cases)
echo "Starting export..."
nohup uv run python scripts/export_random_cases.py \
  --case-ids "$(cat ny_case_ids.txt)" \
  --output-dir data/cases/ny_after_search \
  --table-prefix ny_ \
  > ny_export.log 2>&1 &

echo "Export started in background. PID: $!"
echo "Monitor with: tail -f ny_export.log"
