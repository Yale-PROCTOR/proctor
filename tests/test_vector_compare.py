"""V2 plumbing: stage-output discovery, per-stage comparison, and the
bench summary shape — all without a real toolchain (run_vectors is
stubbed; its own behaviour is covered by the e2e test)."""

import json
from pathlib import Path

import pytest

from proctor.config.model import PipelineConfig
from proctor.orchestrator.bench import run_bench
from proctor.testing import vector_compare
from proctor.testing.vector_harness import (
    VectorHarnessError,
    VectorReport,
    VectorResult,
)

FAKE = Path(__file__).parent / "fake_stages" / "fake"


def _stage_output(run_dir: Path, ordinal: str, stage_id: str) -> Path:
    rust = run_dir / "stages" / f"{ordinal}-{stage_id}" / "out" / "rust"
    rust.mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    return rust


def test_stage_rust_outputs_orders_and_names(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _stage_output(run_dir, "00", "c2rust")
    _stage_output(run_dir, "01", "crat")
    # a stage that produced no rust project must not appear
    (run_dir / "stages" / "02-report" / "out").mkdir(parents=True)

    outputs = vector_compare.stage_rust_outputs(run_dir)
    assert [sid for sid, _ in outputs] == ["c2rust", "crat"]
    assert outputs[0][1].name == "rust"


def _report(case: str, *, passed: int) -> VectorReport:
    results = [VectorResult("build", "pass")]
    results += [VectorResult(f"test{i}", "pass") for i in range(passed)]
    return VectorReport(case=case, results=tuple(results))


def test_verify_final_uses_last_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _stage_output(run_dir, "00", "c2rust")
    _stage_output(run_dir, "01", "crat")
    case_dir = tmp_path / "case"
    (case_dir / "test_vectors").mkdir(parents=True)

    seen: list[Path] = []

    def fake_run_vectors(rust: Path, cd: Path, **kw: object) -> VectorReport:
        seen.append(rust)
        return _report(cd.name, passed=3)

    monkeypatch.setattr(vector_compare, "run_vectors", fake_run_vectors)
    result = vector_compare.verify_final(run_dir, case_dir, workdir=tmp_path / "wd")

    assert result is not None
    assert result.stage_id == "crat"  # only the last rust-producing stage
    assert len(seen) == 1
    assert seen[0].parent.parent.name == "01-crat"


def test_compare_stages_verifies_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _stage_output(run_dir, "00", "c2rust")
    _stage_output(run_dir, "01", "crat")
    case_dir = tmp_path / "case"
    (case_dir / "test_vectors").mkdir(parents=True)

    monkeypatch.setattr(
        vector_compare,
        "run_vectors",
        lambda rust, cd, **kw: _report(cd.name, passed=2),
    )
    comparison = vector_compare.compare_stages(
        run_dir, case_dir, workdir=tmp_path / "wd"
    )
    assert [s.stage_id for s in comparison.stages] == ["c2rust", "crat"]
    assert all(s.report is not None and s.report.ok for s in comparison.stages)
    assert comparison.delta_table()[0] == {
        "stage": "c2rust",
        "passed": 2,
        "total": 2,
        "ok": True,
    }


def test_compare_stages_captures_harness_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _stage_output(run_dir, "00", "c2rust")
    case_dir = tmp_path / "case"
    (case_dir / "test_vectors").mkdir(parents=True)

    def boom(rust: Path, cd: Path, **kw: object) -> VectorReport:
        raise VectorHarnessError("cargo missing")

    monkeypatch.setattr(vector_compare, "run_vectors", boom)
    comparison = vector_compare.compare_stages(
        run_dir, case_dir, workdir=tmp_path / "wd"
    )
    assert comparison.stages[0].report is None
    assert comparison.stages[0].error == "cargo missing"


# --- bench integration (fake stage + stubbed harness) ---------------------


def _vector_corpus(tmp_path: Path, names: list[str]) -> Path:
    corpus = tmp_path / "corpus"
    for name in names:
        rust = corpus / name / "rust"
        rust.mkdir(parents=True)
        (rust / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        (corpus / name / "test_vectors").mkdir()
    return corpus


def _vector_config() -> PipelineConfig:
    return PipelineConfig.from_dict(
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


def test_bench_records_vector_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _vector_corpus(tmp_path, ["c1", "c2"])
    monkeypatch.setattr(
        vector_compare,
        "run_vectors",
        lambda rust, cd, **kw: _report(cd.name, passed=3),
    )
    result = run_bench(_vector_config(), tmp_path, corpus, name="t")

    assert result.ok
    assert all(o.vectors_ok for o in result.outcomes)

    summary = json.loads((result.bench_dir / "bench.json").read_text())
    assert summary["vectors_ok"] == 2
    case = next(c for c in summary["cases"] if c["name"] == "c1")
    assert case["vectors_ok"] is True
    assert case["vectors"][0]["stage"] == "a"  # the pipeline's only stage
    assert case["vectors"][0]["passed"] == 3
    assert case["vectors"][0]["total"] == 3
    assert case["vectors"][0]["build_ok"] is True
