#! /bin/bash

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"

set -euo pipefail
cd "${SCRIPT_DIR}/.."

# use ssh link instead of root's http link
if [[ ! -d "Test-Corpus" ]]; then
    echo "Downloading Test-Corpus"
    git clone git@github.com:DARPA-TRACTOR-Program/Test-Corpus.git
fi

if [[ ! -d "Test-Corpus-Custom" ]]; then
    echo "Creating Test-Corpus-Custom"
    cp -r Test-Corpus Test-Corpus-Custom
    pushd Test-Corpus-Custom
    git checkout 96ce4c7
    git apply ../fixes.patch
    cd deployment/scripts/github-actions
    rm run-tests.pyz
    python3 -m zipapp . -m "runtests.__main__:main" -p "/usr/bin/env python3" -o run-tests.pyz
    popd
fi

if [[ ! -d "Python-3.13.0" ]]; then
    echo "Downloading Python"
    wget https://www.python.org/ftp/python/3.13.0/Python-3.13.0.tgz
    tar xf Python-3.13.0.tgz
    rm Python-3.13.0.tgz
fi

# invoke the root's download script
./download.sh
