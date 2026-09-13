#!/bin/sh
# Run the audit, then compare costs using that audit's output.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

case "${1-}" in
    -h|--help)
        printf '%s\n' \
            'Usage: sh run_analysis.sh [OUTPUT_DIR] [ANALYZER_OPTIONS...]' \
            'Runs analyze_codex_usage.py, then compare_models.py if analysis succeeds.' \
            'OUTPUT_DIR defaults to outputs/ beside this script.' \
            'Example: sh run_analysis.sh outputs/weekdays --exclude-days sat sun'
        exit 0
        ;;
esac

output_dir="$script_dir/outputs"
if [ "$#" -gt 0 ]; then
    case "$1" in
        -*) ;;
        *) output_dir=$1; shift ;;
    esac
fi

python3 "$script_dir/src/analyze_codex_usage.py" "$@" --output-dir "$output_dir"
echo 
python3 "$script_dir/src/compare_models.py" \
    --input "$output_dir/token_usage.csv" --output-dir "$output_dir"
