# PROCTOR Orchestration Framework

A configurable pipeline that turns a TRACTOR C project into tested, idiomatic Rust.
It orchestrates translation **stages** — standalone programs pinned as git submodules
under `stages/`, wired by a JSON envelope contract (`docs/stage-contract.md`) — and
provides a vendor-agnostic LLM API, usage/cost tracking, and a prompt library.

Stages today: **`c2rust` → `crat`** (the Translation component: C → tested *unsafe* Rust),
plus an optional **`abstraction_recovery`** stage (LLM-driven: replace hand-rolled C data
structures with Rust std collections).

## Setup

```bash
git clone <repo> && cd proctor
git submodule update --init stages/crat stages/c2rust
./fetch_corpus.sh            # TRACTOR corpus at the pinned commit (DARPA access; not vendored)
uv sync
uv run proctor warmup -c tests/e2e/translation_smoke.toml   # pre-build stages
```

Host toolchain: `rustup`, `cmake`, `make`/`ninja`, and CRAT's build deps (libclang/z3) —
see `tests/e2e/README.md`. Or skip all of it with [Docker](#docker).

## Run one case

```bash
uv run proctor run -c tests/e2e/translation_smoke.toml \
  --input-c tests/e2e/fixtures/001_helloworld/c \
  --tests   tests/e2e/fixtures/001_helloworld/tests
```

`-c` picks the pipeline config. **`translation_smoke.toml` is the baseline Translation
pipeline** — it wires the two stages `c2rust → crat` (C → tested *unsafe* Rust) with no
LLM. Swap it for `configs/c2rust_crat_absrec.toml` to add the `abstraction_recovery` stage.

Each run is a self-contained `runs/<run_id>/` (resolved config, provenance, event log,
per-stage envelopes, outputs, checkpoints).

| Verb | Purpose |
|---|---|
| `proctor validate -c <cfg>` | check pipeline wiring (rejects an artifact with no producer) |
| `proctor stages -c <cfg>` | list configured stages |
| `proctor resume runs/<id> [--from crat]` | reuse checkpoints; redo the rest |
| `proctor bench -c <cfg> --corpus <dir> --jobs 8` | whole corpus, one run dir per case |
| `proctor report runs/ --group-by stage,model` | LLM token/cost aggregation |

## Benchmark a suite

Two harnesses benchmark a suite: **`./bench.sh`** (original, vendored corpus) and
**`./bench_no_falco.sh`** (newer corpus, incl. B03). Both translate every case, then verify it
against the corpus's own test vectors (we drive TRACTOR's authoritative runner, never
reimplement it). They differ because of **Falco**: the newer corpus's runner uses a privileged
Falco/eBPF sidecar to observe *file-change* vectors, which needs kernel privileges a host-level
run can't grant — so its verify runs `--no-falco` and skips only those vectors. The two paths
can't collapse into one command: the newer harness spawns a Docker container per vector (host
level), while the old *direct* harness runs entirely inside the framework container. Pick by
corpus:

| | `./bench.sh` (original) | `./bench_no_falco.sh` |
|---|---|---|
| Corpus | vendored `tractor-test-corpus/` (B01, B02, P00/P01) | newer `tractor-test-corpus-newer/` (adds **B03**, Examples) |
| Translate | in-container `proctor bench` (`bench_vectors.toml`) | in-container `proctor bench` (`bench.toml`) |
| Verify | **inline, same container** → `vector_harness` → vendored `runtests.rust` (pre-Falco *direct* harness; needs only cargo/cmake/ninja) | **separate host-level pass** → `no_falco_bench.py` → newer `tools/test_runner --no-falco` (container-per-vector; needs **nix + docker**) |
| Vector types | all types | all except **file-change** (Falco-only), which are skipped |
| Results in | `bench.json` (verify folded in) | `bench.json` (translated?) + `verify.json` + `verify.xml` |
| Metrics via `bench_report.sh` | vectors only | vectors **+ unsafe + idiomaticity** |

```bash
./fetch_corpus.sh                          # vendored corpus
./fetch_corpus.sh --no-falco               # newer corpus
docker build -t proctor-framework:dev .    # once

./bench.sh B02_organic                     # translate + verify a suite
./bench.sh B02_organic arr_del_lib --all   # one case, verify every stage
JOBS=8 ./bench.sh B01_synthetic

./bench_no_falco.sh B03_organic            # newer corpus / B03
./bench_no_falco.sh B01_synthetic 001_helloworld    # one case (name is a regex)

# verify directly with the corpus's own runner (no translation step):
./no_falco_verify.sh Public-Tests/B03_organic/array_list [<translated_rust>]  # one case
./no_falco_verify.sh Public-Tests/B03_organic     # a whole suite    (C-reference smoke)
./no_falco_verify.sh Public-Tests                 # the whole corpus (C-reference smoke)
```

Each run is one self-contained dir under `out/` (`bench-<suite>-<pid>-<stamp>/`) with the
per-case translations, the JSON/XML above, and `run.log`. Both parallelize translation;
verify serializes (the harness isn't concurrency-safe). The directory forms of
`no_falco_verify.sh` check the C reference across every discovered case (a harness smoke);
to verify *translated Rust* across a suite, use `bench_no_falco.sh`, which translates then verifies.

## Abstraction recovery + the gate

Add the LLM stage via config: `configs/c2rust_crat_absrec.toml` (Claude Code backend,
`ANTHROPIC_API_KEY`) or `configs/c2rust_crat_absrec_llm.toml` (single-model GPT backend,
`OPENAI_API_KEY`).

```bash
CONFIG=configs/c2rust_crat_absrec.toml ./bench_no_falco.sh B03_organic --gate
```

**`--gate` (recommended whenever recovery is on).** The stage's only self-check is
`cargo build`, which can't catch a transform that compiles but changes observable behavior
or the `extern "C"` ABI. With `--gate`, any case the recovery didn't pass cleanly is
re-verified against the previous stage (`crat`) and the non-regressing result is kept —
recovery keeps its safety/idiomaticity wins where correct, falls back to `crat` where not.
`verify.json` records `accepted_stage` and `fell_back` per case.

## Measure safety + idiomaticity

Vendored authoritative tools under `tools/`: DARPA's `measure_unsafety` (a `syn` scorer,
source-only) and Yale's `measure_idiomaticity` (`cargo clippy`). Lower is better for both.

```bash
# one crate or one run dir:
./metrics.sh <crate>                    # unsafe + idiomaticity
./metrics.sh out/bench-.../array_list   # per stage (run dir with stages/)
./metrics.sh <crate> --no-idiomaticity  # unsafe only (no clippy build)
./metrics.sh <crate> --complexity       # + cognitive-complexity histogram
./metrics.sh <crate> --json out.json    # also write JSON

# whole run (vectors + metrics per case):
./bench_report.sh <suite|bench-dir>     # per case: vectors + final-stage unsafe + clippy
./bench_report.sh --per-stage           # per-stage table (c2rust → crat → …) + suite totals
./bench_report.sh --no-idiomaticity     # skip the clippy build
./bench_report.sh --no-metrics          # vectors only (fast)
```

`bench_report.sh` reads either harness's run dir (`verify.json` if present, else `bench.json`);
the unsafe + idiomaticity columns are added for `--no-falco` runs — a `bench.sh` run reports
vectors only, but `metrics.sh` can be pointed at *any* run dir to score it. Unsafe is
source-only (works even if the crate doesn't build); idiomaticity runs clippy (needs the crate
to build). Survey: `plan_docs/{unsafe,idiomaticity}_evaluation_plan.md`.

## Docker

```bash
docker build -t proctor-framework:dev .
docker run --rm proctor-framework:dev run -c tests/e2e/translation_smoke.toml \
  --input-c tests/e2e/fixtures/001_helloworld/c --tests tests/e2e/fixtures/001_helloworld/tests
```

The image bakes in every toolchain (LLVM, rustup, uv, the `claude` CLI) and pre-builds the
stages — containerized runs need zero host setup. Mount a volume over
`/home/proctor/proctor/runs` to keep run dirs.

## Add a stage

A stage is a standalone program (any language) that reads `stage_input.json` and writes
`stage_output.json`.

1. Copy `stages/example-stage/`; declare consumed/produced artifacts in `stage.toml`; pin
   deps in your `pyproject.toml` (isolated venv per stage).
2. `git submodule add <url> stages/<name>`.
3. Wire it in and validate:
   ```toml
   [pipeline]
   order = ["c2rust", "crat", "<name>"]
   [stages.<name>]
   uses = "stages/<name>"
   ```
   `uv run proctor validate -c <cfg>`

Walkthrough: `docs/writing-a-stage.md` · envelope reference: `docs/stage-contract.md`.

## What the framework offers a stage

A stage only has to honor the JSON envelope contract — everything else is an optional
convenience library it can import. What's available, and where each is documented:

- **Stage contract & envelope** — `stage_input.json`/`stage_output.json`, the `proctor.toml`
  manifest, artifact wrappers. The one hard requirement. → `docs/stage-contract.md`,
  `docs/writing-a-stage.md`
- **LLM API** — one vendor-agnostic client (Anthropic / OpenAI / vLLM / cassette replay);
  switch model or provider by config, never code. → `proctor/llm/README.md`
- **Usage & cost tracking** — per-call token/cost records to `usage.jsonl`, aggregated by
  `proctor report`. → `proctor/llm/README.md` (*Usage tracking*)
- **Prompt library** — versioned Jinja2 templates with content hashes for reproducibility.
  → `proctor/prompts/` (templates in `templates/`)
- **Code-context retrieval** — a Rust-backed crate index + `retrieve_context(strategy=, target=)`.
  → `plan_docs/orchestration_framework_implementation_plan.md`
- **Vector harness & metrics** — build + `run_test.sh` runner, vector verification, and the
  unsafe/idiomaticity scorers. → [Benchmark a suite](#benchmark-a-suite),
  [Measure safety + idiomaticity](#measure-safety--idiomaticity)

Full module-by-module map: `proctor/README.md`.

## Develop

```bash
uv run pytest                                        # unit tests (fake stages, no toolchains)
uv run pytest -m e2e                                 # real-stage tests
uv run ruff check . && uv run ruff format . && uv run mypy proctor
```

Config is layered — later `-c` files win, `--set` wins over all:

```bash
uv run proctor run -c base.toml -c experiments/sonnet.toml --set stages.crat.config.final_pass=simpl
```

Design docs: pipeline `plan_docs/orchestration_framework_implementation_plan.md` ·
no-Falco verification `plan_docs/falco_integration_notes.md`. The legacy scripts/container live on `master`.
