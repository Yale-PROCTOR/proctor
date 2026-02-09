#!/bin/bash

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"

set -euo pipefail
cd "${SCRIPT_DIR}/.."

# use ssh link instead of root's http link
if [[ ! -d "Test-Corpus" ]]; then
    echo "Downloading Test-Corpus"
    git clone git@github.com:DARPA-TRACTOR-Program/Test-Corpus.git
fi

# invoke the root's download script
./download.sh
