"""Orchestrator behavior via the configurable fake stage: sequencing,
artifact wiring, failure policy, timeout, checkpoint/resume."""

import json
from pathlib import Path
from typing import Any

from proctor.config.model import PipelineConfig
from proctor.contracts.stage_io import StageInput
from proctor.orchestrator.events import read_events
from proctor.orchestrator.run import resume_run, start_run

FAKE = Path(__file__).parent / "fake_stages" / "fake"


def _config(
    stage_configs: dict[str, dict[str, Any]],
    *,
    on_failure: str = "stop",
) -> PipelineConfig:
    return PipelineConfig.from_dict(
        {
            "run": {"provides": ["rust_project"], "on_stage_failure": on_failure},
            "pipeline": {"order": list(stage_configs)},
            "stages": {
                sid: {"uses": str(FAKE), "config": cfg}
                for sid, cfg in stage_configs.items()
            },
        }
    )


def _source_project(tmp_path: Path) -> Path:
    src = tmp_path / "src_project"
    src.mkdir()
    (src / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    return src


def _start(config: PipelineConfig, tmp_path: Path, src: Path) -> Any:
    return start_run(
        config,
        tmp_path,
        name="test",
        supplied_inputs={"rust_project": src},
        config_files=[],
        overrides=[],
    )


def test_two_stage_chain(tmp_path: Path) -> None:
    config = _config({"a": {"marker": "A"}, "b": {"marker": "B"}})
    result = _start(config, tmp_path, _source_project(tmp_path))
    assert result.ok
    assert [s.status for s in result.stages] == ["success", "success"]

    # stage b consumed stage a's output, not the original input
    envelope = StageInput.read(result.run_dir / "stages" / "01-b" / "stage_input.json")
    assert envelope.inputs.rust_project == (
        result.run_dir / "stages" / "00-a" / "out" / "rust"
    )
    # the final project went through both stages
    final_marker = (result.final["rust_project"] / "MARKER.txt").read_text()
    assert final_marker.startswith("B:")
    # run dir is self-contained
    assert (result.run_dir / "run.toml").is_file()
    assert (result.run_dir / "run.json").is_file()
    assert (result.run_dir / "inputs" / "rust" / "Cargo.toml").is_file()
    events = [e["event"] for e in read_events(result.run_dir / "events.jsonl")]
    assert events[0] == "run_started" and events[-1] == "run_finished"


def test_failure_stops_pipeline(tmp_path: Path) -> None:
    config = _config({"a": {"behavior": "fail"}, "b": {}})
    result = _start(config, tmp_path, _source_project(tmp_path))
    assert not result.ok
    assert [s.status for s in result.stages] == ["failure"]
    assert result.stages[0].error == "deliberate failure"
    assert not (result.run_dir / "stages" / "01-b").exists()


def test_failure_continue_policy(tmp_path: Path) -> None:
    config = _config(
        {"a": {"behavior": "fail"}, "b": {"marker": "B"}}, on_failure="continue"
    )
    result = _start(config, tmp_path, _source_project(tmp_path))
    assert [s.status for s in result.stages] == ["failure", "success"]
    # b ran on the original input since a produced nothing
    envelope = StageInput.read(result.run_dir / "stages" / "01-b" / "stage_input.json")
    assert envelope.inputs.rust_project == result.run_dir / "inputs" / "rust"


def test_skipped_forwards_input(tmp_path: Path) -> None:
    config = _config({"a": {"behavior": "skip"}, "b": {"marker": "B"}})
    result = _start(config, tmp_path, _source_project(tmp_path))
    assert result.ok
    assert [s.status for s in result.stages] == ["skipped", "success"]
    envelope = StageInput.read(result.run_dir / "stages" / "01-b" / "stage_input.json")
    assert envelope.inputs.rust_project == result.run_dir / "inputs" / "rust"


def test_timeout_kills_stage(tmp_path: Path) -> None:
    config = PipelineConfig.from_dict(
        {
            "run": {"provides": ["rust_project"]},
            "pipeline": {"order": ["slow"]},
            "stages": {
                "slow": {
                    "uses": str(FAKE),
                    "timeout_s": 1,
                    "config": {"behavior": "hang", "sleep_s": 30},
                }
            },
        }
    )
    result = _start(config, tmp_path, _source_project(tmp_path))
    assert not result.ok
    assert "timed out" in (result.stages[0].error or "")
    assert result.stages[0].duration_s < 15


def test_missing_output_is_contract_violation(tmp_path: Path) -> None:
    config = _config({"a": {"behavior": "bad_output"}})
    result = _start(config, tmp_path, _source_project(tmp_path))
    assert not result.ok
    assert "no valid stage_output.json" in (result.stages[0].error or "")


def _markers(run_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for stage_dir in (run_dir / "stages").iterdir():
        marker = stage_dir / "out" / "rust" / "MARKER.txt"
        if marker.is_file():
            out[stage_dir.name] = marker.read_text()
    return out


def test_resume_reuses_everything(tmp_path: Path) -> None:
    config = _config({"a": {"marker": "A"}, "b": {"marker": "B"}})
    first = _start(config, tmp_path, _source_project(tmp_path))
    assert first.ok
    before = _markers(first.run_dir)

    second = resume_run(first.run_dir, tmp_path)
    assert second.ok
    assert [s.status for s in second.stages] == ["reused", "reused"]
    assert _markers(first.run_dir) == before  # nothing re-ran


def test_resume_reruns_after_config_change(tmp_path: Path) -> None:
    config = _config({"a": {"marker": "A"}, "b": {"marker": "B"}})
    first = _start(config, tmp_path, _source_project(tmp_path))
    before = _markers(first.run_dir)

    # editing stage b's config in run.toml invalidates b but not a
    run_toml = first.run_dir / "run.toml"
    run_toml.write_text(
        run_toml.read_text(encoding="utf-8").replace('"B"', '"B2"'),
        encoding="utf-8",
    )
    second = resume_run(first.run_dir, tmp_path)
    assert second.ok
    assert [s.status for s in second.stages] == ["reused", "success"]
    after = _markers(first.run_dir)
    assert after["00-a"] == before["00-a"]
    assert after["01-b"] != before["01-b"]
    assert after["01-b"].startswith("B2:")


def test_resume_from_forces_reexecution(tmp_path: Path) -> None:
    config = _config({"a": {"marker": "A"}, "b": {"marker": "B"}})
    first = _start(config, tmp_path, _source_project(tmp_path))
    before = _markers(first.run_dir)

    second = resume_run(first.run_dir, tmp_path, force_from="b")
    assert second.ok
    assert [s.status for s in second.stages] == ["reused", "success"]
    after = _markers(first.run_dir)
    assert after["00-a"] == before["00-a"]
    assert after["01-b"] != before["01-b"]


def test_resume_completes_failed_run(tmp_path: Path) -> None:
    config = _config({"a": {"behavior": "fail"}, "b": {"marker": "B"}})
    first = _start(config, tmp_path, _source_project(tmp_path))
    assert not first.ok

    # "fix" stage a by rewriting its config in run.toml
    run_toml = first.run_dir / "run.toml"
    run_toml.write_text(
        run_toml.read_text(encoding="utf-8").replace(
            'behavior = "fail"', 'marker = "A"'
        ),
        encoding="utf-8",
    )
    second = resume_run(first.run_dir, tmp_path)
    assert second.ok
    assert [s.status for s in second.stages] == ["success", "success"]


def test_run_record_contents(tmp_path: Path) -> None:
    config = _config({"a": {}})
    result = _start(config, tmp_path, _source_project(tmp_path))
    record = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["run_id"] == result.run_id
    assert record["stages"][0]["id"] == "a"
    assert record["stages"][0]["fingerprint"]
    assert record["inputs"] == {"rust_project": "inputs/rust"}
    assert record["resolved_config_hash"]


def test_testing_gate_blocks_failing_stage(tmp_path: Path, monkeypatch: Any) -> None:
    from proctor.testing import runner as testing_runner
    from proctor.testing.runner import TestResult

    def fake_run_tests(project: Path, package: Path, **kwargs: Any) -> TestResult:
        return TestResult(
            build_ok=True,
            passed=False,
            exit_code=1,
            duration_s=0.1,
            stdout="",
            stderr="diff mismatch",
        )

    monkeypatch.setattr(testing_runner, "run_tests", fake_run_tests)

    tests_dir = tmp_path / "tests_pkg"
    (tests_dir / "test_data").mkdir(parents=True)
    (tests_dir / "run_test.sh").write_text("#!/bin/sh\nexit 0\n")

    config = PipelineConfig.from_dict(
        {
            "run": {"provides": ["rust_project", "test_package"]},
            "testing": {"after_each_stage": True},
            "pipeline": {"order": ["a"]},
            "stages": {"a": {"uses": str(FAKE)}},
        }
    )
    result = start_run(
        config,
        tmp_path,
        name="gated",
        supplied_inputs={
            "rust_project": _source_project(tmp_path),
            "test_package": tests_dir,
        },
        config_files=[],
        overrides=[],
    )
    assert not result.ok
    assert "post-stage gate" in (result.stages[0].error or "")


def test_testing_gate_allows_passing_stage(tmp_path: Path, monkeypatch: Any) -> None:
    from proctor.testing import runner as testing_runner
    from proctor.testing.runner import TestResult

    monkeypatch.setattr(
        testing_runner,
        "run_tests",
        lambda project, package, **kwargs: TestResult(
            build_ok=True,
            passed=True,
            exit_code=0,
            duration_s=0.1,
            stdout="ok",
            stderr="",
        ),
    )
    tests_dir = tmp_path / "tests_pkg"
    (tests_dir / "test_data").mkdir(parents=True)
    (tests_dir / "run_test.sh").write_text("#!/bin/sh\nexit 0\n")

    config = PipelineConfig.from_dict(
        {
            "run": {"provides": ["rust_project", "test_package"]},
            "testing": {"after_each_stage": True},
            "pipeline": {"order": ["a"]},
            "stages": {"a": {"uses": str(FAKE)}},
        }
    )
    result = start_run(
        config,
        tmp_path,
        name="gated-ok",
        supplied_inputs={
            "rust_project": _source_project(tmp_path),
            "test_package": tests_dir,
        },
        config_files=[],
        overrides=[],
    )
    assert result.ok
    events = read_events(result.run_dir / "events.jsonl")
    test_events = [e for e in events if e["event"] == "test_result"]
    assert test_events and test_events[0]["ok"] is True


def _metrics_config(
    stages: dict[str, dict[str, Any]], **metrics: Any
) -> PipelineConfig:
    return PipelineConfig.from_dict(
        {
            "run": {"provides": ["rust_project"]},
            "pipeline": {"order": list(stages)},
            "stages": {
                sid: {"uses": str(FAKE), "config": c} for sid, c in stages.items()
            },
            "metrics": {"after_each_stage": True, **metrics},
        }
    )


def _stub_measurement(monkeypatch: Any) -> list[tuple[Any, Any]]:
    from proctor.testing import metrics
    from proctor.testing.diff_eval import DiffReport
    from proctor.testing.unsafe_eval import UnsafeReport

    diffs: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        metrics,
        "measure_unsafe",
        lambda c, **k: UnsafeReport.from_stats({"unsafe_score": 3, "total_lines": 10}),
    )

    def fake_diff(before: Any, after: Any, **k: Any) -> DiffReport:
        diffs.append((before, after))
        return DiffReport.from_json(
            {"files_changed": 1, "lines_added": 1, "abi": {"ok": True}}
        )

    monkeypatch.setattr(metrics, "measure_diff", fake_diff)
    monkeypatch.setattr(
        metrics, "_clippy", lambda rust, loc: {"idiomaticity": {"total": 2}}
    )
    return diffs


def test_metrics_written_after_each_stage(tmp_path: Path, monkeypatch: Any) -> None:
    diffs = _stub_measurement(monkeypatch)
    config = _metrics_config({"a": {"marker": "A"}, "b": {"marker": "B"}})
    src = _source_project(tmp_path)
    result = _start(config, tmp_path, src)
    assert result.ok
    stages = result.run_dir / "stages"
    a = json.loads((stages / "00-a" / "metrics.json").read_text())
    b = json.loads((stages / "01-b" / "metrics.json").read_text())
    assert a["stage"] == "a" and b["stage"] == "b"
    # each stage is diffed against the Rust it was given
    assert diffs[0][1] == stages / "00-a" / "out" / "rust"
    assert diffs[1] == (
        stages / "00-a" / "out" / "rust",
        stages / "01-b" / "out" / "rust",
    )
    run = json.loads((result.run_dir / "metrics.json").read_text())
    assert [r["stage"] for r in run["stages"]] == ["a", "b"]
    assert run["summary"]["clippy_first"] == 2
    events = [e["event"] for e in read_events(result.run_dir / "events.jsonl")]
    assert events.count("stage_metrics") == 2


def test_metrics_off_by_default(tmp_path: Path) -> None:
    result = _start(
        _config({"a": {"marker": "A"}}), tmp_path, _source_project(tmp_path)
    )
    assert result.ok
    assert not (result.run_dir / "metrics.json").exists()
    assert not (result.run_dir / "stages" / "00-a" / "metrics.json").exists()


def test_metrics_failure_never_fails_the_run(tmp_path: Path, monkeypatch: Any) -> None:
    from proctor.testing import metrics

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("measure_unsafety missing")

    monkeypatch.setattr(metrics, "_measure", boom)
    config = _metrics_config({"a": {"marker": "A"}})
    result = _start(config, tmp_path, _source_project(tmp_path))
    assert result.ok
    row = json.loads((result.run_dir / "stages" / "00-a" / "metrics.json").read_text())
    assert "measure_unsafety missing" in row["metrics_error"]
