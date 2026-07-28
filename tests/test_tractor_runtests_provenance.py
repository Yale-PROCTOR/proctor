"""Provenance guard for the vendored TRACTOR harness.

`tools/tractor_runtests/runtests` is an unmodified copy of the upstream
runner (Test-Corpus, `deployment/scripts/github-actions/runtests`, at the
commit recorded in PROVENANCE.md). This test checks the vendored files
match that upstream **byte for byte** — same file set, same content — so
the copy can never silently drift.

Skips when the corpus isn't fetched (it's pulled via ./fetch_corpus.sh,
not committed), so CI without corpus access just skips it.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
VENDORED_ROOT = REPO / "tools" / "tractor_runtests"
VENDORED_PKG = VENDORED_ROOT / "runtests"
CORPUS = REPO / "tractor-test-corpus" / "Test-Corpus"
UPSTREAM = "deployment/scripts/github-actions/runtests"


def _upstream_commit() -> str:
    text = (VENDORED_ROOT / "PROVENANCE.md").read_text(encoding="utf-8")
    m = re.search(r"commit \*\*([0-9a-f]{7,40})\*\*", text)
    if not m:
        pytest.skip("PROVENANCE.md records no upstream commit")
    return m.group(1)


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(CORPUS), *args], capture_output=True)


def test_tractor_runtests_provenance() -> None:
    if not (CORPUS / ".git").exists():
        pytest.skip("corpus not fetched (run ./fetch_corpus.sh)")
    commit = _upstream_commit()
    if _git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        pytest.skip(f"corpus checkout does not have commit {commit}")

    # same file set
    vendored = sorted(
        p.relative_to(VENDORED_PKG).as_posix()
        for p in VENDORED_PKG.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert vendored, f"no vendored files under {VENDORED_PKG}"
    listing = _git("ls-tree", "-r", "--name-only", commit, UPSTREAM)
    assert listing.returncode == 0, listing.stderr.decode(errors="replace")
    upstream = sorted(
        Path(line).relative_to(UPSTREAM).as_posix()
        for line in listing.stdout.decode().split()
    )
    assert vendored == upstream, (
        "vendored file set differs from upstream:\n"
        f"  only vendored: {sorted(set(vendored) - set(upstream))}\n"
        f"  only upstream: {sorted(set(upstream) - set(vendored))}"
    )

    # same bytes
    mismatched = []
    for rel in vendored:
        show = _git("show", f"{commit}:{UPSTREAM}/{rel}")
        assert show.returncode == 0, show.stderr.decode(errors="replace")
        if (VENDORED_PKG / rel).read_bytes() != show.stdout:
            mismatched.append(rel)
    assert not mismatched, (
        f"vendored files differ from upstream @ {commit}: {mismatched} "
        f"(re-vendor from {UPSTREAM}, or fix PROVENANCE.md)"
    )
