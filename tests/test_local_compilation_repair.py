from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from compilation_repair import CompilationRepair
from tooling import CommandResult


MODEL = "openrouter/openai/gpt-6.1-sol"


def project(tmp_path: Path, name: str = "rust") -> Path:
    result = tmp_path / name
    result.mkdir()
    (result / "lib.rs").write_text("uncompilable translation\n")
    (result / "Cargo.toml").write_text('[lib]\npath = "lib.rs"\n')
    return result


def repair(tmp_path, build, runner, **settings):
    return CompilationRepair(
        workdir=tmp_path / "work",
        artifacts=tmp_path / "artifacts",
        config={
            "validation_repair_agent": "opencode",
            "validation_repair_model": MODEL,
            "validation_repair_timeout_s": 900,
            **settings,
        },
        build=build,
        analysis_project=tmp_path / "analysis",
        c_project=tmp_path / "original-c.tar.gz",
        runner=runner,
    )


def test_buildable_project_does_not_need_an_agent_or_test_manifest(tmp_path):
    rust = project(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("a buildable project needs no repair")

    loop = repair(
        tmp_path,
        lambda path: CommandResult(0, "successful build"),
        unexpected,
        validation_repair_model=None,
    )
    assert loop.ensure(rust)
    assert loop.report["status"] == "compilation_succeeded"
    assert loop.report["repair_attempts"] == 0
    assert loop.report["compilation_runs"] == 1
    saved = json.loads((tmp_path / "artifacts/compilation-repair.json").read_text())
    assert saved == loop.report


def test_agent_changes_are_not_policed_and_cargo_alone_accepts_them(tmp_path):
    rust = project(tmp_path)
    (rust / "tests").mkdir()
    (rust / "tests/example.rs").write_text("original test\n")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        assert command[0:2] == ["opencode", "run"]
        assert "--auto" in command
        assert command[command.index("--dir") + 1] == str(rust)
        assert command[command.index("--model") + 1] == MODEL
        assert not (rust / "opencode.json").exists()
        assert not (rust / ".validation-repair-prompt.md").exists()
        prompt = Path(command[command.index("-f") + 1]).read_text()
        assert "Make the smallest changes necessary" in prompt
        assert "Fix only compilation" in prompt
        assert "Minimal changes are essential" in prompt
        assert "avoid replacing real implementations with stubs" in prompt
        assert "Do not edit" not in prompt
        assert "Only edit" not in prompt
        assert str(tmp_path / "analysis") in prompt
        assert str(tmp_path / "original-c.tar.gz") in prompt
        assert "initial compile error" in prompt
        assert kwargs["timeout"] == 900
        (rust / "lib.rs").write_text("changed signature, globals, and body\n")
        (rust / "Cargo.toml").write_text("changed Cargo configuration\n")
        (rust / "tests/example.rs").write_text("agent changed a test\n")
        (rust / "opencode.json").write_text("agent configuration\n")
        return CommandResult(7, "agent failed after making buildable edits")

    builds = []

    def build(path):
        builds.append(path)
        assert (path / "lib.rs").read_text() == "changed signature, globals, and body\n"
        return CommandResult(0)

    loop = repair(tmp_path, build, runner)
    assert loop.ensure(rust, CommandResult(101, "", "initial compile error"))
    assert builds == [rust]
    assert len(calls) == 1
    assert (rust / "Cargo.toml").read_text() == "changed Cargo configuration\n"
    assert (rust / "tests/example.rs").read_text() == "agent changed a test\n"
    assert (rust / "opencode.json").read_text() == "agent configuration\n"
    assert loop.report["attempts"][-1]["repair"]["returncode"] == 7


def test_failed_repairs_keep_progress_and_exhaustion_preserves_the_project(tmp_path):
    rust = project(tmp_path)
    (rust / "target").mkdir()
    (rust / "target/cache").write_text("build cache")
    calls = []
    builds = []

    def runner(command, **kwargs):
        number = len(calls) + 1
        assert (rust / "lib.rs").read_text() == (
            "uncompilable translation\n"
            if number == 1
            else f"partial repair {number - 1}\n"
        )
        (rust / "lib.rs").write_text(f"partial repair {number}\n")
        calls.append(command)
        return CommandResult(0)

    def build(path):
        builds.append((path / "lib.rs").read_text())
        return CommandResult(101, "", "remaining compiler error " + "x" * 20000)

    loop = repair(tmp_path, build, runner, max_validation_repairs=0)
    assert not loop.ensure(rust)
    assert len(calls) == 3
    assert len(builds) == 4
    assert loop.report["repair_limit"] == loop.report["repair_attempts"] == 3
    assert loop.report["reason"] == "repair_limit_reached"
    partial = tmp_path / "artifacts" / loop.report["partial_project"]
    assert (partial / "lib.rs").read_text() == "partial repair 3\n"
    assert (partial / "Cargo.toml").read_text() == (rust / "Cargo.toml").read_text()
    assert not (partial / "target").exists()
    assert len(loop.report["attempts"][-1]["diagnostics"]) > 20000
    assert (
        tmp_path / "artifacts" / loop.report["attempts"][-1]["stderr_log"]
    ).read_text() == "remaining compiler error " + "x" * 20000


def test_compilation_budget_is_shared_between_projects_and_separate_from_tests(
    tmp_path,
):
    before = project(tmp_path, "before-postprocess")
    after = project(tmp_path, "after-postprocess")
    last = project(tmp_path, "last")
    builds = iter(
        [CommandResult(101), CommandResult(0), CommandResult(101), CommandResult(0)]
    )
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return CommandResult(0)

    loop = repair(
        tmp_path,
        lambda path: next(builds),
        runner,
        max_compilation_repairs=2,
        max_validation_repairs=9,
    )
    assert loop.ensure(before)
    assert loop.ensure(after)
    assert not loop.ensure(last, CommandResult(101))
    assert len(calls) == 2
    assert [command[command.index("--dir") + 1] for command in calls] == [
        str(before),
        str(after),
    ]
    assert loop.report["repair_attempts"] == 2
    assert loop.report["compilation_runs"] == 5
    assert loop.report["reason"] == "repair_limit_reached"
    assert (tmp_path / "artifacts/compilation-repair-project/lib.rs").read_text() == (
        last / "lib.rs"
    ).read_text()


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"max_compilation_repairs": 0}, "repair_limit_reached"),
        ({"validation_repair_model": None}, "repair_model_not_configured"),
    ],
)
def test_unavailable_repair_retains_partial_project_and_report(
    tmp_path, settings, reason
):
    rust = project(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("no repair agent is available")

    loop = repair(tmp_path, unexpected, unexpected, **settings)
    assert not loop.ensure(rust, CommandResult(101, "", "compile failed"))
    assert loop.report["repair_attempts"] == 0
    assert loop.report["reason"] == reason
    assert (tmp_path / "artifacts/compilation-repair-project/lib.rs").exists()
    saved = json.loads((tmp_path / "artifacts/compilation-repair.json").read_text())
    assert saved == loop.report


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("opencode unavailable"),
        subprocess.TimeoutExpired("opencode", 900, output=b"partial agent output"),
    ],
)
def test_agent_invocation_errors_still_receive_an_independent_build(tmp_path, error):
    rust = project(tmp_path)
    builds = []

    def runner(*args, **kwargs):
        (rust / "lib.rs").write_text("buildable changes before agent error\n")
        raise error

    def build(path):
        builds.append(path)
        assert (path / "lib.rs").read_text() == "buildable changes before agent error\n"
        return CommandResult(0)

    loop = repair(tmp_path, build, runner)
    assert loop.ensure(rust, CommandResult(101))
    assert builds == [rust]
    execution = loop.report["attempts"][-1]["repair"]
    assert execution["error"] == (
        "timeout"
        if isinstance(error, subprocess.TimeoutExpired)
        else "invocation_failed"
    )
    assert execution["returncode"] in (124, 127)


def test_build_invocation_error_is_recorded_and_missing_source_is_reported(tmp_path):
    rust = tmp_path / "missing-project"

    def build(path):
        raise FileNotFoundError("Cargo could not start")

    loop = repair(tmp_path, build, None, max_compilation_repairs=0)
    assert not loop.ensure(rust)
    assert loop.report["attempts"][0]["error"] == "invocation_failed"
    assert "Cargo could not start" in loop.report["attempts"][0]["diagnostics"]
    assert "partial_project_error" in loop.report
    assert (tmp_path / "artifacts/compilation-repair.json").exists()
