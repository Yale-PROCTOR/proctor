# Framework container (plan §6, M8): the orchestrator plus everything
# CRAT, c2rust, and the index crate need — all built FROM SOURCE at image
# build time against the image's own LLVM toolchain (like the reference
# tractor-crat-dockerfile), so none of the bare-host env workarounds
# (CPATH for builtin headers, userspace z3/libclang) are needed here.
#
# Build:
#   docker build -t proctor-framework:dev .
#
# Run the c2rust -> crat translation on the whole B01_synthetic corpus,
# mounting the corpus read-only and an output dir for the run results:
#   mkdir -p out && chmod 777 out
#   docker run --rm \
#     -v "$PWD/tractor-test-corpus/Test-Corpus/Public-Tests/B01_synthetic:/corpus:ro" \
#     -v "$PWD/out:/out" \
#     proctor-framework:dev \
#     bench -c configs/bench.toml --corpus /corpus \
#     --set run.output_dir=/out --jobs 16
#
# Single translation:
#   docker run --rm -v "$PWD/case:/case:ro" -v "$PWD/out:/out" \
#     proctor-framework:dev run -c configs/c2rust_crat.toml \
#     --input-c /case --set run.output_dir=/out

FROM ubuntu:24.04

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    clang \
    cmake \
    curl \
    git \
    libclang-dev \
    libssl-dev \
    libz3-dev \
    llvm-dev \
    ninja-build \
    pkg-config \
    python3 \
    zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m proctor
USER proctor
WORKDIR /home/proctor
ENV PATH="/home/proctor/local/bin:/home/proctor/.local/bin:/home/proctor/.cargo/bin:${PATH}"
RUN mkdir -p /home/proctor/local/bin

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y -q --default-toolchain stable
# The tractor-test-corpus (@0319ab0) workspace pins this nightly via its
# rust-toolchain.toml; the cando test-vector runners build with it. Bake
# it in so per-case runner builds reuse one toolchain instead of each
# racing to auto-install it in parallel (which corrupts it).
RUN rustup toolchain install nightly-2025-11-11 --profile minimal
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

COPY --chown=proctor:proctor . /home/proctor/proctor
WORKDIR /home/proctor/proctor

RUN uv sync
# Warm everything: c2rust-transpile (built from the submodule against
# the image's LLVM), crat (pulls its pinned nightly via
# rust-toolchain.toml), stage venvs, and the index crate.
RUN uv run proctor warmup -c tests/e2e/translation_smoke.toml

# Put the built tools on PATH (reference tractor-crat-dockerfile parity):
# c2rust-transpile is a plain binary; `crat` is a wrapper script that
# self-resolves its DIR/SYSROOT via readlink, so a symlink works.
# With c2rust on PATH the adapter resolves it there (native build — no
# CPATH needed), and it survives a live repo mount.
RUN ln -sf /home/proctor/proctor/stages/c2rust/target/release/c2rust-transpile \
      /home/proctor/local/bin/c2rust-transpile \
 && ln -sf /home/proctor/proctor/stages/crat/crat \
      /home/proctor/local/bin/crat

ARG PROCTOR_IMAGE=proctor-framework:dev
ENV PROCTOR_IMAGE=${PROCTOR_IMAGE}

ENTRYPOINT ["uv", "run", "proctor"]
CMD ["--help"]
