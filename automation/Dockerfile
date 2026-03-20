# Use this Dockerfile with the build context of the repo root
# Prefix necessary files with dir: automation/file
# Invoke from repo root: docker build -t <label> -f automation/Dockerfile .
FROM ubuntu:25.04
SHELL ["/bin/bash", "-c"]

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
    vim

RUN locale-gen en_US.UTF-8
RUN echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER ubuntu
WORKDIR /home/ubuntu
ENV LANG="en_US.UTF-8" \
    CLIPPY_CONF_DIR="/home/ubuntu/.clippy" \
    SYS_VENV="/home/ubuntu/venvs/sys_venv"
ENV PATH="${SYS_VENV}/bin:/home/ubuntu/local/bin:/home/ubuntu/.cargo/bin:/home/ubuntu/.local/bin:${PATH}"

COPY --chown=ubuntu:ubuntu ./cmake-3.31.9-linux-x86_64 ./local
COPY --chown=ubuntu:ubuntu ./ninja ./local/bin

# install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# uv uses the system python for the venv
RUN uv venv "${SYS_VENV}" \
    && uv pip install \
        toml tabulate requests beautifulsoup4 lxml libclang mpire \
        --python "${SYS_VENV}"

# install node via nvm
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash \
    && source "${HOME}/.nvm/nvm.sh" \
    && nvm install 24

# install rust via rustup
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y -q --default-toolchain none

RUN rustup toolchain install -c rust-src,rustc-dev,llvm-tools-preview nightly-2025-06-23 \
    && rustup component add rust-analyzer \
    && cargo install ripgrep

# build c2rust
RUN git clone https://github.com/Yale-PROCTOR/c2rust.git \
    && cd c2rust \
    && git checkout tractor-0.21.0 \
    && cargo build --release --bin c2rust-transpile -Z sparse-registry \
    && ln -s ~/c2rust/target/release/c2rust-transpile ~/local/bin

# build crat
RUN git clone https://github.com/Yale-PROCTOR/crat.git \
    && cd crat \
    && git checkout d15a3ce \
    && cd deps_crate \
    && cargo build \
    && cd .. \
    && cargo build --release --bin crat \
    && ln -s ~/crat/crat ~/local/bin

COPY --chown=ubuntu:ubuntu ./Test-Corpus Test-Corpus
COPY --chown=ubuntu:ubuntu 0001-fixed.patch 0001-fixed.patch
RUN cd Test-Corpus \
    && git restore . \
    && git clean -f \
    && git checkout 0319ab0 \
    && git apply ../0001-fixed.patch

COPY --chown=ubuntu:ubuntu \
    ./add_link_args.py \
    ./cdylib.py \
    ./find_fns.py \
    ./filter_files.py \
    ./get_target.py \
    ./automation/scripts/* \
    Test-Corpus

COPY --chown=ubuntu:ubuntu ./automation/measurements measurements
RUN cargo install --path ~/measurements/measure_unsafety --root ~/measurements/measure_unsafety \
    && ln -s ~/measurements/measure_unsafety/bin/measure_unsafety ~/local/bin/measure_unsafety

RUN chmod +x ~/measurements/measure_idiomaticity/measure_idiomaticity.py \
    && ln -s ~/measurements/measure_idiomaticity/measure_idiomaticity.py ~/local/bin/measure_idiomaticity

RUN mkdir "${CLIPPY_CONF_DIR}" \
    && ln -s ~/measurements/measure_idiomaticity/clippy.toml "${CLIPPY_CONF_DIR}/clippy.toml"

RUN ln -s ~/Test-Corpus/translate.sh ~/local/bin/translate \
    && ln -s ~/Test-Corpus/run_tests.sh ~/local/bin/run_tests \
    && ln -s ~/Test-Corpus/aggregate.py ~/local/bin/aggregate \
    && ln -s ~/Test-Corpus/visualize.py ~/local/bin/visualize \
    && ln -s ~/Test-Corpus/process_all.py ~/local/bin/process_all
