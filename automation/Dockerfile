# Use this Dockerfile with the build context of the repo root
# Prefix necessary files with dir: automation/file
# Invoke from repo root: docker build -t <label> -f automation/Dockerfile .
FROM ubuntu:20.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive \
    apt-get install -y \
    clang \
    curl \
    g++ \
    gcc \
    git \
    libclang-dev \
    libssl-dev \
    llvm \
    locales \
    make \
    pkg-config \
    sudo \
    zlib1g-dev \
    vim \
 && rm -rf /var/lib/apt/lists/*

RUN locale-gen en_US.UTF-8
RUN useradd -m -s /bin/bash ubuntu
RUN echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER ubuntu
WORKDIR /home/ubuntu
ENV LANG="en_US.UTF-8" \
    CLIPPY_CONF_DIR="/home/ubuntu/.clippy" \
    PATH="/home/ubuntu/local/bin:/home/ubuntu/.cargo/bin:/home/ubuntu/.local/bin:${PATH}"

COPY --chown=ubuntu:ubuntu ./cmake-3.31.9-linux-x86_64 ./local
COPY --chown=ubuntu:ubuntu ./ninja ./local/bin
COPY --chown=ubuntu:ubuntu ./Python-3.14.0 ./Python-3.14.0
RUN cd Python-3.14.0 \
 && ./configure --prefix=/home/ubuntu/local \
 && make -j \
 && make install \
 && cd .. \
 && rm -rf Python-3.14.0
RUN pip3 install toml tabulate beautifulsoup4 lxml libclang mpire

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y -q --default-toolchain nightly-2025-06-23-x86_64-unknown-linux-gnu

RUN git clone https://github.com/Medowhill/c2rust \
 && cd c2rust \
 && git checkout tractor-0.21.0 \
 && cargo build --release -Z sparse-registry \
 && ln -s ~/c2rust/target/release/c2rust-transpile ~/local/bin

RUN git clone https://github.com/kaist-plrg/crat \
 && cd crat \
 && git checkout 7e7d744 \
 && cd deps_crate \
 && cargo build \
 && cd .. \
 && cargo build --release --bin crat \
 && ln -s ~/crat/crat ~/local/bin

RUN cd crat \
 && git checkout master \
 && git pull \
 && git checkout 2dcafd7 \
 && cargo build --release --bin crat

COPY --chown=ubuntu:ubuntu ./Test-Corpus Test-Corpus
COPY --chown=ubuntu:ubuntu \
     ./add_link_args.py \
     ./cdylib.py \
     ./find_fns.py \
     ./filter_files.py \
     ./get_target.py \
     ./automation/scripts/* \
     Test-Corpus

COPY --chown=ubuntu:ubuntu ./Test-Corpus Test-Corpus-Custom
COPY --chown=ubuntu:ubuntu ./fixes.patch Test-Corpus-Custom

RUN cd Test-Corpus-Custom \
 && git checkout 96ce4c7 \
 && git apply fixes.patch \
 && rm fixes.patch \
 && cd deployment/scripts/github-actions \
 && rm run-tests.pyz \
 && python3 -m zipapp . -m "runtests.__main__:main" -p "/usr/bin/env python3" -o run-tests.pyz

COPY --chown=ubuntu:ubuntu \
     ./add_link_args.py \
     ./cdylib.py \
     ./find_fns.py \
     ./filter_files.py \
     ./get_target.py \
     ./automation/scripts/* \
     Test-Corpus-Custom

COPY --chown=ubuntu:ubuntu ./automation/measurements measurements
RUN cargo install --path ~/measurements/measure_unsafety --root ~/measurements/measure_unsafety \
 && ln -s ~/measurements/measure_unsafety/bin/measure_unsafety ~/local/bin/measure_unsafety

RUN chmod +x ~/measurements/measure_idiomaticity/measure_idiomaticity.py \
 && ln -s ~/measurements/measure_idiomaticity/measure_idiomaticity.py ~/local/bin/measure_idiomaticity

RUN mkdir ~/.clippy && ln -s ~/measurements/measure_idiomaticity/clippy.toml ~/.clippy/clippy.toml

RUN ln -s ~/Test-Corpus/translate.sh ~/local/bin/translate \
 && ln -s ~/Test-Corpus/translate_all.sh ~/local/bin/translate_all \
 && ln -s ~/Test-Corpus/run_tests.sh ~/local/bin/run_tests \
 && ln -s ~/Test-Corpus/run_tests_all.sh ~/local/bin/run_tests_all \
 && ln -s ~/Test-Corpus/aggregate.py ~/local/bin/aggregate \
 && ln -s ~/Test-Corpus/aggregate.sh ~/local/bin/aggregate_all \
 && ln -s ~/Test-Corpus/visualize.py ~/local/bin/visualize \
 && ln -s ~/Test-Corpus/visualize.sh ~/local/bin/visualize_all
