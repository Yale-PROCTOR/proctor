#!/bin/bash

usage() {
    >&2 cat <<EOF
Usage: $0 <bundles dir>
EOF
    exit 1
}

check_file() {
    if [[ ! -f "${1}" ]]; then
        echo "Required file not found: ${1}"
        exit 1
    fi
}

if [[ $# -ne 1 ]]; then
    usage
fi

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
AGGREGATE_PY="${SCRIPT_DIR}/aggregate.py"

check_file "${AGGREGATE_PY}"
[[ $# -eq 0 ]] && usage

bundles_dir="${1}"
types=("unsafety" "idiomaticity" "tests")
bundles=("B01_organic" "B01_synthetic" "P00_perlin_noise" "P01_sphincs_plus")
confs=("c2rust" "c2rust_cfix" "c2rust_crat" "c2rust_crat_cfix")

declare -A types2ext
types2ext["unsafety"]="json"
types2ext["idiomaticity"]="json"
types2ext["tests"]="xml"

for type in "${types[@]}"; do
    out_dir="${type}_results"
    mkdir "${out_dir}" >/dev/null 2>&1

    for bundle in "${bundles[@]}"; do
        for conf in "${confs[@]}"; do
            bundle_path="${bundles_dir}/${bundle}"
            ext="${types2ext[${type}]}"
            file_pattern="**/translated_${conf}/results/${type}.${ext}"
            out_prefix="${out_dir}/${bundle}_${conf}"

            python3 "${AGGREGATE_PY}" \
                --type "${type}" \
                --bundle_path "${bundle_path}" \
                --file_pattern "${file_pattern}" \
                --out_prefix "${out_prefix}"
        done
    done
done
