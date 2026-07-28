# PROCTOR Orchestration Framework

Shared infrastructure for the PROCTOR C-to-Rust translation pipeline:
a configurable pipeline orchestrator, vendor-agnostic LLM API, usage
tracker, prompt library, and code-context retrieval.

Pipeline stages are standalone programs in their own repositories,
pinned as git submodules under `stages/`, invoked through a JSON
envelope contract (`docs/stage-contract.md`). Current real stages:
`c2rust` and `crat` — together the spec's Translation component,
turning a TRACTOR C project into a tested unsafe-Rust project carrying
`proctor.toml`.

## Setup

```bash
git clone <this repo> && cd proctor
git submodule update --init stages/crat stages/c2rust
uv sync
uv run proctor warmup -c tests/e2e/translation_smoke.toml   # pre-build stages
```

Host toolchain requirements (or use Docker below, which has them all):
`rustup`, `cmake`, `make`/`ninja`, and CRAT's build deps — see
`tests/e2e/README.md` for the libclang/z3 setup with and without sudo,
and for how the c2rust transpiler is located.

Run the following command to generate test case bundles under
`tractor-test-corpus/bundles`:

```bash
python3 \
  tractor-test-corpus/aws-translate/scripts/package/package.py \
  -o "$(pwd)/tractor-test-corpus/bundles" \
  --root tractor-test-corpus/Test-Corpus
```

You only need to do this when you want to pass a bundle to `c2rust-adapter`.

## Running the pipeline

One test case, C source to tested Rust:

```bash
uv run proctor run -c tests/e2e/translation_smoke.toml \
  --input-c tests/e2e/fixtures/001_helloworld/c \
  --tests   tests/e2e/fixtures/001_helloworld/tests
```

Every run creates a self-contained directory under `runs/<run_id>/`
with the resolved config (`run.toml`), provenance (`run.json`), event
log, per-stage envelopes, outputs, and checkpoints.

Useful verbs:

```bash
uv run proctor validate -c <cfg>                 # check pipeline wiring before running
uv run proctor stages   -c <cfg>                 # list configured stages
uv run proctor resume   runs/<run_id>            # reuse checkpoints, redo the rest
uv run proctor resume   runs/<run_id> --from crat  # force re-run from a stage
uv run proctor bench    -c <cfg> --corpus <dir> --jobs 8   # whole corpus, one run dir per case
uv run proctor report   runs/ --group-by stage,model       # LLM token/cost aggregation
```

### Verifying translations against the TRACTOR vectors

`bench` can check each case's translated Rust against the corpus's own
test vectors, using TRACTOR's authoritative `runtests.rust` harness
(vendored under `tools/tractor_runtests/`) — we drive their runner, we
don't reimplement it. Enable it in config:

```toml
[bench]
verify_vectors = true      # verify the final Rust output per case
verify_all_stages = false  # true: verify every stage's output (per-stage delta)
```

Each corpus case must carry a `test_vectors/` directory (the standard
TRACTOR layout). Results land in `bench.json` (`vectors_ok` per case and
a top-level pass count) and print inline as `vectors 3/3 (crat)`. Needs
only `cargo`/`cmake`/`ninja` on `PATH` — no Docker or Falco. File-change
vectors (the Falco path) are deferred; see
`plan_docs/vector_testing_integration_plan.md` and
`plan_docs/falco_integration_notes.md`.

Experiments are config overlays — later files win, `--set` wins over all:

```bash
uv run proctor run -c base.toml -c experiments/sonnet.toml \
  --set stages.crat.config.final_pass=simpl ...
```

## Running in Docker

```bash
docker build -t proctor-framework:dev .
docker run --rm proctor-framework:dev run -c tests/e2e/translation_smoke.toml \
  --input-c tests/e2e/fixtures/001_helloworld/c \
  --tests   tests/e2e/fixtures/001_helloworld/tests
```

The image bakes in every toolchain (LLVM, rustup, uv) and pre-builds
the stages via `proctor warmup`, so containerized runs need zero host
setup. Mount a volume over `/home/proctor/proctor/runs` to keep run
directories. `PROCTOR_IMAGE` is stamped into each run's `run.json`.

## Adding a stage

A stage is a standalone program in its own repo — any language, any
internal machinery (own LLM client, agent SDK, Claude Code) — that
reads a `stage_input.json` and writes a `stage_output.json`:

1. Start from the template: copy `stages/example-stage/` (or the
   richer scaffold in the `abstraction_recovery` repo). Declare what
   you consume/produce in `stage.toml`; pin your own dependencies in
   your `pyproject.toml` (each stage gets an isolated venv).
2. Pin it here: `git submodule add <url> stages/<name>`.
3. Wire it into a config:

   ```toml
   [pipeline]
   order = ["c2rust", "crat", "<name>"]
   [stages.<name>]
   uses = "stages/<name>"
   [stages.<name>.config]
   your_option = 3
   ```

4. Check the wiring: `uv run proctor validate -c <cfg>` — it rejects
   the pipeline if a required artifact has no producer.

Full walkthrough: `docs/writing-a-stage.md`; field-by-field envelope
reference: `docs/stage-contract.md`.

## Development

```bash
uv run pytest          # unit tests (fake stages, no toolchains needed)
uv run pytest -m e2e   # real-stage tests (see tests/e2e/README.md)
uv run ruff check . && uv run ruff format . && uv run mypy proctor
```

- Design and milestones: `plan_docs/orchestration_framework_implementation_plan.md`
- Stage authors start at `docs/writing-a-stage.md` + `stages/example-stage/`
- The envelope contract: `docs/stage-contract.md`

The legacy translation scripts and container live on the `master`
branch (`docker build -t proctor:june2026 .` there).

## Using the LLM API

Stages that use the shared LLM client configure everything — provider,
model, API-key env var, reasoning effort, pricing — through the `[llm]`
config table; see **`proctor/llm/README.md`** for the full guide.
`stages/example-llm-stage/` is a complete working example:

```bash
export ANTHROPIC_API_KEY=...    # keys always come from env, never config
uv run proctor run -c configs/llm_example.toml \
  --input-rust tests/e2e/fixtures/001_helloworld/c2rust
uv run proctor report runs/ --group-by stage,model   # tokens + cost
```
