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

# set -euxo pipefail
# set -e

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"

GET_TARGET_PY="${SCRIPT_DIR}/get_target.py"
CDYLIB_PY="${SCRIPT_DIR}/cdylib.py"
ADD_LINK_ARGS_PY="${SCRIPT_DIR}/add_link_args.py"
FILTER_FILES_PY="${SCRIPT_DIR}/filter_files.py"
FIND_FNS_PY="${SCRIPT_DIR}/find_fns.py"

check_file "${GET_TARGET_PY}"
check_file "${CDYLIB_PY}"
check_file "${ADD_LINK_ARGS_PY}"
check_file "${FILTER_FILES_PY}"

use_crat=true
use_cfix=true
translated_dir="translated_c2rust_crat_cfix"

args=$(getopt -o '' --long help,c2rust,c2rust_cfix,c2rust_crat,c2rust_crat_cfix -- "$@")
[[ ! $? -eq 0 ]] && usage

eval set -- "${args}"
while true; do
    case "${1}" in
    --c2rust)
        use_crat=false
        use_cfix=false
        translated_dir="translated_c2rust"
        shift
        ;;
    --c2rust_cfix)
        use_crat=false
        use_cfix=true
        translated_dir="translated_c2rust_cfix"
        shift
        ;;
    --c2rust_crat)
        use_crat=true
        use_cfix=false
        translated_dir="translated_c2rust_crat"
        shift
        ;;
    --c2rust_crat_cfix)
        use_crat=true
        use_cfix=true
        translated_dir="translated_c2rust_crat_cfix"
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

src="$(realpath "${1}")"
#dst="${src}/${translated_dir}"

rm -rf "${src}/build-ninja" "${src}/config.toml"
#rm -rf "${dst:?}" "${src}/build-ninja"
#mkdir -p "${dst}"

# create compile commands
pushd "${src}" >/dev/null
mkdir -p build-ninja/.cmake/api/v1/query
touch build-ninja/.cmake/api/v1/query/codemodel-v2
if [[ -f CMakePresets.json ]]; then
    cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=1 -S ./ --preset test
    cd build-ninja
    cmake --build ./
    src_root="${src}"
else
    cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=1 -S ./test_case -B ./build-ninja -G Ninja
    cd build-ninja
    cmake --build ./
    src_root="${src}/test_case"
fi
popd >/dev/null

target_name=$("${GET_TARGET_PY}" "${src}/build-ninja" name)
target_type=$("${GET_TARGET_PY}" "${src}/build-ninja" type)

true_dst="${src}/${translated_dir}"
dst="${src}/${translated_dir}/${target_name}"
rm -rf "${true_dst:?}"
mkdir -p "${dst}"

target_lib_so="${src}/build-ninja/lib${target_name}.so"
if [[ -f "${target_lib_so}" ]]; then
    rm "${target_lib_so}"
    echo Removed previous "${target_lib_so}"
fi

# compile with c2rust and crat
if [[ "${target_type}" == "EXECUTABLE" ]]; then
    "${FILTER_FILES_PY}" "${src}/build-ninja" "${src_root}" "${src}/build-ninja/compile_commands.json"

    if [[ "${use_crat}" == "false" ]]; then
        main_file="main"
        if [[ "${src}" == *"P01_sphincs_plus"* ]]; then
            main_file="PQCgenKAT_sign"
        fi

        c2rust-transpile \
            --binary "${main_file}" \
            -e "${src}/build-ninja/compile_commands.json" \
            -o "${dst}"

        sed -i "s/name = \"${main_file}\"/name = \"${target_name}\"/" "${dst}/Cargo.toml"
        "${ADD_LINK_ARGS_PY}" "${src}/build-ninja" "${dst}/build.rs"

    else
        c2rust-transpile \
            -e "${src}/build-ninja/compile_commands.json" \
            -o "${dst}"

        "${ADD_LINK_ARGS_PY}" "${src}/build-ninja" "${dst}/build.rs"

        mkdir -p "${dst}/.cargo"
        echo '[target.x86_64-unknown-linux-gnu]' >"${dst}/.cargo/config.toml"
        echo 'rustflags = ["-Clink-arg=-Wl,-z,lazy", "-Zplt=yes"]' >>"${dst}/.cargo/config.toml"

        "${FIND_FNS_PY}" "${src}/build-ninja/compile_commands.json" "${src}/test_case" "${src}/config.toml"

        crat \
            --config "${src}/config.toml" \
            --inplace \
            --extern-cmake-reply-index-file "${src}/build-ninja/.cmake/api/v1/reply/index-*.json" \
            --extern-build-dir "${src}/build-ninja" \
            --extern-source-dir "${src}" \
            --extern-ignore-return-type \
            --io-assume-to-str-ok \
            --unsafe-remove-unused \
            --unsafe-remove-no-mangle \
            --unsafe-remove-extern-c \
            --unsafe-replace-pub \
            --unexpand-use-print \
            --bin-name "${target_name}" \
            --pass expand,preprocess,extern,pointer,io,libc,static,simpl,check,interface,unsafe,unexpand,split,bin \
            "${dst}"
    fi

else
    c2rust-transpile \
        -e "${src}/build-ninja/compile_commands.json" \
        -o "${dst}"

    "${ADD_LINK_ARGS_PY}" "${src}/build-ninja" "${dst}/build.rs"

    if [[ "${use_crat}" == "true" ]]; then
        mkdir -p "${dst}/.cargo"
        echo '[target.x86_64-unknown-linux-gnu]' >"${dst}/.cargo/config.toml"
        echo 'rustflags = ["-Clink-arg=-Wl,-z,lazy", "-Zplt=yes"]' >>"${dst}/.cargo/config.toml"
        "${FIND_FNS_PY}" "${src}/build-ninja/compile_commands.json" "${src}/test_case" "${src}/config.toml"

        crat \
            --config "${src}/config.toml" \
            --inplace \
            --io-assume-to-str-ok \
            --unsafe-remove-unused \
            --unsafe-remove-no-mangle \
            --unsafe-remove-extern-c \
            --unsafe-replace-pub \
            --unexpand-use-print \
            --pass expand,preprocess,extern,pointer,io,libc,static,simpl,check,interface,unsafe,unexpand,split,bin \
            "${dst}"
    fi
fi

# refine with clippy fix and cargo fmt
if [[ "${use_cfix}" == "true" ]]; then
    while cargo clippy \
        --fix \
        --allow-no-vcs \
        --manifest-path "${dst}/Cargo.toml" 2>&1 |
        grep -q "run \`cargo clippy --fix"; do
        echo "Running clippy --fix"
    done
fi

# cargo generate-lockfile --manifest-path "${dst}/Cargo.toml"
cargo fmt --manifest-path "${dst}/Cargo.toml"
"${CDYLIB_PY}" "${src}/build-ninja" "${src}" "${dst}"

# measure unsafety and idiomaticity and run tests
mkdir -p "${dst}/results"

measure_unsafety "${dst}" 2>&1 | tee "${dst}/results/unsafety.json"

measure_idiomaticity \
    --manifest "${dst}/Cargo.toml" \
    --output "${dst}/results/idiomaticity.json" \
    --debug \
    --uid "$(id -u)" \
    --gid "$(id -g)"

mv "${dst}"/* "${dst}/.cargo" "${true_dst}"
rm -d "${dst}"

# test_case directory needs to be the parent of the target directory
# thus we move our directories first and then run tests
run_tests "${true_dst}" "${true_dst}/results/tests.xml" --verbose

rm -r "${src}/build-ninja" "${src}/config.toml"
