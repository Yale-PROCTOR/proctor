#! /bin/bash

set -euo pipefail

if [ ! -d "cmake-3.31.9-linux-x86_64" ]; then
  echo "Downloading Cmake"
  wget https://github.com/Kitware/CMake/releases/download/v3.31.9/cmake-3.31.9-linux-x86_64.tar.gz
  tar xf cmake-3.31.9-linux-x86_64.tar.gz
  rm cmake-3.31.9-linux-x86_64.tar.gz
fi

if [ ! -f "ninja" ]; then
  echo "Downloading Ninja"
  wget https://github.com/ninja-build/ninja/releases/download/v1.13.1/ninja-linux.zip
  unzip ninja-linux.zip
  rm ninja-linux.zip
fi

if [ ! -d "Python-3.14.0" ]; then
  echo "Downloading Python"
  wget https://www.python.org/ftp/python/3.14.0/Python-3.14.0.tgz
  tar xf Python-3.14.0.tgz
  rm Python-3.14.0.tgz
fi

if [ ! -d "PUBLIC-Test-Corpus" ]; then
  echo "Downloading PUBLIC-Test-Corpus"
  git clone https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus
fi
