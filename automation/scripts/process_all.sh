#!/bin/bash

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

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
PROCESS_ALL_PY="${SCRIPT_DIR}/process_all.py"

check_dir "${ROOT_DIR}"
check_file "${PROCESS_ALL_PY}"

actions=("translate" "aggregate" "visualize")

bundles_dir="${ROOT_DIR}/Public-Tests"
bundles=("B01_organic" "B01_synthetic" "P00_perlin_noise" "P01_sphincs_plus" "B02_organic" "B02_synthetic")
confs=("c2rust" "c2rust_cfix" "c2rust_crat" "c2rust_crat_cfix")

processes=10

for action in "${actions[@]}"; do
    "${PROCESS_ALL_PY}" \
        "${bundles_dir}" \
        --processes "${processes}" \
        --action "${action}" \
        --bundles "${bundles[@]}" \
        --confs "${confs[@]}"
done
