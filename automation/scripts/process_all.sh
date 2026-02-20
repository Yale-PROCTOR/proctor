#!/bin/bash

actions=("translate" "aggregate" "visualize")

bundles_dir="./Public-Tests"
bundles=("B01_organic" "B01_synthetic" "P00_perlin_noise" "P01_sphincs_plus" "B02_organic" "B02_synthetic")
confs=("c2rust" "c2rust_cfix" "c2rust_crat" "c2rust_crat_cfix")

processes=10

for action in "${actions[@]}"; do
    ./process_all.py \
        "${bundles_dir}" \
        --processes "${processes}" \
        --action "${action}" \
        --bundles "${bundles[@]}" \
        --confs "${confs[@]}"
done
