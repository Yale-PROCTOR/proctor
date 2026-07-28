"""V-lib e2e: verify a library (cando) case in place, Falco-free.

The fixture ``translated_rust`` is a real c2rust→crat translation of
001_helloworld_lib — a cdylib named ``hello``, matching the runner's
``library: "hello"``. We copy the corpus workspace, drop the fixture
into the case slot, and run TRACTOR's direct harness: it builds cando2 +
the ``_001_helloworld_lib_runner`` and runs it against the state vector.

Opt-in (``pytest -m e2e``): needs cargo/rustup and the TRACTOR test
corpus at the direct-harness era (0319ab0) — fetch it with
``./fetch_corpus.sh``. First run builds cando2 + the runner (tens of
seconds).
"""

import shutil
from pathlib import Path

import pytest

from proctor.testing.vector_harness import (
    copy_workspace,
    find_workspace_root,
    run_vectors_in_place,
)

REPO = Path(__file__).parent.parent.parent
FIXTURE = REPO / "tests" / "e2e" / "fixtures" / "001_helloworld_lib" / "translated_rust"
CASE = (
    REPO
    / "tractor-test-corpus"
    / "Test-Corpus"
    / "Public-Tests"
    / "B01_synthetic"
    / "001_helloworld_lib"
)

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo/rustup")
@pytest.mark.skipif(not CASE.is_dir(), reason="corpus submodule not present")
def test_library_case_verifies_in_place(tmp_path: Path) -> None:
    ws_root = find_workspace_root(CASE)
    assert ws_root is not None, (
        "corpus must be at the direct-harness era (0319ab0) with tools/cando2"
    )
    ws_copy = copy_workspace(ws_root, tmp_path / "ws")
    case_in_copy = ws_copy / CASE.resolve().relative_to(ws_root.resolve())

    report = run_vectors_in_place(
        FIXTURE,
        case_in_copy,
        workspace_root=ws_copy,
        junit_out=tmp_path / "lib.xml",
        timeout_s=900,
    )
    assert report.build_ok, report.raw_junit
    assert report.failed == 0, report.summary()
    assert report.passed >= 1
    assert report.ok
