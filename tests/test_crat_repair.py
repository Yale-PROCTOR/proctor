from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from proctor.contracts.stage_io import (
    FrameworkSettings,
    InputArtifacts,
    OutputDestinations,
    StageInput,
    StageOutput,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "crat_repair_main", ROOT / "stages/crat-repair/main.py"
)
assert SPEC is not None and SPEC.loader is not None
STAGE: ModuleType = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STAGE
SPEC.loader.exec_module(STAGE)


@pytest.fixture
def envelope(tmp_path: Path) -> StageInput:
    source = tmp_path / "input"
    source.mkdir()
    (source / "config.toml").write_text('c_exposed_fns = ["answer"]\n')
    (source / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.1.0"\n[lib]\npath = "lib.rs"\n'
    )
    (source / "lib.rs").write_text("pub fn answer() -> i32 { 42 }\n")
    (source / "target").mkdir()
    (source / "target/ignored").write_text("build output")
    return StageInput(
        run_id="test",
        stage_id="crat_repair",
        stage_index=1,
        inputs=InputArtifacts(rust_project=source),
        outputs=OutputDestinations(
            rust_project=tmp_path / "out/rust",
            artifacts_dir=tmp_path / "out/artifacts",
        ),
        framework=FrameworkSettings(workdir=tmp_path / "work"),
        config={"model": "openrouter/openai/gpt-5.6-terra"},
    )


class FakeTools:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], Path, dict[str, str], float]] = []
        self.failed_pass: str | None = None
        self.broken_pass: str | None = None
        self.repairs_needed = 1
        self.repairs: list[tuple[str, Path]] = []
        self.build_timeout = False
        self.config_edit = False
        self.binary = True
        self.gated_binary = False
        self.test_results: list[Any] = []
        self.test_calls: list[Path] = []
        self.test_repairs: list[tuple[str, Path]] = []
        self.test_repair_action: Any = None
        self.test_run_action: Any = None
        self.harnesses = ("tests", "runner")

    def build_crat(self, _config: dict, _log: Path) -> tuple[Path, dict]:
        return Path("/fake/crat"), {}

    def run_command(
        self, command: list[str], *, cwd: Path, env: dict, timeout_s: float, log: Path
    ) -> Any:
        self.commands.append((command, cwd, env, timeout_s))
        if "--pass" in command:
            name = command[command.index("--pass") + 1]
            source = Path(command[-1])
            target = Path(command[command.index("-o") + 1]) / source.name
            shutil.copytree(source, target)
            with (target / "lib.rs").open("a") as stream:
                stream.write(f"// {name}\n")
            if name == "expand":
                for harness in self.harnesses:
                    shutil.rmtree(target / harness, ignore_errors=True)
            if name == self.failed_pass:
                (target / "field_spec_map.json").write_text("invalid partial sidecar")
                return STAGE.CommandResult(1, stderr="Crat failed after partial writes")
            if name == self.broken_pass:
                (target / "broken").touch()
            return STAGE.CommandResult(0)
        if command[:2] == ["cargo", "build"]:
            if self.gated_binary and "--bin" in command:
                return STAGE.CommandResult(
                    1, stderr="binary requires unavailable features"
                )
            if self.build_timeout:
                return STAGE.CommandResult(-9, timed_out=True)
            if (cwd / "broken").exists():
                return STAGE.CommandResult(
                    1, stderr=f"error[E0308]: repair {len(self.repairs)}"
                )
            return STAGE.CommandResult(0)
        if command[:2] == ["cargo", "metadata"]:
            targets = [
                {"kind": ["lib"], "name": "fixture", "src_path": str(cwd / "lib.rs")}
            ]
            if self.binary and (cwd / "main.rs").exists():
                targets.append(
                    {"kind": ["bin"], "name": "app", "src_path": str(cwd / "main.rs")}
                )
            return STAGE.CommandResult(
                0,
                stdout=json.dumps(
                    {
                        "packages": [
                            {
                                "manifest_path": str(cwd / "Cargo.toml"),
                                "targets": targets,
                            }
                        ]
                    }
                ),
            )
        if command[:2] == ["uv", "run"]:
            self.test_calls.append(cwd)
            assert tools_finished(cwd)
            assert "--no-auto-complete" in command
            assert "--coverage" not in command
            if self.test_run_action:
                self.test_run_action(command, cwd)
            result = self.test_results.pop(0) if self.test_results else evaluation()
            if isinstance(result, Exception):
                raise result
            return result
        raise AssertionError(command)

    def agent(
        self, config: dict, project: Path, prompt: str, directory: Path, env: dict
    ) -> dict:
        assert (project / "lib.rs").is_file()
        assert config["model"] == "openrouter/openai/gpt-5.6-terra"
        assert env["CARGO_TARGET_DIR"] == env["CARGO_BUILD_BUILD_DIR"]
        if directory.name.startswith("test-repair-"):
            self.test_repairs.append((prompt, project))
            if self.test_repair_action:
                self.test_repair_action(project, len(self.test_repairs))
        else:
            self.repairs.append((prompt, project))
            if len(self.repairs) >= self.repairs_needed:
                (project / "broken").unlink()
            if self.config_edit:
                (project / "config.toml").write_text('passes = ["prepare"]\n')
        return {
            "metrics": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_read_tokens": 30,
                "cost_usd": 0.01,
            }
        }

    @property
    def passes(self) -> list[str]:
        return [
            command[command.index("--pass") + 1]
            for command, *_ in self.commands
            if "--pass" in command
        ]


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    tools = FakeTools()
    monkeypatch.setattr(STAGE, "build_crat", tools.build_crat)
    monkeypatch.setattr(STAGE, "run_command", tools.run_command)
    monkeypatch.setattr(STAGE, "run_agent", tools.agent)
    return tools


def tools_finished(project: Path) -> bool:
    return "// bin\n" in (project / "lib.rs").read_text()


def evaluation(*, successes=1, failures=0, errors=0, skipped=0, **extra):
    status = (
        "build_failed"
        if extra.get("compiler_errors")
        else "harness_error"
        if extra.get("execution_error")
        else "failed"
        if failures
        or errors
        or any(v["result"]["status"] != "ok" for v in extra.get("verifications", []))
        else "ok"
        if successes + skipped
        else "no_tests"
    )
    payload = {
        "status": status,
        "passed": status == "ok",
        "tests_count": successes + failures + errors + skipped,
        "successes_count": successes,
        "failures_count": failures,
        "errors_count": errors,
        "skipped_count": skipped,
        **extra,
    }
    return STAGE.CommandResult(int(status != "ok"), stdout=json.dumps(payload))


@pytest.fixture
def oracle(envelope):
    source = envelope.inputs.rust_project
    (source / "testinfra.toml").write_text('strategy = "cando2"\ntarget_kind = "lib"\n')
    for name in (
        "runner/src/main.rs",
        "tests/check.rs",
        "test_vectors/test.json",
        ".scripts/run_tests.sh",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {name}\n")
    return envelope


@pytest.mark.parametrize("interface", [False, True])
def test_fixed_pipeline_builds_each_pass_and_preserves_input(
    envelope, tools, interface
):
    original = (envelope.inputs.rust_project / "lib.rs").read_bytes()
    output = STAGE.run_stage(
        replace(envelope, config={**envelope.config, "enable_interface": interface})
    )
    assert output.status == "success", output.error
    expected = [
        "expand",
        "extern",
        "preprocess",
        "outparam",
        "punning",
        "enum",
        "pointer-struct-arrays",
        "pointer-struct-param-fields",
        "pointer-epoch-split",
        "pointer-array-local-provenance",
        "pointer",
        "io",
        "libc",
        "static",
        "simpl",
        *(["interface"] if interface else []),
        "unsafe",
        "unexpand",
        "split",
        "bin",
    ]
    assert tools.passes == expected
    commands = [command for command, *_ in tools.commands]
    for index, command in enumerate(commands):
        if "--pass" in command:
            assert commands[index + 1] == ["cargo", "build", "--quiet"]
    pointer = next(c for c in commands if "--pointer-skip-preprocessing" in c)
    assert pointer[pointer.index("--pass") + 1] == "pointer"
    assert (envelope.inputs.rust_project / "lib.rs").read_bytes() == original
    assert not (output.outputs.rust_project / "target").exists()
    manifest = tomllib.loads((output.outputs.rust_project / "proctor.toml").read_text())
    assert manifest == {
        "target_kind": "library",
        "target_name": "fixture",
        "api_functions": ["answer"],
        "wrappers": [],
    }
    assert not tools.repairs
    assert output.usage.calls == 0
    StageOutput.from_dict(output.to_dict())


def test_failed_crat_output_is_discarded_before_next_pass(envelope, tools):
    tools.failed_pass = "extern"
    output = STAGE.run_stage(envelope)
    assert output.status == "success", output.error
    text = (output.outputs.rust_project / "lib.rs").read_text()
    assert "// expand" in text and "// preprocess" in text and "// extern" not in text
    assert not (output.outputs.rust_project / "field_spec_map.json").exists()
    assert output.metrics["skipped_passes"] == 1
    assert output.metadata["passes"][1]["status"] == "skipped"
    assert not tools.repairs


def test_agent_repairs_latest_diagnostics_before_next_pass(envelope, tools):
    tools.broken_pass = "extern"
    tools.repairs_needed = 2
    output = STAGE.run_stage(envelope)
    assert output.status == "success", output.error
    assert len(tools.repairs) == 2
    assert "repair 0" in tools.repairs[0][0]
    assert "repair 1" in tools.repairs[1][0]
    assert "repair 0" not in tools.repairs[1][0]
    assert "diff" not in tools.repairs[0][0]
    assert "cargo build --quiet" in tools.repairs[0][0]
    assert output.usage.calls == 2
    assert output.usage.input_tokens == 200
    assert output.usage.cached_input_tokens == 60
    assert output.models[0].model == envelope.config["model"]
    assert output.metadata["passes"][1]["status"] == "repaired"
    assert not (output.outputs.rust_project / "broken").exists()


def test_exhausted_repairs_stop_before_next_pass_and_report_usage(envelope, tools):
    tools.broken_pass = "extern"
    tools.repairs_needed = 10
    output = STAGE.run_stage(envelope)
    assert output.status == "failure"
    assert "3 repairs" in output.error
    assert tools.passes == ["expand", "extern"]
    assert len(tools.repairs) == 3
    assert output.usage.calls == 3
    assert output.usage.output_tokens == 60
    assert output.outputs.rust_project is None
    assert not envelope.outputs.rust_project.exists()
    report = json.loads(
        (envelope.outputs.artifacts_dir / "statistics.json").read_text()
    )
    assert report["passes"][-1]["status"] == "failure"
    assert report["usage"]["calls"] == 3


def test_build_timeout_never_becomes_source_repair(envelope, tools):
    tools.build_timeout = True
    output = STAGE.run_stage(envelope)
    assert output.status == "failure" and "timed out" in output.error
    assert not tools.repairs
    assert not tools.passes


def test_repair_cannot_inject_extra_passes(envelope, tools):
    tools.broken_pass = "extern"
    tools.config_edit = True
    output = STAGE.run_stage(envelope)
    assert output.status == "failure" and "protected config.toml" in output.error
    assert tools.passes == ["expand", "extern"]


def test_executable_requires_matching_binary_target(envelope, tools):
    (envelope.inputs.rust_project / "config.toml").write_text('[bin]\nname = "app"\n')
    output = STAGE.run_stage(envelope)
    assert (
        output.status == "failure" and "missing its declared binary app" in output.error
    )
    assert not envelope.outputs.rust_project.exists()


def test_executable_manifest_uses_generated_binary(envelope, tools):
    (envelope.inputs.rust_project / "config.toml").write_text('[bin]\nname = "app"\n')
    (envelope.inputs.rust_project / "main.rs").write_text("fn main() {}\n")
    output = STAGE.run_stage(envelope)
    assert output.status == "success", output.error
    manifest = tomllib.loads((output.outputs.rust_project / "proctor.toml").read_text())
    assert manifest["target_kind"] == "executable"
    assert manifest["target_name"] == "app"
    assert manifest["api_functions"] == []
    assert tools.commands[-1][0] == ["cargo", "build", "--quiet", "--bin", "app"]


def test_feature_gated_binary_must_actually_build(envelope, tools):
    (envelope.inputs.rust_project / "config.toml").write_text('[bin]\nname = "app"\n')
    (envelope.inputs.rust_project / "main.rs").write_text("fn main() {}\n")
    tools.gated_binary = True
    output = STAGE.run_stage(envelope)
    assert output.status == "failure" and "final declared target build" in output.error
    assert not envelope.outputs.rust_project.exists()


@pytest.mark.parametrize(
    "config",
    [
        {"passes": ["expand"]},
        {"final_pass": "bin"},
        {"pass_args": {}},
        {"enable_interface": "true"},
        {"max_repair_attempts": True},
        {"max_repair_attempts": 0},
        {"max_validation_repairs": True},
        {"max_validation_repairs": -1},
        {"max_validation_repairs": 1.5},
        {"validation_timeout_s": 0},
        {"validation_timeout_s": float("inf")},
        {"testinfra_dir": ""},
        {"build_timeout_s": float("nan")},
        {"crat_timeout_s": 0},
        {"agent_timeout_s": True},
    ],
)
def test_invalid_config_is_rejected_before_work(envelope, tools, config):
    output = STAGE.run_stage(replace(envelope, config=config))
    assert output.status == "failure"
    assert not tools.commands
    assert not envelope.framework.workdir.exists()


def test_embedded_project_passes_are_rejected(envelope, tools):
    (envelope.inputs.rust_project / "config.toml").write_text('passes = ["prepare"]\n')
    output = STAGE.run_stage(envelope)
    assert output.status == "failure" and "must not select" in output.error
    assert not tools.commands


def test_input_and_existing_output_are_protected(envelope, tools):
    output = STAGE.run_stage(
        replace(
            envelope,
            framework=FrameworkSettings(workdir=envelope.inputs.rust_project / "work"),
        )
    )
    assert output.status == "failure" and "overlap" in output.error
    envelope.outputs.rust_project.mkdir(parents=True)
    marker = envelope.outputs.rust_project / "keep"
    marker.write_text("original")
    output = STAGE.run_stage(envelope)
    assert output.status == "failure" and "must be new" in output.error
    assert marker.read_text() == "original"
    assert not tools.commands


@pytest.mark.parametrize(
    "key,value,expected",
    [
        (None, None, "-Awarnings"),
        ("RUSTFLAGS", "--cfg custom", "--cfg custom -Awarnings"),
        ("CARGO_BUILD_RUSTFLAGS", "--cfg custom", "--cfg custom -Awarnings"),
        ("CARGO_ENCODED_RUSTFLAGS", "--cfg\x1fcustom", "--cfg\x1fcustom\x1f-Awarnings"),
    ],
)
def test_quiet_build_preserves_flags_and_forces_private_cache(
    monkeypatch, tmp_path, key, value, expected
):
    for name in ("RUSTFLAGS", "CARGO_BUILD_RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CARGO_TARGET_DIR", "/shared/target")
    monkeypatch.setenv("CARGO_BUILD_BUILD_DIR", "/shared/build")
    if key:
        monkeypatch.setenv(key, value)
    env = STAGE.cargo_environment(tmp_path)
    assert env[key or "CARGO_BUILD_RUSTFLAGS"] == expected
    assert env["CARGO_TARGET_DIR"] == env["CARGO_BUILD_BUILD_DIR"] == str(tmp_path)


@pytest.mark.parametrize("detached", [False, True])
def test_command_timeout_kills_agent_children(tmp_path, detached):
    marker = tmp_path / "child-survived"
    child = (
        "import time; from pathlib import Path; time.sleep(0.4); Path("
        + repr(str(marker))
        + ").touch()"
    )
    parent = (
        "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
        + repr(child)
        + f"], start_new_session={detached!r}); print('ready', flush=True); time.sleep(5)"
    )
    result = STAGE.run_command(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout_s=0.15,
        log=tmp_path / "command.log",
    )
    assert result.timed_out
    assert "ready" in result.stdout
    time.sleep(0.5)
    assert not marker.exists()


def test_stage_cancellation_kills_active_command_group(tmp_path):
    ready = tmp_path / "ready"
    survivor = tmp_path / "survivor"
    command = (
        "import time; from pathlib import Path; "
        f"Path({str(ready)!r}).touch(); time.sleep(0.8); "
        f"Path({str(survivor)!r}).touch()"
    )
    script = (
        "import os, runpy, sys; from pathlib import Path; "
        f"stage = runpy.run_path({str(ROOT / 'stages/crat-repair/main.py')!r}); "
        f"stage['run_command']([sys.executable, '-c', {command!r}], "
        f"cwd=Path({str(tmp_path)!r}), env=os.environ.copy(), "
        f"timeout_s=10, log=Path({str(tmp_path / 'cancel.log')!r}))"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 5
        while (
            not ready.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 128 + signal.SIGTERM
        time.sleep(0.9)
        assert not survivor.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("existing_prompt", [False, True])
@pytest.mark.parametrize("existing_config", [False, True])
def test_agent_timeout_cleans_generated_files(
    monkeypatch, tmp_path, existing_prompt, existing_config
):
    project = tmp_path / "project"
    project.mkdir()
    prompt_file, config_file = project / ".agent_prompt.md", project / "opencode.json"
    if existing_prompt:
        prompt_file.write_text("original prompt")
    if existing_config:
        config_file.write_text("original config")

    def timed_out(*_args, **_kwargs):
        prompt_file.write_text("agent prompt")
        if not existing_config:
            config_file.write_text("generated config")
        return STAGE.CommandResult(-9, timed_out=True)

    monkeypatch.setattr(STAGE, "run_command", timed_out)
    result = STAGE.run_agent(
        STAGE.resolve_config({}), project, "fix compilation", tmp_path / "repair", {}
    )
    assert result["timed_out"]
    assert (
        prompt_file.read_text() == "original prompt"
        if existing_prompt
        else not prompt_file.exists()
    )
    assert (
        config_file.read_text() == "original config"
        if existing_config
        else not config_file.exists()
    )


def test_agent_configuration_and_stage_contract(envelope):
    resolved = STAGE.resolve_config(envelope.config)
    assert resolved["agent"] == "opencode"
    assert resolved["model"] == "openrouter/openai/gpt-5.6-terra"
    assert resolved["enable_interface"] is False
    assert resolved["max_repair_attempts"] == 3
    assert resolved["max_validation_repairs"] == 3
    assert resolved["validation_timeout_s"] == 600
    assert resolved["testinfra_dir"] == str(
        (ROOT / "stages/test-infrastructure").resolve()
    )
    manifest = tomllib.loads((ROOT / "stages/crat-repair/stage.toml").read_text())
    assert manifest["requires"] == {"rust_project": "required"}
    assert manifest["produces"] == {"rust_project": True}
    assert set(manifest["config"]) == set(STAGE.DEFAULTS)


def test_entrypoint_reports_invalid_envelope(tmp_path):
    source, destination = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text("{}")
    assert STAGE.main(["--input", str(source), "--output", str(destination)]) == 1
    assert StageOutput.read(destination).status == "failure"


def test_missing_oracle_preserves_compilation_only_behavior(envelope, tools):
    output = STAGE.run_stage(envelope)
    assert output.status == "success", output.error
    assert output.metadata["test_validation"]["status"] == "no_manifest"
    assert not tools.test_calls and not tools.test_repairs
    assert output.metrics["validation_repair_calls"] == 0


def test_final_validation_preserves_original_harness_and_input(oracle, tools):
    source = oracle.inputs.rust_project
    before = {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }

    def inspect(command, tested):
        assert not oracle.outputs.rust_project.exists()
        assert (tested / "proctor.toml").is_file()
        assert (tested / "runner/src/main.rs").read_bytes() == before[
            Path("runner/src/main.rs")
        ]
        assert (tested / "tests/check.rs").read_bytes() == before[
            Path("tests/check.rs")
        ]
        assert tools.commands[-2][0] == ["cargo", "build", "--quiet", "--lib"]
        assert command[command.index("--directory") + 1] == str(
            (ROOT / "stages/test-infrastructure").resolve()
        )

    tools.test_run_action = inspect
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    report = output.metadata["test_validation"]
    assert report["status"] == "validation_succeeded"
    assert report["validation_runs"] == 1 and report["repair_attempts"] == 0
    assert len(tools.test_calls) == 1
    assert not tools.test_repairs
    assert before == {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    assert (
        json.loads((oracle.outputs.artifacts_dir / "test-validation.json").read_text())
        == report
    )
    assert "test-validation.json" in output.logs
    StageOutput.from_dict(output.to_dict())


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_exhausted_test_repairs_publish_last_buildable_output(oracle, tools, limit):
    tools.test_results = [evaluation(failures=1)] * (limit + 1)

    def edit(project, index):
        with (project / "lib.rs").open("a") as stream:
            stream.write(f"// test repair {index}\n")

    tools.test_repair_action = edit
    output = STAGE.run_stage(
        replace(oracle, config={**oracle.config, "max_validation_repairs": limit})
    )
    assert output.status == "success" and output.error is None
    assert len(tools.test_repairs) == limit
    assert len(tools.test_calls) == limit + 1
    report = output.metadata["test_validation"]
    assert report["status"] == "validation_failed"
    assert report["repair_attempts"] == limit
    assert report["reason"] == "repair_limit_reached"
    if limit:
        assert (
            f"// test repair {limit}"
            in (output.outputs.rust_project / "lib.rs").read_text()
        )
    assert output.usage.calls == limit
    assert output.usage.input_tokens == 100 * limit
    assert output.usage.cached_input_tokens == 30 * limit
    assert output.metrics["compilation_repair_calls"] == 0
    assert output.metrics["validation_repair_calls"] == limit
    assert [prompt.id for prompt in output.prompts] == (
        ["crat_test_repair"] if limit else []
    )


def test_test_repairs_have_separate_budget_and_use_latest_diagnostics(oracle, tools):
    tools.broken_pass = "extern"
    tools.test_results = [
        evaluation(failures=1, failing_testcases=[{"message": "first failure"}]),
        evaluation(errors=1, failing_testcases=[{"message": "second failure"}]),
        evaluation(),
    ]
    output = STAGE.run_stage(
        replace(
            oracle,
            config={
                **oracle.config,
                "max_repair_attempts": 1,
                "max_validation_repairs": 2,
            },
        )
    )
    assert output.status == "success", output.error
    assert len(tools.repairs) == 1 and len(tools.test_repairs) == 2
    assert "first failure" in tools.test_repairs[0][0]
    assert "second failure" in tools.test_repairs[1][0]
    assert "first failure" not in tools.test_repairs[1][0]
    assert output.metadata["test_validation"]["status"] == "validation_succeeded"
    assert output.usage.calls == output.metrics["repair_calls"] == 3
    assert output.metrics["compilation_repair_calls"] == 1
    assert output.metrics["validation_repair_calls"] == 2
    assert {prompt.id for prompt in output.prompts} == {
        "crat_compile_repair",
        "crat_test_repair",
    }
    assert output.models[0].model == oracle.config["model"]


@pytest.mark.parametrize(
    "result",
    [
        evaluation(successes=0),
        evaluation(successes=0, skipped=3),
        evaluation(successes=0, compiler_errors=["test build failed"]),
        evaluation(successes=0, execution_error="runner unavailable"),
        STAGE.CommandResult(0, stdout="not JSON"),
        STAGE.CommandResult(0, stdout="[]"),
        STAGE.CommandResult(0, stdout="{}"),
        evaluation(tests_count=True),
        evaluation(tests_count=2),
        evaluation(skipped_count=-1),
        STAGE.CommandResult(1, stdout=evaluation().stdout),
        STAGE.CommandResult(2, stdout=evaluation(failures=1).stdout),
        STAGE.CommandResult(-9, stdout=evaluation(failures=1).stdout),
        STAGE.CommandResult(-9, stdout=evaluation(failures=1).stdout, timed_out=True),
        FileNotFoundError("uv is missing"),
        subprocess.TimeoutExpired("testinfra", 1),
    ],
)
def test_unusable_tests_never_repair_or_fail_translation(oracle, tools, result):
    tools.test_results = [result]
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    report = output.metadata["test_validation"]
    assert report["status"] == "no_executable_tests"
    assert report["repair_attempts"] == 0
    assert len(tools.test_calls) == 1 and not tools.test_repairs
    assert output.usage.calls == 0


@pytest.mark.parametrize("unavailable", [False, True])
def test_runner_setup_mutations_never_reach_published_project(
    oracle, tools, unavailable
):
    def mutate(command, project):
        (project / "lib.rs").write_text("runner setup changed implementation")
        (project / "Cargo.toml").write_text("runner setup changed manifest")
        (project / "test_vectors/test.json").write_text("changed expected output")

    tools.test_run_action = mutate
    tools.test_results = [evaluation(successes=0) if unavailable else evaluation()]
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    published = output.outputs.rust_project
    assert tools_finished(published)
    assert (published / "Cargo.toml").read_bytes() == (
        oracle.inputs.rust_project / "Cargo.toml"
    ).read_bytes()
    assert (
        published / "test_vectors/test.json"
    ).read_text() == "original test_vectors/test.json\n"


def test_unbuildable_repair_is_discarded_and_next_prompt_has_build_diagnostics(
    oracle, tools
):
    (oracle.inputs.rust_project / "keep.rs").write_text("keep module\n")
    tools.test_results = [evaluation(failures=1), evaluation(failures=1), evaluation()]

    def edit(project, index):
        if index == 1:
            (project / "broken").touch()
            (project / "keep.rs").unlink()
            (project / "new.rs").write_text("invalid module")
        else:
            assert not (project / "broken").exists()
            assert not (project / "new.rs").exists()
            assert (project / "keep.rs").read_text() == "keep module\n"
            (project / "lib.rs").write_text(
                (project / "lib.rs").read_text() + "// repaired\n"
            )

    tools.test_repair_action = edit
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert len(tools.test_repairs) == 2
    assert "error[E0308]" in tools.test_repairs[1][0]
    report = output.metadata["test_validation"]
    assert report["attempts"][1]["repair"]["rolled_back"]
    assert not report["attempts"][2]["repair"]["rolled_back"]
    assert not (output.outputs.rust_project / "broken").exists()


@pytest.mark.parametrize("failure", ["exception", "build_timeout", "declared_target"])
def test_test_repair_errors_preserve_buildable_output(oracle, tools, failure):
    tools.test_results = [evaluation(failures=1), evaluation(failures=1)]
    if failure == "declared_target":
        (oracle.inputs.rust_project / "config.toml").write_text('[bin]\nname = "app"\n')
        (oracle.inputs.rust_project / "main.rs").write_text("fn main() {}\n")

    def edit(project, index):
        (project / "broken").touch()
        if failure == "exception":
            raise STAGE.StageFailure("agent driver failed")
        if failure == "build_timeout":
            tools.build_timeout = True
        if failure == "declared_target":
            (project / "broken").unlink()
            tools.gated_binary = True

    tools.test_repair_action = edit
    output = STAGE.run_stage(
        replace(oracle, config={**oracle.config, "max_validation_repairs": 1})
    )
    assert output.status == "success", output.error
    assert output.usage.calls == 1
    assert output.metadata["test_validation"]["attempts"][1]["repair"]["rolled_back"]
    assert not (output.outputs.rust_project / "broken").exists()


def test_agent_failure_can_still_leave_a_passing_buildable_repair(oracle, tools):
    tools.test_results = [evaluation(failures=1), evaluation()]

    def edit(project, index):
        with (project / "lib.rs").open("a") as stream:
            stream.write("// fixed before agent timed out\n")
        raise TimeoutError("agent timed out")

    tools.test_repair_action = edit
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert (
        "fixed before agent timed out"
        in (output.outputs.rust_project / "lib.rs").read_text()
    )
    assert output.metadata["test_validation"]["status"] == "validation_succeeded"


def test_repair_cannot_modify_original_oracle_or_protected_metadata(oracle, tools):
    tools.test_results = [evaluation(failures=1), evaluation()]
    source = oracle.inputs.rust_project
    (source / ".config").mkdir()
    (source / ".config/nextest.toml").write_text("original nextest config\n")

    def edit(project, index):
        for name in ("tests", "runner", "test_vectors", ".scripts", ".config"):
            shutil.rmtree(project / name)
        for name in ("config.toml", "proctor.toml", "testinfra.toml"):
            (project / name).unlink()
        (project / "tests").mkdir()
        (project / "tests/bypassed.rs").write_text("disabled oracle")
        (project / ".config").symlink_to(source / ".config", target_is_directory=True)

    tools.test_repair_action = edit
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    project = output.outputs.rust_project
    for name in (
        "runner/src/main.rs",
        "tests/check.rs",
        "test_vectors/test.json",
        ".scripts/run_tests.sh",
        ".config/nextest.toml",
        "testinfra.toml",
        "config.toml",
    ):
        assert (project / name).read_bytes() == (source / name).read_bytes()
    assert not (project / "tests/bypassed.rs").exists()
    assert (
        tomllib.loads((project / "proctor.toml").read_text())["target_name"]
        == "fixture"
    )


@pytest.mark.parametrize("absolute", [False, True])
def test_custom_runner_is_preserved_and_relocated(oracle, tools, absolute):
    source = oracle.inputs.rust_project
    runner = source / "harness/custom"
    runner.mkdir(parents=True)
    (runner / "Cargo.toml").write_text(
        '[package]\nname = "runner"\nversion = "0.1.0"\n'
    )
    (runner / "main.rs").write_text("original custom runner\n")
    (source / "testinfra.toml").write_text(
        f'strategy = "cando2"\n[cando2]\nrunner = "{runner if absolute else "harness/custom"}"\n'
    )
    tools.harnesses = (*tools.harnesses, "harness/custom")

    def inspect(command, tested):
        assert command[command.index("--runner") + 1] == str(tested / "harness/custom")
        assert (
            tested / "harness/custom/main.rs"
        ).read_text() == "original custom runner\n"

    tools.test_run_action = inspect
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert output.metadata["test_validation"]["status"] == "validation_succeeded"
    assert (
        output.outputs.rust_project / "harness/custom/main.rs"
    ).read_text() == "original custom runner\n"


@pytest.mark.parametrize(
    "manifest",
    [
        "invalid TOML [",
        'strategy="cando2"\nrunner="../outside"\n',
        'strategy="cando2"\nrunner="."\n',
        'strategy="cando2"\nrunner="lib.rs"\n',
        'strategy="cando2"\nrunner="Cargo.toml"\n',
        'strategy="cando2"\nrunner="config.toml"\n',
        'strategy="cando2"\nrunner="missing"\n',
    ],
)
def test_invalid_oracle_metadata_is_nonfatal_and_not_repaired(oracle, tools, manifest):
    (oracle.inputs.rust_project / "testinfra.toml").write_text(manifest)
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert tools_finished(output.outputs.rust_project)
    assert output.metadata["test_validation"]["status"] == "no_executable_tests"
    assert not tools.test_calls and not tools.test_repairs


def test_failed_verification_triggers_test_repair(oracle, tools):
    tools.test_results = [
        evaluation(
            verifications=[
                {
                    "kind": "relocation",
                    "result": json.loads(evaluation(failures=1).stdout),
                }
            ]
        ),
        evaluation(),
    ]
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert len(tools.test_repairs) == 1
    assert output.metadata["test_validation"]["status"] == "validation_succeeded"


def test_unusable_verification_does_not_trigger_repair(oracle, tools):
    tools.test_results = [
        evaluation(
            verifications=[
                {
                    "kind": "relocation",
                    "result": json.loads(
                        evaluation(successes=0, execution_error="runner missing").stdout
                    ),
                }
            ]
        )
    ]
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert not tools.test_repairs
    assert output.metadata["test_validation"]["status"] == "no_executable_tests"


def test_generated_project_self_alias_does_not_recurse(oracle, tools):
    (oracle.inputs.rust_project / "translated_rust").symlink_to(
        ".", target_is_directory=True
    )
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert output.metadata["test_validation"]["status"] == "validation_succeeded"
    assert (oracle.inputs.rust_project / "translated_rust").is_symlink()


def test_stage_deadline_skips_further_test_repairs(oracle, tools, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(STAGE.time, "monotonic", lambda: now[0])
    tools.test_results = [evaluation(failures=1)]
    tools.test_run_action = lambda command, project: now.__setitem__(0, 30.0)
    output = STAGE.run_stage(
        replace(oracle, framework=replace(oracle.framework, timeout_s=20))
    )
    assert output.status == "success", output.error
    assert not tools.test_repairs
    assert output.metadata["test_validation"]["reason"] == "stage_time_budget_exhausted"
    command = next(
        record for record in tools.commands if record[0][:2] == ["uv", "run"]
    )
    assert command[3] == 18


def test_nested_directories_named_target_are_preserved(oracle, tools):
    source = oracle.inputs.rust_project
    for name in ("src/target/mod.rs", "test_vectors/target/case.json"):
        path = source / name
        path.parent.mkdir(parents=True)
        path.write_text("required project content\n")
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    for project in (*tools.test_calls, output.outputs.rust_project):
        for name in ("src/target/mod.rs", "test_vectors/target/case.json"):
            assert (project / name).read_text() == "required project content\n"
    assert not (output.outputs.rust_project / "target").exists()


def test_tests_becoming_unavailable_stop_further_repair(oracle, tools):
    tools.test_results = [
        evaluation(failures=1),
        evaluation(successes=0, execution_error="runner unavailable"),
    ]
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert len(tools.test_repairs) == 1
    assert output.metadata["test_validation"]["status"] == "no_executable_tests"


@pytest.mark.parametrize("workspace", ["validation-0", "repair-1"])
def test_validation_workspace_error_keeps_compilation_only_output(
    oracle, tools, monkeypatch, workspace
):
    copy = STAGE.copy_project

    def unavailable(source, destination):
        if destination.name == workspace:
            raise OSError("validation workspace unavailable")
        copy(source, destination)

    monkeypatch.setattr(STAGE, "copy_project", unavailable)
    tools.test_results = [evaluation(failures=1)]
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert not tools.test_repairs
    assert tools_finished(output.outputs.rust_project)


def test_unusable_validation_directory_never_modifies_input(oracle, tools, monkeypatch):
    build = tools.build_crat

    def preexisting(config, log):
        (oracle.framework.workdir / "test-validation").symlink_to(
            oracle.inputs.rust_project, target_is_directory=True
        )
        return build(config, log)

    monkeypatch.setattr(STAGE, "build_crat", preexisting)
    output = STAGE.run_stage(oracle)
    assert output.status == "success", output.error
    assert output.metadata["test_validation"]["status"] == "no_executable_tests"
    assert not tools.test_calls and not tools.test_repairs
    assert not (oracle.inputs.rust_project / "validation-0").exists()


def test_exhausted_stage_budget_skips_validation_copying(oracle, tools, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(STAGE.time, "monotonic", lambda: now[0])

    def build_crat(config, log):
        now[0] = 30.0
        return tools.build_crat(config, log)

    monkeypatch.setattr(STAGE, "build_crat", build_crat)
    output = STAGE.run_stage(
        replace(oracle, framework=replace(oracle.framework, timeout_s=20))
    )
    assert output.status == "success", output.error
    assert not tools.test_calls and not tools.test_repairs
    assert not (oracle.framework.workdir / "test-validation/validation-0").exists()
    assert output.metadata["test_validation"]["status"] == "no_executable_tests"
