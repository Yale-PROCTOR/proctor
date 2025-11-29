#!/bin/bash

set -e

cd ~/c2rust
git checkout tractor
git pull
cargo build --release -Z sparse-registry

cd ~/crat
git checkout master
git pull
cd deps_crate
cargo build
cd ..
cargo build --release
