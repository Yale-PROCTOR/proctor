#!/usr/bin/env bash
# Bench a corpus suite through c2rust -> crat with vector verification, in
# the framework container. The full Test-Corpus is mounted (library cases
# need the workspace root); --corpus points at the suite. The repo's
# configs/ and proctor/ are mounted too, so config and orchestrator-code
# edits take effect without rebuilding the image (the built c2rust/crat,
# stage adapters, and harness stay baked in — rebuild if those change).
#
#   ./bench.sh B02_organic                 # whole suite, final stage
#   ./bench.sh B02_organic --all           # whole suite, per-stage delta
#   ./bench.sh B02_organic arr_del_lib      # one case (name is a regex)
#   ./bench.sh B02_organic arr_del_lib --all
#   JOBS=8 ./bench.sh B01_synthetic
#
# Output: one dir per run under out/, chowned to you:
# out/bench-<suite>-<pid>-<timestamp>/, containing
#   <case>/stages/NN-<stage>/out/rust   the per-case translations, per stage
#   bench.json    per-case run status + vector results (vectors_ok / passed /
#                 failed / total) — bench.sh translates and verifies in one step,
#                 so both land in bench.json (no separate verify.* files)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SUITE="${1:?usage: ./bench.sh <suite> [case] [--all]   e.g. B02_organic arr_del_lib}"
shift

ALL="false"
CASE=""
for arg in "$@"; do
  case "$arg" in
    --all) ALL="true" ;;
    --*) echo "unknown flag: $arg" >&2; exit 1 ;;
    *) CASE="$arg" ;;  # a case name/regex -> bench --match
  esac
done

CORPUS="$ROOT/tractor-test-corpus/Test-Corpus"
if [ ! -d "$CORPUS/Public-Tests/$SUITE" ]; then
  echo "no such suite: $SUITE   (fetch the corpus with ./fetch_corpus.sh?)" >&2
  exit 1
fi

MATCH=()
[ -n "$CASE" ] && MATCH=(--match "$CASE")

RUNTAG="$$"   # our PID: a per-run tag so concurrent same-suite runs get distinct
              # bench dirs (and each chowns only its own), like bench_no_falco.sh

mkdir -p "$ROOT/out" && chmod 777 "$ROOT/out"

set +e
docker run --rm \
  -v "$CORPUS:/corpus:ro" \
  -v "$ROOT/out:/out" \
  -v "$ROOT/configs:/home/proctor/proctor/configs:ro" \
  -v "$ROOT/proctor:/home/proctor/proctor/proctor:ro" \
  proctor-framework:dev \
  bench -c configs/bench_vectors.toml \
  --corpus "/corpus/Public-Tests/$SUITE" --name "$SUITE-$RUNTAG" \
  "${MATCH[@]}" \
  --set run.output_dir=/out \
  --set "bench.verify_all_stages=$ALL" \
  --jobs "${JOBS:-16}"
RC=$?
set -e

# The bench CLI ran as the container's `proctor` user (uid 1001), so this run's
# out/bench-* dir is owned by that uid, not you. Chown it to the invoking host
# user (needs root, hence a throwaway root container) so its per-case
# translations and bench.json are yours to read, edit, and delete. Preserve the
# bench exit code (non-zero when a case/vector failed).
BENCH_DIR="$(ls -dt "$ROOT"/out/bench-"$SUITE-$RUNTAG"-* 2>/dev/null | head -1 || true)"
if [ -n "$BENCH_DIR" ]; then
  docker run --rm --user root -v "$ROOT/out:/out" --entrypoint chown \
    proctor-framework:dev -R "$(id -u):$(id -g)" "/out/$(basename "$BENCH_DIR")" \
    || echo "warning: couldn't chown $BENCH_DIR to you; it stays container-owned"
fi
exit "$RC"
