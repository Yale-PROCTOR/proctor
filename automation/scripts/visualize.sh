#!/bin/bash

check_file() {
    if [[ ! -f "${1}" ]]; then
        echo "Required file not found: ${1}"
        exit 1
    fi
}

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
VISUALIZE_PY="${SCRIPT_DIR}/visualize.py"

check_file "${VISUALIZE_PY}"

types=("unsafety" "idiomaticity" "tests")
bundles=("B01_organic" "B01_synthetic" "P00_perlin_noise" "P01_sphincs_plus")
confs=("c2rust" "c2rust_cfix" "c2rust_crat" "c2rust_crat_cfix")

for type in "${types[@]}"; do
    agg_dir="${type}_results"

    if [[ ! -d "${agg_dir}" ]]; then
        echo "No directory found: ${agg_dir}"
        continue
    fi

    out_dir="${agg_dir}/tables"
    out_file="${out_dir}/${type}.txt"
    mkdir "${out_dir}" >/dev/null 2>&1

    python3 "${VISUALIZE_PY}" \
        --type "${type}" \
        --agg_dir "${agg_dir}" \
        --bundles "${bundles[@]}" \
        --confs "${confs[@]}" \
        --out_file "${out_file}"
done
