#!/bin/bash
# Verify that resume capability is working correctly

echo "========================================"
echo "Verify Resume Capability"
echo "========================================"
echo ""

# Check current state
exported=$(ls -1d data/cases/ny_after_search/case_* 2>/dev/null | wc -l | tr -d ' ')
echo "Currently exported: $exported cases"
echo ""

if [ "$exported" -eq 0 ]; then
    echo "No cases exported yet. Nothing to verify."
    exit 0
fi

echo "Checking case completeness..."
echo ""

incomplete=0
complete=0

for case_dir in data/cases/ny_after_search/case_*; do
  case_id=$(basename "$case_dir" | sed 's/case_//')
  json="$case_dir/case_${case_id}.json"

  if [ ! -f "$json" ]; then
    echo "⚠ INCOMPLETE: case_${case_id} (no JSON)"
    incomplete=$((incomplete + 1))
  else
    complete=$((complete + 1))
  fi
done

echo ""
echo "========================================"
echo "Results:"
echo "  Complete cases: $complete"
echo "  Incomplete cases: $incomplete"
echo "========================================"
echo ""

if [ "$incomplete" -gt 0 ]; then
    echo "Some cases are incomplete."
    echo "This is normal if export was interrupted."
    echo ""
    echo "To resume and complete all cases:"
    echo "  bash export_ny_all_cases.sh"
else
    echo "✓ All exported cases are complete!"
    echo ""
    echo "To continue exporting remaining cases:"
    echo "  bash export_ny_all_cases.sh"
fi
echo ""

# Show sample of what resume would skip
if [ "$complete" -gt 0 ]; then
    echo "When you resume, these cases will be skipped:"
    count=0
    for case_dir in data/cases/ny_after_search/case_*; do
        if [ "$count" -ge 3 ]; then
            break
        fi
        case_id=$(basename "$case_dir" | sed 's/case_//')
        json="$case_dir/case_${case_id}.json"
        if [ -f "$json" ]; then
            echo "  - case_${case_id} ✓"
            count=$((count + 1))
        fi
    done
    if [ "$complete" -gt 3 ]; then
        echo "  ... and $(($complete - 3)) more"
    fi
fi
echo ""
