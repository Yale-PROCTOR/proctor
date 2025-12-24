#!/bin/bash

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
APPTAINER_SIF="${SCRIPT_DIR}/apptainer.sif"
APPTAINER_OVERLAY="${SCRIPT_DIR}/apptainer_ext3.img"

MODE="${1}"
TEST_CORPUS_DIR="$(realpath "${2}")"

case "${MODE}" in
custom)
    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --pwd "${TEST_CORPUS_DIR}" \
        "${APPTAINER_SIF}" \
        "${@:3}"
    ;;

translate_all)
    TESTS_DIR="$(realpath "${3}")"

    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --pwd "${TEST_CORPUS_DIR}" \
        "${APPTAINER_SIF}" \
        process_all \
        "${TESTS_DIR}" \
        --action translate \
        "${@:4}"
    ;;

aggregate_all)
    TESTS_DIR="$(realpath "${3}")"

    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --pwd "${TEST_CORPUS_DIR}" \
        "${APPTAINER_SIF}" \
        process_all \
        "${TESTS_DIR}" \
        --action aggregate \
        "${@:4}"
    ;;

visualize_all)
    TESTS_DIR="$(realpath "${3}")"

    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --pwd "${TEST_CORPUS_DIR}" \
        "${APPTAINER_SIF}" \
        process_all \
        "${TESTS_DIR}" \
        --action visualize \
        "${@:4}"
    ;;

translate)
    SRC_DIR="$(realpath "${3}")"

    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --bind "$(dirname "${SRC_DIR}")" \
        --bind "${SRC_DIR}" \
        --pwd "${TEST_CORPUS_DIR}" \
        "${APPTAINER_SIF}" \
        translate \
        "${SRC_DIR}" \
        "${@:4}"
    ;;

unsafety)
    SOURCE_DIR="$(realpath "${3}")"
    OUT_FILE="$(realpath "${4}")"
    touch "${OUT_FILE}"

    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --bind "${SOURCE_DIR}" \
        --bind "${OUT_FILE}" \
        --pwd "${TEST_CORPUS_DIR}" \
        "${APPTAINER_SIF}" \
        measure_unsafety \
        "${SOURCE_DIR}" \
        2>&1 | tee "${OUT_FILE}"
    ;;

idiomaticity)
    SOURCE_DIR="$(realpath "${3}")"
    OUT_FILE="$(realpath "${4}")"
    touch "${OUT_FILE}"

    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --bind "${SOURCE_DIR}" \
        --bind "${OUT_FILE}" \
        --pwd "$(pwd -P)" \
        "${APPTAINER_SIF}" \
        measure_idiomaticity \
        --manifest "${SOURCE_DIR}/Cargo.toml" \
        --output "${OUT_FILE}" \
        --debug \
        --uid "$(id -u)" \
        --gid "$(id -g)" \
        "${@:5}"
    ;;

test)
    SRC_DIR="$(realpath "${3}")"
    OUTPUT_XML="$(realpath "${4}")"
    touch "${OUTPUT_XML}"

    apptainer exec \
        --overlay "${APPTAINER_OVERLAY}" \
        --containall \
        --bind "${TEST_CORPUS_DIR}" \
        --bind "$(dirname "${SRC_DIR}")" \
        --bind "${SRC_DIR}" \
        --bind "${OUTPUT_XML}" \
        --pwd "${TEST_CORPUS_DIR}" \
        "${APPTAINER_SIF}" \
        run_tests \
        "${SRC_DIR}" \
        "${OUTPUT_XML}" \
        "${@:5}"
    ;;

*)
    echo "Unknown mode: '${MODE}'"
    exit 1
    ;;
esac
