#!/usr/bin/env bash
# Fetch the TRACTOR test corpus at the exact commit the vendored harness
# targets. The corpus is a large, access-restricted DARPA repo, so it is
# NOT committed/submoduled here — pull it on demand with this script.
#
# Needs read access to the DARPA-TRACTOR-Program repos (your git/gh
# credentials are used). Run from anywhere:
#
#   tools/fetch_corpus.sh                 # Test-Corpus (needed for testing)
#   tools/fetch_corpus.sh --with-aws      # also aws-translate (packaging)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# repo -> pinned commit. Test-Corpus @ 0319ab0 is the direct-harness era
# that matches tools/tractor_runtests (see its PROVENANCE.md).
TEST_CORPUS_REPO="https://github.com/DARPA-TRACTOR-Program/Test-Corpus.git"
TEST_CORPUS_COMMIT="0319ab0ae6fdcc5c354d2fbf7ee646049e5006b1"
AWS_TRANSLATE_REPO="https://github.com/DARPA-TRACTOR-Program/aws-translate.git"
AWS_TRANSLATE_COMMIT="b4bad32031a1983820bc5e2396ec4f9aa2b97721"

fetch_repo() {
  local dest="$1" repo="$2" commit="$3"
  if [ -d "$dest/.git" ] &&
     git -C "$dest" rev-parse -q --verify "${commit}^{commit}" >/dev/null 2>&1; then
    git -C "$dest" checkout -q "$commit"
    echo "present: $dest @ ${commit:0:7}"
    return
  fi
  mkdir -p "$dest"
  git -C "$dest" init -q
  git -C "$dest" remote add origin "$repo" 2>/dev/null ||
    git -C "$dest" remote set-url origin "$repo"
  # fetch just the pinned commit (GitHub serves reachable SHAs); fall back
  # to a full fetch if the server refuses a bare-SHA fetch.
  if git -C "$dest" fetch -q --depth 1 origin "$commit" 2>/dev/null; then
    git -C "$dest" checkout -q FETCH_HEAD
  else
    git -C "$dest" fetch -q origin
    git -C "$dest" checkout -q "$commit"
  fi
  echo "fetched: $dest @ ${commit:0:7}"
}

fetch_repo "$ROOT/tractor-test-corpus/Test-Corpus" \
  "$TEST_CORPUS_REPO" "$TEST_CORPUS_COMMIT"

if [ "${1:-}" = "--with-aws" ]; then
  fetch_repo "$ROOT/tractor-test-corpus/aws-translate" \
    "$AWS_TRANSLATE_REPO" "$AWS_TRANSLATE_COMMIT"
fi

echo "done."
