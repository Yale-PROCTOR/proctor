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
 && rm -rf /var/lib/apt/lists/*

RUN locale-gen en_US.UTF-8
RUN useradd -m -s /bin/bash ubuntu
RUN echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER ubuntu
WORKDIR /home/ubuntu
ENV LANG="en_US.UTF-8" \
    PATH="/home/ubuntu/local/bin:/home/ubuntu/.cargo/bin:/home/ubuntu/.local/bin:${PATH}"

COPY --chown=ubuntu:ubuntu cmake-3.31.9-linux-x86_64 local
COPY --chown=ubuntu:ubuntu ninja local/bin
COPY --chown=ubuntu:ubuntu Python-3.14.0 Python-3.14.0
RUN cd Python-3.14.0 \
 && ./configure --prefix=/home/ubuntu/local \
 && make -j \
 && make install \
 && cd .. \
 && rm -rf Python-3.14.0
RUN pip3 install toml libclang

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y -q --default-toolchain none

RUN git clone https://github.com/Yale-PROCTOR/c2rust \
 && cd c2rust \
 && git checkout tractor-0.21.0 \
 && cargo build --release --bin c2rust-transpile -Z sparse-registry \
 && ln -s ~/c2rust/target/release/c2rust-transpile ~/local/bin

RUN rustup toolchain install -c rust-src,rustc-dev,llvm-tools-preview nightly-2025-06-23
RUN git clone https://github.com/Yale-PROCTOR/crat \
 && cd crat \
 && git checkout 3e4b29f \
 && cd deps_crate \
 && cargo build \
 && cd .. \
 && cargo build --release\
 && ln -s ~/crat/crat ~/local/bin \
 && ln -s ~/crat/crat-finder ~/local/bin

COPY --chown=ubuntu:ubuntu PUBLIC-Test-Corpus PUBLIC-Test-Corpus-static
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-static \
 && git checkout first-evaluation \
 && sed -i 's/nightly-2025-11-11/nightly-2025-06-23/g' rust-toolchain.toml \
 && sed -i 's/switch-arith/switch_arith/g' Hidden-Tests/B01_synthetic/016_switch-arith_lib/runner/src/main.rs

COPY --chown=ubuntu:ubuntu \
     add_link_args.py \
     cdylib.py \
     find_fns.py \
     filter_files.py \
     get_target.py \
     translate.sh \
     translate_all.py \
     /home/ubuntu/PUBLIC-Test-Corpus-static/

RUN cp -r PUBLIC-Test-Corpus-static PUBLIC-Test-Corpus-no-clippy \
 && sed -i 's/count=0/count=10/g' PUBLIC-Test-Corpus-no-clippy/translate.sh \
 && cp -r PUBLIC-Test-Corpus-static PUBLIC-Test-Corpus-libc \
 && sed -i 's/static,//g' PUBLIC-Test-Corpus-libc/translate.sh \
 && cp -r PUBLIC-Test-Corpus-libc PUBLIC-Test-Corpus-io \
 && sed -i 's/libc,//g' PUBLIC-Test-Corpus-io/translate.sh \
 && cp -r PUBLIC-Test-Corpus-io PUBLIC-Test-Corpus-pointer \
 && sed -i 's/io,//g' PUBLIC-Test-Corpus-pointer/translate.sh \
 && cp -r PUBLIC-Test-Corpus-pointer PUBLIC-Test-Corpus-punning \
 && sed -i 's/pointer,//g' PUBLIC-Test-Corpus-punning/translate.sh \
 && cp -r PUBLIC-Test-Corpus-punning PUBLIC-Test-Corpus-outparam \
 && sed -i 's/punning,//g' PUBLIC-Test-Corpus-outparam/translate.sh \
 && cp -r PUBLIC-Test-Corpus-outparam PUBLIC-Test-Corpus-preprocess \
 && sed -i 's/outparam,//g' PUBLIC-Test-Corpus-preprocess/translate.sh \
 && cp -r PUBLIC-Test-Corpus-preprocess PUBLIC-Test-Corpus-extern \
 && sed -i 's/preprocess,//g' PUBLIC-Test-Corpus-extern/translate.sh \
 && cp -r PUBLIC-Test-Corpus-extern PUBLIC-Test-Corpus-base \
 && sed -i 's/extern,//g' PUBLIC-Test-Corpus-base/translate.sh \
 && sed -i 's/--unsafe-remove-unused//g' PUBLIC-Test-Corpus-base/translate.sh

RUN cd /home/ubuntu/PUBLIC-Test-Corpus-no-clippy && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-static && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-libc && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-io && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-pointer && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-punning && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-outparam && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-preprocess && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-extern && ./translate_all.py
RUN cd /home/ubuntu/PUBLIC-Test-Corpus-base && ./translate_all.py

COPY --chown=ubuntu:ubuntu count.py count_c.py count_clippy.py stat.py /home/ubuntu/
RUN mkdir counts \
 && ./count.py PUBLIC-Test-Corpus-static counts \
 && ./count.py PUBLIC-Test-Corpus-libc counts \
 && ./count.py PUBLIC-Test-Corpus-io counts \
 && ./count.py PUBLIC-Test-Corpus-pointer counts \
 && ./count.py PUBLIC-Test-Corpus-punning counts \
 && ./count.py PUBLIC-Test-Corpus-outparam counts \
 && ./count.py PUBLIC-Test-Corpus-preprocess counts \
 && ./count.py PUBLIC-Test-Corpus-extern counts \
 && ./count.py PUBLIC-Test-Corpus-base counts
RUN sudo apt-get update \
 && DEBIAN_FRONTEND=noninteractive \
    sudo apt-get install -y \
    cloc \
 && sudo rm -rf /var/lib/apt/lists/*
