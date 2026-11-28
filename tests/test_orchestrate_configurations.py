"""Configuration matrix orchestration with fake translations and merger."""

import json
import subprocess
import tarfile
import tomllib
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
import tomli_w

from proctor.config.model import PipelineConfig, StageEntry
from proctor.orchestrator.run import RunError, RunResult, StageResult
from scripts import orchestrate


def test_full_matrix_at_limit() -> None:
    parameters = {f"FLAG_{index}": [True, False] for index in range(5)}
    selected, ignore_boolean = orchestrate.combinations(parameters)

    assert not ignore_boolean
    assert len(selected) == 32
    assert selected[0] == dict.fromkeys(parameters, True)
    assert selected[-1] == dict.fromkeys(parameters, False)


def test_large_matrix_pins_booleans_before_expansion() -> None:
    flags = {f"FLAG_{index}": [True, False] for index in range(20)}
    selected, ignore_boolean = orchestrate.combinations(
        {**flags, "WORD_SIZE": [32, 64]}
    )

    assert ignore_boolean
    assert selected == [
        {**dict.fromkeys(flags, True), "WORD_SIZE": 32},
        {**dict.fromkeys(flags, True), "WORD_SIZE": 64},
    ]


def test_large_matrix_keeps_all_nonboolean_combinations() -> None:
    selected, ignore_boolean = orchestrate.combinations({"REPEAT": list(range(33))})

    assert ignore_boolean
    assert selected == [{"REPEAT": repeat} for repeat in range(33)]


def test_prepare_project_preserves_wrapper_for_linked_test_case(tmp_path: Path) -> None:
    common = tmp_path / "common" / "test_case"
    common.mkdir(parents=True)
    (common / "CMakeLists.txt").touch()
    (common / "configuration.json").write_text(
        '{"configurable_variables":{"DEBUG":[true,false]}}'
    )
    case = tmp_path / "sample"
    case.mkdir()
    (case / "CMakeLists.txt").write_text("add_subdirectory(test_case)\n")
    (case / "CMakePresets.json").touch()
    (case / "test_case").symlink_to("../common/test_case")

    project, name = orchestrate.prepare_project(case, tmp_path / "work")

    assert name == "sample"
    assert (project / "CMakeLists.txt").read_text() == "add_subdirectory(test_case)\n"
    assert (project / "CMakePresets.json").is_file()
    assert not (project / "test_case").is_symlink()
    assert orchestrate.read_parameters(project) == {"DEBUG": [True, False]}
    assert (case / "test_case").is_symlink()


def _config_file(tmp_path: Path, *later_stages: str) -> Path:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "stage.toml").write_text(
        'id = "c2rust"\nversion = "1"\nexec = ["fake"]\n'
        '[requires]\nc_project = "required"\n[produces]\nrust_project = true\n'
    )
    stages = {
        "translate": {
            "uses": str(adapter),
            "config": {"cmake_cache_variables": {"UNRELATED": "kept"}},
        },
    }
    for stage_id in later_stages:
        stage_dir = tmp_path / stage_id
        stage_dir.mkdir()
        (stage_dir / "stage.toml").write_text(
            f'id = "{stage_id}"\nversion = "1"\nexec = ["fake"]\n'
            '[requires]\nrust_project = "required"\n[produces]\nrust_project = true\n'
        )
        stages[stage_id] = {"uses": str(stage_dir)}
    config = tmp_path / "config.toml"
    config.write_text(
        tomli_w.dumps(
            {
                "run": {
                    "provides": ["c_project"],
                    "output_dir": str(tmp_path / "runs"),
                },
                "pipeline": {"order": list(stages)},
                "stages": stages,
            }
        )
    )
    return config


@pytest.mark.parametrize("flag_count", [1, 5])
def test_translation_and_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag_count: int
) -> None:
    config = _config_file(tmp_path)
    case = tmp_path / "source" / "test_case"
    case.mkdir(parents=True)
    (case / "CMakeLists.txt").touch()
    parameters = {
        **{f"FLAG_{index}": [True, False] for index in range(flag_count)},
        "MODE": ["fast", "safe"],
    }
    (case / "configuration.json").write_text(
        json.dumps({"configurable_variables": parameters})
    )
    archive_path = tmp_path / "sample.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(case.parent, arcname="bundle")

    expected, pinned = orchestrate.combinations(parameters)
    runs: dict[Path, dict[str, Any]] = {}
    barrier = Barrier(2)

    def translate(variant: PipelineConfig, root: Path, **kwargs: Any) -> RunResult:
        barrier.wait(timeout=5)
        assert kwargs["item"] == "sample"
        assert (
            kwargs["supplied_inputs"]["c_project"] / "test_case/CMakeLists.txt"
        ).is_file()
        defines = variant.stages["translate"].config["cmake_cache_variables"]
        assert defines["UNRELATED"] == "kept"
        run_dir = kwargs["run_dir"]
        runs[run_dir] = {name: defines[name] for name in parameters}
        crate = run_dir / "out" / "rust"
        (crate / "runner").mkdir(parents=True)
        (crate / "tests").mkdir()
        (crate / "tests" / "generated.rs").touch()
        (crate / "test_vectors").mkdir()
        (crate / ".scripts").mkdir()
        (crate / "Cargo.toml").write_text("[package]\n")
        (crate / "proctor.toml").touch()
        (crate / "testinfra.toml").touch()
        (crate / "lib.rs").touch()
        return RunResult(
            run_dir.name,
            run_dir,
            [StageResult("translate", "success", rust_project=crate)],
            final={"rust_project": crate},
        )

    def merge(manifest: Path, destination: Path, *, ignore_boolean: bool) -> None:
        assert ignore_boolean is pinned
        assert len(runs) == len(expected)
        rows = json.loads(manifest.read_text())
        for row, values in zip(rows, expected, strict=True):
            crate = Path(row.pop("dir"))
            assert crate.name == "rust"
            assert set(path.name for path in crate.iterdir()) == {
                "Cargo.toml",
                "lib.rs",
            }
            assert row == values
        merged = destination / "rust"
        merged.mkdir()
        (merged / "Cargo.toml").write_text(
            '[package]\nname = "sample"\n[features]\n'
            "FLAG_0 = []\nMODE_fast = []\nMODE_safe = []\n"
        )

    monkeypatch.setattr(orchestrate, "start_run", translate)
    monkeypatch.setattr(orchestrate, "run_merge", merge)
    monkeypatch.setattr(
        orchestrate,
        "cmake_defaults",
        lambda *args: ({"MODE": "safe", "FLAG_0": "ON"}, None),
    )
    output = tmp_path / "translated_rust"

    orchestrate.orchestrate([config], archive_path, None, output, jobs=2)

    ordered_runs = sorted(
        runs, key=lambda path: int(path.name.removeprefix("variant-"))
    )
    assert [runs[run_dir] for run_dir in ordered_runs] == expected
    for run_dir in runs:
        assert (run_dir / "out/rust/tests/generated.rs").is_file()
        assert (run_dir / "out/rust/proctor.toml").is_file()
    cargo = tomllib.loads((output / "Cargo.toml").read_text())
    assert cargo["features"]["default"] == (
        ["MODE_safe"] if pinned else ["FLAG_0", "MODE_safe"]
    )


def test_c2rust_failure_in_one_configuration_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_file(tmp_path)
    case = tmp_path / "case"
    case.mkdir()
    (case / "CMakeLists.txt").touch()
    (case / "configuration.json").write_text(
        '{"configurable_variables":{"DEBUG":[true,false]}}'
    )
    output = tmp_path / "translated_rust"

    def translate(
        variant: PipelineConfig, root: Path, *, run_dir: Path, **kwargs: Any
    ) -> RunResult:
        if not variant.stages["translate"].config["cmake_cache_variables"]["DEBUG"]:
            return RunResult(
                run_dir.name, run_dir, [StageResult("translate", "failure")]
            )
        crate = run_dir / "rust"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text("[package]\n")
        return RunResult(
            run_dir.name,
            run_dir,
            [StageResult("translate", "success", rust_project=crate)],
            final={"rust_project": crate},
        )

    monkeypatch.setattr(orchestrate, "start_run", translate)
    monkeypatch.setattr(
        orchestrate, "run_merge", lambda *a, **kw: pytest.fail("unexpected merge")
    )

    with pytest.raises(RunError, match="translation failed for"):
        orchestrate.orchestrate([config], case, None, output, jobs=2)
    assert not output.exists()


@pytest.mark.parametrize("fallback", [None, "repair", "translate"])
def test_later_failure_publishes_latest_mergeable_crate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: str | None
) -> None:
    config = _config_file(tmp_path, "repair", "finalize")
    case = tmp_path / "case"
    case.mkdir()
    (case / "CMakeLists.txt").touch()
    (case / "configuration.json").write_text(
        '{"configurable_variables":{"DEBUG":[true,false]}}'
    )
    attempts = [["finalize", "repair"]]
    if fallback is not None:
        attempts.append(["repair", "repair"])
        if fallback == "translate":
            attempts.append(["translate", "translate"])
    observed = []

    def translate(
        variant: PipelineConfig, root: Path, *, run_dir: Path, **kwargs: Any
    ) -> RunResult:
        enabled = variant.stages["translate"].config["cmake_cache_variables"]["DEBUG"]
        stages = []
        for stage in variant.order:
            if stage == "finalize" and not enabled:
                stages.append(StageResult(stage, "failure"))
                break
            crate = run_dir / stage / "rust"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").write_text('[package]\nname = "case"\n')
            (crate / "lib.rs").write_text(stage)
            stages.append(StageResult(stage, "success", rust_project=crate))
        return RunResult(run_dir.name, run_dir, stages, final={"rust_project": crate})

    def merge(manifest: Path, destination: Path, *, ignore_boolean: bool) -> None:
        rows = json.loads(manifest.read_text())
        assert [row["DEBUG"] for row in rows] == [True, False]
        stages = [(Path(row["dir"]) / "lib.rs").read_text() for row in rows]
        observed.append(stages)
        merged = destination / "rust"
        merged.mkdir()
        if len(observed) < len(attempts):
            (merged / "partial").touch()
            raise subprocess.CalledProcessError(1, "crat-merge")
        (merged / "Cargo.toml").write_text(
            '[package]\nname = "case"\n[features]\nDEBUG = []\n'
        )

    monkeypatch.setattr(orchestrate, "start_run", translate)
    monkeypatch.setattr(orchestrate, "run_merge", merge)
    monkeypatch.setattr(orchestrate, "cmake_defaults", lambda *args: ({}, None))
    output = tmp_path / "translated_rust"

    orchestrate.orchestrate([config], case, None, output, jobs=2)

    assert observed == attempts
    assert set(path.name for path in output.iterdir()) == {"Cargo.toml"}


@pytest.mark.parametrize("later_failure", [False, True])
def test_unconfigured_project_publishes_single_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later_failure: bool
) -> None:
    config = _config_file(tmp_path, *(("finalize",) if later_failure else ()))
    case = tmp_path / "case"
    case.mkdir()
    (case / "CMakeLists.txt").touch()
    calls = []

    def translate(*args: Any, run_dir: Path, **kwargs: Any) -> RunResult:
        calls.append(run_dir)
        crate = run_dir / "rust"
        (crate / "tests").mkdir(parents=True)
        (crate / "Cargo.toml").write_text("[package]\n")
        (crate / "proctor.toml").touch()
        stages = [StageResult("translate", "success", rust_project=crate)]
        if later_failure:
            stages.append(StageResult("finalize", "failure"))
        return RunResult(run_dir.name, run_dir, stages, final={"rust_project": crate})

    monkeypatch.setattr(orchestrate, "start_run", translate)
    monkeypatch.setattr(
        orchestrate, "run_merge", lambda *a, **kw: pytest.fail("unexpected merge")
    )
    output = tmp_path / "translated_rust"

    orchestrate.orchestrate([config], case, None, output, jobs=2)

    assert len(calls) == 1
    assert set(path.name for path in output.iterdir()) == {"Cargo.toml"}


def test_cmake_defaults_respect_preset_and_baseline_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "CMakeLists.txt").touch()
    (project / "CMakePresets.json").touch()
    adapter = StageEntry(
        "c2rust",
        Path("unused"),
        config={"preset": "custom", "cmake_cache_variables": {"DEBUG": False}},
    )

    def configure(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        assert command[-3:] == ["--preset", "custom", "-DDEBUG=OFF"]
        assert Path(command[command.index("-S") + 1]) != project
        build = Path(command[command.index("-B") + 1])
        build.mkdir()
        (build / "CMakeCache.txt").write_text(
            "# comment\n// description\nDEBUG:BOOL=OFF\nMODE:STRING=safe\n"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(orchestrate.subprocess, "run", configure)

    assert orchestrate.cmake_defaults(project, tmp_path, "example", adapter) == (
        {"DEBUG": "OFF", "MODE": "safe"},
        "custom",
    )
