"""V2 e2e: TRACTOR's authoritative vector harness on a real crat output.

The fixture ``translated_rust`` is a genuine c2rust→crat translation of
001_helloworld (it builds a ``driver`` binary printing "Hello World!").
We drive the vendored ``runtests.rust`` harness against it and assert
all three corpus vectors pass — proving the harness integration end to
end, not a reimplementation of it.

Opt-in (``pytest -m e2e``): needs cargo/rustup. The fixture pins
nightly-2025-06-23 via its ``rust-toolchain`` file; rustup installs it
on first build.
"""

import shutil
from pathlib import Path

import pytest

from proctor.testing.vector_harness import run_vectors

REPO = Path(__file__).parent.parent.parent
CASE = REPO / "tests" / "e2e" / "fixtures" / "001_helloworld"
TRANSLATED = CASE / "translated_rust"

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo/rustup")
def test_real_translation_passes_authoritative_vectors(tmp_path: Path) -> None:
    report = run_vectors(TRANSLATED, CASE, workdir=tmp_path, timeout_s=900)
    assert report.build_ok, report.raw_junit
    assert report.total == 3, report.summary()
    assert report.passed == 3, report.summary()
    assert report.failed == 0
    assert report.ok
