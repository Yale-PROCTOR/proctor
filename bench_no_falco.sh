#!/usr/bin/env bash
# Like bench.sh, but verifies against the NEWER TRACTOR corpus using its own
# orchestrator (tools/test_runner --no-falco) — i.e. the newer cando2, rustc
# 1.94.1, and B03 — instead of the vendored direct harness. Two halves:
#
#   1. TRANSLATE each case IN the framework container (default c2rust -> crat;
#      set CONFIG=configs/c2rust_crat_absrec.toml to add the LLM
#      abstraction_recovery stage), then
#   2. VERIFY each translation against the newer corpus at HOST level
#      (nix + docker, Falco-free) via proctor.testing.no_falco_bench.
#
# The newer orchestrator spawns a container per vector, so step 2 must run at
# host level; file-change vectors are skipped (Falco-only). See
# plan_docs/falco_integration_notes.md.
#
# Concurrency: translations are independent, so runs on different cases
# translate in PARALLEL; only the verify step is serialized (an exclusive lock),
# because TRACTOR's harness removes its vector containers by a shared label and
# builds in the one shared corpus workspace — it isn't concurrency-safe.
#
# The verbose build/harness output goes to a log; only a per-case summary (like
# bench.sh) is printed. Each run is a single self-contained dir under out/,
# owned by you: out/bench-<suite>-<pid>-<stamp>/, containing
#
#   <case>/stages/NN-<stage>/out/rust   the per-case translations, per stage
#   bench.json    translation outcome — did each case translate (from bench CLI)
#   verify.json   verification rollup  — vectors passed/skipped/failed per case
#   verify.xml    verification JUnit   — raw per-vector record (standard JUnit)
#   run.log       the full build + harness log
#
# Override the log path (only) with a *.log arg.
#
#   ./fetch_corpus.sh --no-falco                     # once: fetch the newer corpus
#   ./bench_no_falco.sh B03_organic                  # whole suite
#   ./bench_no_falco.sh B03_organic run.log          # whole suite, custom log path
#   ./bench_no_falco.sh B01_synthetic 001_helloworld # one case (name is a regex)
#   JOBS=8 ./bench_no_falco.sh B02_organic
#
# Requires: docker (framework image proctor-framework:dev), nix, and uv.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SUITE="${1:?usage: ./bench_no_falco.sh <suite> [case] [logfile.log]}"
shift

CASE=""
LOG=""
GATE=""
for arg in "$@"; do
  case "$arg" in
    --gate) GATE="--gate" ;;   # accept abs_rec only if it doesn't regress crat
    --*) echo "unknown flag: $arg" >&2; exit 1 ;;
    *.log|*/*) LOG="$arg" ;;   # a log path (ends in .log or contains a slash)
    *) CASE="$arg" ;;          # a case name/regex
  esac
done

CORPUS="$ROOT/tractor-test-corpus-newer/Test-Corpus"
if [ ! -d "$CORPUS/Public-Tests/$SUITE" ]; then
  echo "no such suite: $SUITE at $CORPUS" >&2
  echo "  fetch the newer corpus first: ./fetch_corpus.sh --no-falco" >&2
  exit 1
fi

MATCH=()
[ -n "$CASE" ] && MATCH=(--match "$CASE")

# nix on PATH (single-user install); host-level docker needs a mountable TMPDIR
if ! command -v nix >/dev/null 2>&1 && [ -f "$HOME/.nix-profile/etc/profile.d/nix.sh" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.nix-profile/etc/profile.d/nix.sh"
fi
export TMPDIR="${TMPDIR:-$HOME/.cache/nofalco-tmp}"
mkdir -p "$TMPDIR"
command -v nix    >/dev/null || { echo "error: nix not found (needed for tools/test_runner)" >&2; exit 1; }
command -v docker >/dev/null || { echo "error: docker not found" >&2; exit 1; }

mkdir -p "$ROOT/out" && chmod 777 "$ROOT/out"

# The bench CLI (translation) creates the run's dir itself, out/bench-<name>-
# <stamp>, owned by the container user; we chown it to you afterward and drop
# the log/JUnit/JSON in there too, so each run is ONE self-contained dir (like
# bench.sh). The log is written during translation, before that dir exists, so
# buffer it in a temp file and move it in after — unless you named a log path.
if [ -n "$LOG" ]; then
  mkdir -p "$(dirname "$LOG")"; : > "$LOG"
  LOGTMP="$LOG"
else
  LOGTMP="$(mktemp "$TMPDIR/bench_no_falco-XXXXXX.log")"
fi

# --- 1. translate the suite in the framework container -----------------------
# Pipeline config (override with CONFIG=... ). The default is a plain
# c2rust -> crat translation (no LLM). To add the abstraction_recovery stage
# (billed per case), set CONFIG to either
# configs/c2rust_crat_absrec.toml      (claude agent backend, ANTHROPIC_API_KEY) or
# configs/c2rust_crat_absrec_llm.toml  (single-model GPT backend, OPENAI_API_KEY).
# Both keys are forwarded into the container below.
CONFIG="${CONFIG:-configs/bench.toml}"
RUNTAG="$$"   # our PID: a per-run tag so parallel runs get distinct bench dirs
TARGET="$SUITE"
[ -n "$CASE" ] && TARGET="$SUITE/$CASE"
echo ">> translating $TARGET  [$(basename "$CONFIG" .toml)] ..."
# Don't let a non-zero translate exit (some case failed to translate — common
# for B03) abort the script under `set -e`: we still want to chown the output
# and verify whatever did translate. The final status comes from the verify.
set +e
docker run --rm \
  -e ANTHROPIC_API_KEY \
  -e OPENAI_API_KEY \
  -v "$CORPUS:/corpus:ro" \
  -v "$ROOT/out:/out" \
  -v "$ROOT/configs:/home/proctor/proctor/configs:ro" \
  -v "$ROOT/proctor:/home/proctor/proctor/proctor:ro" \
  proctor-framework:dev \
  bench -c "$CONFIG" \
  --corpus "/corpus/Public-Tests/$SUITE" --name "$SUITE-$RUNTAG" \
  "${MATCH[@]}" \
  --set run.output_dir=/out \
  --set bench.layout.c_project=. \
  --jobs "${JOBS:-16}" >>"$LOGTMP" 2>&1
set -e

# This run's dir (its unique --name tag makes it unambiguous even when other
# runs translate in parallel). It holds the per-case, per-stage outputs
# (<case>/stages/NN-<stage>/out/rust) — the same layout bench.sh produces.
BENCH_DIR="$(ls -dt "$ROOT"/out/bench-"$SUITE-$RUNTAG"-* 2>/dev/null | head -1 || true)"
[ -n "$BENCH_DIR" ] || { echo "error: translation produced no bench dir; see $LOGTMP" >&2; exit 1; }

# The bench CLI ran as the container's `proctor` user (uid 1001), so this dir
# is owned by that uid, not you. Chown it to the invoking host user (needs
# root, hence a throwaway root container) so the whole run dir — translations
# and the verify artifacts we add next — is yours to read, edit, and delete.
docker run --rm --user root -v "$ROOT/out:/out" --entrypoint chown \
  proctor-framework:dev -R "$(id -u):$(id -g)" "/out/$(basename "$BENCH_DIR")" \
  || echo "warning: couldn't chown $BENCH_DIR to you; it stays container-owned"

# Settle the log into the (now host-owned) run dir, unless a custom path was
# given. The verify writes its JUnit + JSON here too — one self-contained dir.
if [ -z "$LOG" ]; then
  LOG="$BENCH_DIR/run.log"
  mv "$LOGTMP" "$LOG"
fi

# --- 2. verify each translation against the newer corpus, Falco-free --------
# Serialize just this step: the newer harness removes its vector containers by a
# shared label and builds cando runners in the one shared corpus workspace, so
# two verifies at once corrupt each other. Translations above already ran in
# parallel; here we wait our turn behind any other run's verify.
echo ">> verifying against the newer corpus (--no-falco) ..."
exec 9>"$ROOT/out/.bench_no_falco.verify.lock"
if ! flock -n 9; then
  echo "   (another run is verifying — the harness isn't concurrency-safe; waiting our turn)"
  flock 9
fi
# `uv run` finds the project by walking up from CWD, so run it from the repo
# root (all args below are already absolute). Without this, invoking the script
# by absolute path from another directory fails the verify after the costly
# translate has already run.
cd "$ROOT"
uv run python -m proctor.testing.no_falco_bench \
  --bench-dir "$BENCH_DIR" \
  --corpus "$CORPUS" \
  --suite "$SUITE" \
  "${MATCH[@]}" \
  $GATE \
  --junit-out "$BENCH_DIR/verify.xml" \
  --log-file "$LOG"
echo "run dir: $BENCH_DIR  (translations + log + JUnit + JSON)"
