#!/bin/bash
# Clean partial export to start fresh
# Use this if you want to restart the export from scratch

echo "WARNING: This will delete all partially exported NY cases"
echo "Currently exported: $(ls -1d data/cases/ny_after_search/case_* 2>/dev/null | wc -l) cases"
echo "Directory size: $(du -sh data/cases/ny_after_search 2>/dev/null | cut -f1)"
echo ""
read -p "Are you sure you want to delete these? (yes/no): " confirm

if [ "$confirm" = "yes" ]; then
    echo "Deleting ny_after_search directory..."
    rm -rf data/cases/ny_after_search
    echo "✓ Deleted"
    echo ""
    echo "Ready for fresh export. Run:"
    echo "  bash export_ny_all_cases.sh"
else
    echo "Cancelled. No files deleted."
    echo ""
    echo "To resume export (skip completed cases):"
    echo "  bash export_ny_all_cases.sh"
fi
