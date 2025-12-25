#!/bin/bash

root_dir="$(realpath "${1}")"
if [[ ! -d "${root_dir}" ]]; then
    echo "Provided directory not found: ${root_dir}"
    exit 0
fi

find "${root_dir}/Public-Tests/P01_sphincs_plus" -type l -iname '*test_case' -print0 |
    while IFS= read -r -d $'\0' slink; do
        rpath=$(realpath "${slink}") &&
            rm "${slink}" &&
            cp -r "${rpath}" "${slink}" &&
            echo -e "Replaced ${slink} with contents of ${rpath}\n"
    done
