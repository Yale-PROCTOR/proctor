"""In-place (library-capable) verification: workspace discovery, the
slot-drop + harness invocation (subprocess stubbed), the compare
helpers, and bench routing. No toolchain — the real cando build is
covered by tests/e2e/test_vector_lib_e2e.py."""

import json
from pathlib import Path

import pytest

from proctor.config.model import PipelineConfig
from proctor.orchestrator.bench import _verify_case, run_bench
from proctor.orchestrator.bench import BenchCase, BenchSettings
from proctor.testing import vector_compare
from proctor.testing import vector_harness as vh
from proctor.testing.vector_harness import VectorReport, VectorResult

FAKE = Path(__file__).parent / "fake_stages" / "fake"


def _stage_output(run_dir: Path, ordinal: str, stage_id: str) -> Path:
    rust = run_dir / "stages" / f"{ordinal}-{stage_id}" / "out" / "rust"
    rust.mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    return rust


def _report(case: str, *, passed: int) -> VectorReport:
    results = [VectorResult("build", "pass")]
    results += [VectorResult(f"test{i}", "pass") for i in range(passed)]
    return VectorReport(case=case, results=tuple(results))


def _workspace(tmp_path: Path) -> Path:
    """A minimal corpus workspace: [workspace] root + tools/cando2."""
    ws = tmp_path / "corpus"
    (ws / "tools" / "cando2").mkdir(parents=True)
    (ws / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["*_lib/runner", "tools/cando*"]\n', encoding="utf-8"
    )
    return ws


# --- workspace discovery --------------------------------------------------


def test_find_workspace_root(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    case = ws / "Public-Tests" / "B01" / "x_lib"
    case.mkdir(parents=True)
    assert vh.find_workspace_root(case) == ws.resolve()


def test_find_workspace_root_none_without_cando(tmp_path: Path) -> None:
    plain = tmp_path / "plain" / "case"
    plain.mkdir(parents=True)
    assert vh.find_workspace_root(plain) is None


def test_is_library_case(tmp_path: Path) -> None:
    assert vh.is_library_case(tmp_path / "001_helloworld_lib")
    assert not vh.is_library_case(tmp_path / "001_helloworld")


def test_copy_workspace_skips_git_and_target(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    (ws / ".git").mkdir()
    (ws / "target").mkdir()
    (ws / "tools" / "cando2" / "lib.rs").write_text("", encoding="utf-8")
    dest = vh.copy_workspace(ws, tmp_path / "copy")
    assert (dest / "Cargo.toml").is_file()
    assert (dest / "tools" / "cando2" / "lib.rs").is_file()
    assert not (dest / ".git").exists()
    assert not (dest / "target").exists()


# --- run_vectors_in_place (subprocess stubbed) ----------------------------


def test_run_vectors_in_place_drops_slot_and_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    case = ws / "Public-Tests" / "B01" / "001_hello_lib"
    (case / "test_vectors").mkdir(parents=True)
    translated = tmp_path / "translated"
    translated.mkdir()
    (translated / "Cargo.toml").write_text("[package]\nname='hello'\n", "utf-8")

    seen = {}

    def fake_run(cmd: list[str], **kw: object) -> object:
        junit = Path(cmd[cmd.index("--junit-xml") + 1])
        seen["subset"] = cmd[cmd.index("--subset") + 1]
        junit.write_text(
            f"<testsuites><testsuite name='{seen['subset']}'>"
            "<testcase name='build'/><testcase name='test1'/>"
            "</testsuite></testsuites>",
            encoding="utf-8",
        )

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr("proctor.testing.vector_harness.subprocess.run", fake_run)
    report = vh.run_vectors_in_place(
        translated, case, workspace_root=ws, junit_out=tmp_path / "j.xml"
    )
    # the case's translated_rust slot was populated in place
    assert (case / "translated_rust" / "Cargo.toml").is_file()
    # the harness was scoped to the case's path under the workspace
    assert seen["subset"] == "Public-Tests/B01/001_hello_lib"
    assert report.passed == 1 and report.ok


def test_run_vectors_in_place_rejects_case_outside_workspace(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    outside = tmp_path / "elsewhere" / "case"
    (outside / "test_vectors").mkdir(parents=True)
    translated = tmp_path / "t"
    translated.mkdir()
    (translated / "Cargo.toml").write_text("", encoding="utf-8")
    with pytest.raises(vh.VectorHarnessError, match="not under workspace root"):
        vh.run_vectors_in_place(
            translated, outside, workspace_root=ws, junit_out=tmp_path / "j.xml"
        )


# --- compare helpers (run_vectors_in_place stubbed) -----------------------


def test_compare_stages_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _stage_output(run_dir, "00", "c2rust")
    _stage_output(run_dir, "01", "crat")
    case = tmp_path / "ws" / "case"
    (case / "test_vectors").mkdir(parents=True)
    monkeypatch.setattr(
        vector_compare,
        "run_vectors_in_place",
        lambda rust, cd, **kw: _report(cd.name, passed=1),
    )
    comp = vector_compare.compare_stages_in_place(
        run_dir, case, workspace_root=tmp_path / "ws", workdir=tmp_path / "wd"
    )
    assert [s.stage_id for s in comp.stages] == ["c2rust", "crat"]
    assert all(s.report is not None and s.report.ok for s in comp.stages)


# --- bench routing --------------------------------------------------------


def test_verify_case_lib_without_workspace_errors(tmp_path: Path) -> None:
    case_dir = tmp_path / "001_x_lib"
    (case_dir / "test_vectors").mkdir(parents=True)
    run_dir = tmp_path / "run"
    _stage_output(run_dir, "00", "crat")
    case = BenchCase(name="001_x_lib", inputs={}, source_dir=case_dir)
    settings = BenchSettings(verify_vectors=True)
    comp = _verify_case(settings, case, run_dir, None, None)
    assert comp is not None
    assert comp.stages[0].error is not None
    assert "workspace" in comp.stages[0].error


def _ws_corpus(tmp_path: Path, cases: list[str]) -> Path:
    """A corpus that is also a workspace root (has tools/cando2)."""
    ws = _workspace(tmp_path)
    for name in cases:
        rust = ws / name / "rust"
        rust.mkdir(parents=True)
        (rust / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        (ws / name / "test_vectors").mkdir()
    return ws


def test_bench_routes_through_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _ws_corpus(tmp_path, ["c1", "c1_lib"])
    config = PipelineConfig.from_dict(
        {
            "run": {"provides": ["rust_project"]},
            "bench": {
                "layout": {"rust_project": "rust"},
                "jobs": 2,
                "verify_vectors": True,
            },
            "pipeline": {"order": ["a"]},
            "stages": {"a": {"uses": str(FAKE), "config": {"fail_if_flag": True}}},
        }
    )
    calls: list[str] = []

    def fake_in_place(rust: Path, cd: Path, **kw: object) -> VectorReport:
        calls.append(cd.name)
        return _report(cd.name, passed=2)

    monkeypatch.setattr(vector_compare, "run_vectors_in_place", fake_in_place)
    result = run_bench(config, tmp_path, corpus, name="t")

    assert result.ok
    assert all(o.vectors_ok for o in result.outcomes)
    # verification ran in place (against the workspace copy), for both cases
    assert sorted(calls) == ["c1", "c1_lib"]
    summary = json.loads((result.bench_dir / "bench.json").read_text())
    assert summary["vectors_ok"] == 2
    # the writable workspace copy was made once under the bench dir
    assert (result.bench_dir / "_corpus_ws" / "Cargo.toml").is_file()
