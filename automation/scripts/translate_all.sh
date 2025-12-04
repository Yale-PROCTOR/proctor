#!/bin/bash

usage() {
    >&2 cat <<EOF
Usage: ${0} <bundles dir> [-seq]
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
TRANSLATE_SH="${SCRIPT_DIR}/translate.sh"

check_file "${TRANSLATE_SH}"
[[ $# -eq 0 ]] && usage

# set -e

bundles_dir="${1}"
bundles=("B01_organic" "B01_synthetic" "P00_perlin_noise" "P01_sphincs_plus")
confs=("c2rust" "c2rust_cfix" "c2rust_crat" "c2rust_crat_cfix")

case "${2}" in
-seq)
    echo "Translating sequentially"
    for bundle in "${bundles[@]}"; do
        find "${bundles_dir}/${bundle}" -maxdepth 1 -type d -print0 |
            while IFS= read -r -d $'\0' dir; do
                for conf in "${confs[@]}"; do
                    "${TRANSLATE_SH}" "--${conf}" "${dir}"
                done
            done
    done
    ;;

*)
    echo "Translating in parallel"

    translate_dir_with_confs() {
        dir="${1}"
        translate_sh="${2}"
        confs=("${@:3}")

        echo "Processing directory: ${dir}"

        for conf in "${confs[@]}"; do
            "${translate_sh}" "--${conf}" "${dir}"
        done
    }
    export -f translate_dir_with_confs

    joblogfile="translate_all.joblog"
    outfile="translate_all.log"

    for bundle in "${bundles[@]}"; do
        find "${bundles_dir}/${bundle}" -mindepth 1 -maxdepth 1 -type d |
            parallel \
                --keep-order \
                --group \
                --joblog "${joblogfile}" \
                translate_dir_with_confs {} "${TRANSLATE_SH}" "${confs[@]}" \
                >>"${outfile}" 2>&1 &
    done
    wait
    ;;
esac
