#!/bin/bash

usage() {
    >&2 cat <<EOF
Usage: $0 <bundles dir> [args for run-tests.pyz]
EOF
    exit 1
}

check_file() {
    if [[ ! -f "${1}" ]]; then
        echo "Required file not found: ${1}"
        exit 1
    fi
}

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
RUN_TESTS_SH="${SCRIPT_DIR}/run_tests.sh"

check_file "${RUN_TESTS_SH}"
[[ $# -eq 0 ]] && usage

# set -e

bundles_dir="${1}"
bundles=("B01_organic" "B01_synthetic" "P00_perlin_noise" "P01_sphincs_plus")
confs=("c2rust" "c2rust_cfix" "c2rust_crat" "c2rust_crat_cfix")

for bundle in "${bundles[@]}"; do
    for conf in "${confs[@]}"; do
        for dir in "${bundles_dir}/${bundle}"/**/"translated_${conf}"; do
            "${RUN_TESTS_SH}" "${dir}" "${dir}/results/tests.xml" "${@:2}"
        done
    done
done
