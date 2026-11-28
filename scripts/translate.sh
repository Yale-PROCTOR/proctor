#!/bin/sh
set -eu

usage() {
    printf 'Usage: %s INPUT_TARBALL OUTPUT_DIR\n' "$0"
}

if [ "$#" -eq 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
    usage
    exit 0
fi
if [ "$#" -ne 2 ]; then
    usage >&2
    exit 2
fi

project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec uv run --project "$project" "$project/scripts/orchestrate.py" \
    -c "$project/configs/full.toml" \
    --rule-set "$project/rule-sets/empty-rule-set.json" \
    --input-c="$1" --output="$2"
