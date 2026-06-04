#!/bin/bash

usage() {
    >&2 cat <<EOF
Usage: ${0} [option] <source_dir>
Options:
   --c2rust
   --c2rust_cfix
   --c2rust_crat
   --c2rust_crat_cfix (enabled by default)
   --help
EOF
    exit 1
}

check_file() {
    if [[ ! -f "${1}" ]]; then
        echo "Required file not found: ${1}"
        exit 1
    fi
}

check_dir() {
    if [[ ! -d "${1}" ]]; then
        echo "Required directory not found: ${1}"
        exit 1
    fi
}

set -euo pipefail
# set -euxo pipefail

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
ORCHESTRATE_PY="${SCRIPT_DIR}/orchestrate.py"

check_file "${ORCHESTRATE_PY}"

conf="c2rust_crat_cfix"

args=$(getopt -o '' --long help,c2rust,c2rust_cfix,c2rust_crat,c2rust_crat_cfix -- "$@")
[[ ! $? -eq 0 ]] && usage

eval set -- "${args}"
while true; do
    case "${1}" in
    --c2rust)
        conf="c2rust"
        shift
        ;;
    --c2rust_cfix)
        conf="c2rust_cfix"
        shift
        ;;
    --c2rust_crat)
        conf="c2rust_crat"
        shift
        ;;
    --c2rust_crat_cfix)
        conf="c2rust_crat_cfix"
        shift
        ;;
    --help)
        usage
        ;;
    --)
        shift
        break
        ;;
    *)
        echo Unsupported option: "${1}"
        usage
        ;;
    esac
done

[[ $# -ne 1 ]] && usage

echo "Translation Start"
src="$(realpath "${1}")"
src_bundle="${src/Test-Corpus/Test-Corpus/bundles}.tar.gz"
check_file "${src_bundle}"
echo "source: ${src_bundle}"

translated_dir="translated_${conf}"
dst="${src}/${translated_dir}"
rm -rf "${dst}"
echo "destination: ${dst}"

# translate
"${ORCHESTRATE_PY}" "${src_bundle}" "${dst}" "${conf}"

# measure unsafety and idiomaticity
mkdir -p "${dst}/results"

measure_unsafety "${dst}" 2>&1 | tee "${dst}/results/unsafety.json"

measure_idiomaticity \
    --include_ccc \
    --output "${dst}/results/idiomaticity.json" \
    "${dst}"

# run tests
run_tests "${dst}" "${dst}/results/tests.xml" --verbose

echo "Translation End"
