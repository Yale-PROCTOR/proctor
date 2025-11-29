#!/bin/bash

usage() {
    >&2 cat <<EOF
Usage: $0 <translation dir - subset of Public-Tests> <output xml file> [args for run-tests.pyz]
EOF
    exit 1
}

check_file() {
    if [[ ! -f "${1}" ]]; then
        echo "Required file not found: ${1}"
        exit 1
    fi
}

[[ $# -lt 2 ]] && usage

translation_dir="$(realpath "${1}")"
output_xml="$(realpath "${2}")"

translation_dir_dir=$(dirname "${translation_dir}")
translated_rust_dir_link="${translation_dir_dir}/translated_rust"

link_target="$(basename "${translation_dir}")"
ln -sf "${link_target}" "${translated_rust_dir_link}"
echo "Created relative symbolic link ${translated_rust_dir_link} -> ${link_target}"

root_dir="${translation_dir%Public-Tests*}"
if [[ "${root_dir}" == "${translation_dir}" ]]; then
    echo "translation_dir is not subset of Public-Tests"
    usage
else
    cd "${root_dir}" || usage
fi

RUN_TESTS_SCRIPT="./deployment/scripts/github-actions/run-tests.pyz"
check_file "${RUN_TESTS_SCRIPT}"

cargo build --release --manifest-path "${translation_dir}/Cargo.toml"

lib_so=$(find "${translation_dir}/target/release/" -mindepth 1 -maxdepth 1 -type f -iname "lib*.so")
if [[ -f "${lib_so}" ]]; then
    build_ninja_dir="${translation_dir_dir}/build-ninja"
    mkdir -p "${build_ninja_dir}"
    ln -sf "${lib_so}" "${build_ninja_dir}/."
fi

${RUN_TESTS_SCRIPT} \
    --rust \
    --keep-going \
    --subset "${translation_dir_dir}" \
    --junit-xml "${output_xml}" \
    "${@:3}"

[[ -f "${lib_so}" ]] && rm "${build_ninja_dir}/$(basename "${lib_so}")"
rm "${translated_rust_dir_link}"
