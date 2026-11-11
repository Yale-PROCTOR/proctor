"""metrics.record_stage / record_run: the per-stage and run-level
metrics.json the orchestrator writes under ``[metrics]`` — measurement tools stubbed."""

import json
from pathlib import Path
from typing import Any

import pytest

from proctor.testing import metrics
from proctor.testing.diff_eval import DiffReport
from proctor.testing.idiomaticity_eval import IdiomReport
from proctor.testing.unsafe_eval import UnsafeReport


def _stage(run: Path, ordinal: str, sid: str) -> Path:
    rust = run / "stages" / f"{ordinal}-{sid}" / "out" / "rust"
    rust.mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\nname='x'\n", "utf-8")
    return rust


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"unsafe": [], "diff": [], "clippy": []}
    scores = iter([100, 60, 40, 30, 20, 10])

    def fake_unsafe(crate: Path, **kw: Any) -> UnsafeReport:
        calls["unsafe"].append(crate)
        return UnsafeReport.from_stats(
            {"unsafe_score": next(scores), "total_lines": 200}
        )

    def fake_diff(before: Path, after: Path, **kw: Any) -> DiffReport:
        calls["diff"].append((before, after, kw.get("api")))
        return DiffReport.from_json(
            {
                "files_changed": 1,
                "lines_added": 5,
                "lines_removed": 9,
                "abi": {"ok": True},
            }
        )

    def fake_clippy(crate: Path, **kw: Any) -> IdiomReport:
        calls["clippy"].append(crate)
        return IdiomReport(by_group={"style": {"needless_return": 7}}, loc=200)

    monkeypatch.setattr(metrics, "measure_unsafe", fake_unsafe)
    monkeypatch.setattr(metrics, "measure_diff", fake_diff)
    monkeypatch.setattr(metrics, "measure_idiomaticity", fake_clippy)
    return calls


def test_record_stage_writes_unsafe_and_diff(tmp_path: Path, stubs: Any) -> None:
    prev, rust = _stage(tmp_path, "00", "c2rust"), _stage(tmp_path, "01", "crat")
    row = metrics.record_stage(rust.parent.parent, "crat", prev, rust, api=["f"])
    written = json.loads((rust.parent.parent / "metrics.json").read_text())
    assert row == written and written["stage"] == "crat"
    assert written["unsafe"]["score"] == 100
    assert written["diff"]["lines_removed"] == 9 and written["diff"]["abi_ok"]
    assert stubs["diff"] == [(prev, rust, ["f"])]  # the public API scopes the diff


def test_first_stage_has_no_diff(tmp_path: Path, stubs: Any) -> None:
    rust = _stage(tmp_path, "00", "c2rust")
    row = metrics.record_stage(rust.parent.parent, "c2rust", None, rust, api=None)
    assert row is not None and "diff" not in row and not stubs["diff"]


def test_record_stage_is_fail_open(tmp_path: Path, monkeypatch: Any) -> None:
    rust = _stage(tmp_path, "00", "c2rust")

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(metrics, "measure_stage", boom)
    row = metrics.record_stage(rust.parent.parent, "c2rust", None, rust, api=None)
    assert row is not None and "tool exploded" in row["metrics_error"]


def test_record_run_clippy_first_and_last_only(tmp_path: Path, stubs: Any) -> None:
    a = _stage(tmp_path, "00", "c2rust")
    b = _stage(tmp_path, "01", "crat")
    c = _stage(tmp_path, "02", "abstraction_recovery")
    for prev, rust, sid in [
        (None, a, "c2rust"),
        (a, b, "crat"),
        (b, c, "abstraction_recovery"),
    ]:
        metrics.record_stage(rust.parent.parent, sid, prev, rust, api=None)
    out = metrics.record_run(tmp_path, idiomaticity="first_last")
    assert out is not None
    assert stubs["clippy"] == [a, c]  # not the middle stage
    assert [("idiomaticity" in r) for r in out["stages"]] == [True, False, True]
    s = out["summary"]
    assert (s["unsafe_first"], s["unsafe_final"]) == (100, 40)
    assert (s["clippy_first"], s["clippy_final"]) == (7, 7)
    assert (s["lines_added"], s["lines_removed"], s["abi_ok"]) == (10, 18, True)
    assert json.loads((tmp_path / "metrics.json").read_text()) == out


def test_record_run_measures_unrecorded_stages(tmp_path: Path, stubs: Any) -> None:
    a = _stage(tmp_path, "00", "c2rust")
    b = _stage(tmp_path, "01", "crat")  # no per-stage metrics.json (e.g. resumed)
    metrics.record_stage(a.parent.parent, "c2rust", None, a, api=None)
    out = metrics.record_run(tmp_path, idiomaticity="none")
    assert out is not None and [r["stage"] for r in out["stages"]] == ["c2rust", "crat"]
    assert (b.parent.parent / "metrics.json").is_file() and not stubs["clippy"]
