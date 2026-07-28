# End-to-end tests

```bash
uv run pytest -m e2e
```

Runs one public TRACTOR case (`001_helloworld`) through the real
pipeline stages:

- **crat smoke** — crat pass chain over the vendored c2rust fixture,
  test-package gate on, resume reuses the checkpoint;
- **translation smoke** — full C source → c2rust → CRAT → tested Rust
  with `proctor.toml`;
- **index e2e** — builds `proctor-rust-index` and indexes the fixture;
- **vector harness e2e** — drives TRACTOR's authoritative
  `runtests.rust` harness against the real crat output
  (`fixtures/001_helloworld/translated_rust`) and asserts all three
  corpus vectors pass. Needs only cargo/rustup (no Docker/Falco); the
  fixture pins nightly-2025-06-23 via its `rust-toolchain` file.

## c2rust-transpile resolution

The c2rust stage looks for the transpiler in order:

1. `c2rust-transpile` on `PATH`;
2. a prebuilt under `$PROCTOR_CACHE_DIR/c2rust/bin` with its shared
   libs in `$PROCTOR_CACHE_DIR/c2rust/lib` (extractable from the legacy
   `proctor:june2026` image — binary plus `libLLVM-10.so.1`,
   `libffi.so.7`, `libedit.so.2`, `libtinfo.so.6`, copied with
   `docker cp -L`);
3. built from the `stages/c2rust` submodule — needs clang/LLVM dev
   packages (`apt install clang libclang-dev llvm-dev`), which the
   framework container image includes.

## Requirements

- `rustup` (crat's `rust-toolchain.toml` auto-installs its pinned
  nightly and components on first build)
- the crat submodule: `git submodule update --init stages/crat`
- CRAT's build-time system deps: **libclang** (bindgen) and **z3**
  (z3-sys). The first build takes minutes; it is cached per crat commit.

With sudo, the simple path:

```bash
sudo apt install -y libclang-dev libz3-dev
```

Without sudo, the userspace recipe (all under `~/.cache/proctor`):

```bash
CACHE=~/.cache/proctor && mkdir -p $CACHE && cd $CACHE
curl -LsSf -o z3.zip https://github.com/Z3Prover/z3/releases/download/z3-4.13.4/z3-4.13.4-x64-glibc-2.35.zip
python3 -c "import zipfile; zipfile.ZipFile('z3.zip').extractall()"
mv z3-4.13.4-x64-glibc-2.35 z3 && rm z3.zip
uv pip install --target $CACHE/libclang libclang

export Z3_SYS_Z3_HEADER=$CACHE/z3/include/z3.h
export LIBCLANG_PATH=$CACHE/libclang/clang/native
export LIBRARY_PATH=$CACHE/z3/bin
export LD_LIBRARY_PATH=$CACHE/z3/bin
export RUSTFLAGS="-L native=$CACHE/z3/bin"
# bindgen needs compiler builtin headers (stdbool.h); point it at gcc's:
export BINDGEN_EXTRA_CLANG_ARGS="-isystem $(dirname $(find /usr/lib/gcc/x86_64-linux-gnu -name stdbool.h | head -1))"
```

These env vars are only needed while CRAT builds. Inside the eventual
framework container (M8) none of this applies — the image installs the
apt packages.
