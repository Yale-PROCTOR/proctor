#! /bin/bash

set -euo pipefail
cd ..

# use ssh link instead of root's http link
if [ ! -d "Test-Corpus" ]; then
    echo "Downloading Test-Corpus"
    git clone git@github.com:DARPA-TRACTOR-Program/Test-Corpus.git
fi

if [ ! -d "Python-3.13.0" ]; then
    echo "Downloading Python"
    wget https://www.python.org/ftp/python/3.13.0/Python-3.13.0.tgz
    tar xf Python-3.13.0.tgz
    rm Python-3.13.0.tgz
fi

# invoke the root's download script
./download.sh
