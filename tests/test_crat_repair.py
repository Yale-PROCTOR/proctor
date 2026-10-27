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

from proctor.config.load import load_config
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
        raise AssertionError(command)

    def agent(
        self, config: dict, project: Path, prompt: str, directory: Path, env: dict
    ) -> dict:
        self.repairs.append((prompt, project))
        assert (project / "lib.rs").is_file()
        assert config["model"] == "openrouter/openai/gpt-5.6-terra"
        assert env["CARGO_TARGET_DIR"] == env["CARGO_BUILD_BUILD_DIR"]
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


def test_command_timeout_kills_agent_children(tmp_path):
    marker = tmp_path / "child-survived"
    child = (
        "import time; from pathlib import Path; time.sleep(0.4); Path("
        + repr(str(marker))
        + ").touch()"
    )
    parent = (
        "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
        + repr(child)
        + "]); print('ready', flush=True); time.sleep(5)"
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


def test_openrouter_configuration_and_stage_contract():
    config = load_config([ROOT / "configs/c2rust_crat_repair.toml"])
    assert config["pipeline"]["order"] == ["c2rust", "crat_repair"]
    stage = config["stages"]["crat_repair"]
    assert stage["uses"] == "stages/crat-repair"
    resolved = STAGE.resolve_config(stage["config"])
    assert resolved["agent"] == "opencode"
    assert resolved["model"] == "openrouter/openai/gpt-5.6-terra"
    assert resolved["enable_interface"] is False
    assert resolved["max_repair_attempts"] == 3
    manifest = tomllib.loads((ROOT / stage["uses"] / "stage.toml").read_text())
    assert manifest["requires"] == {"rust_project": "required"}
    assert manifest["produces"] == {"rust_project": True}
    assert set(manifest["config"]) == set(STAGE.DEFAULTS)


def test_entrypoint_reports_invalid_envelope(tmp_path):
    source, destination = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text("{}")
    assert STAGE.main(["--input", str(source), "--output", str(destination)]) == 1
    assert StageOutput.read(destination).status == "failure"
