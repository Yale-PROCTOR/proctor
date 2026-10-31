from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from final_validation import _subprocess_run, validate_project
from stage import _effective_config, run_stage
from tooling import CommandResult, StageFailure
from tests.test_local_transformation import FakeTools, STAGE_DIR, stage_input


MODEL = "openrouter/openai/gpt-6.1-sol"


def suite_result(*, successes=1, failures=0, errors=0, skipped=0, **extra):
    payload = {
        "passed": not failures and not errors,
        "tests_count": successes + failures + errors + skipped,
        "successes_count": successes,
        "failures_count": failures,
        "errors_count": errors,
        "skipped_count": skipped,
        **extra,
    }
    return CommandResult(int(bool(failures or errors)), json.dumps(payload))


def project(tmp_path, *, manifest=True):
    rust = tmp_path / "rust"
    rust.mkdir()
    (rust / "lib.rs").write_text("original buildable translation\n")
    (rust / "Cargo.toml").write_text('[lib]\npath = "lib.rs"\n')
    if manifest:
        (rust / "testinfra.toml").write_text('strategy = "cando2"\n')
    return rust


def validate(tmp_path, rust, runner, *, build=None, c_project=None, **settings):
    return validate_project(
        rust,
        workdir=tmp_path / "work",
        artifacts=tmp_path / "artifacts",
        config=_effective_config(settings, STAGE_DIR),
        build=build or (lambda path: CommandResult(0)),
        c_project=c_project,
        runner=runner,
    )


def test_missing_manifest_does_not_invoke_tools(tmp_path):
    rust = project(tmp_path, manifest=False)

    def unexpected(*args, **kwargs):
        pytest.fail("no tools should run without a testinfra manifest")

    report = validate(tmp_path, rust, unexpected, build=unexpected)
    assert report["status"] == "no_manifest"
    assert report["validation_runs"] == report["repair_attempts"] == 0
    assert report["tests_count"] is None
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize(
    ("result", "status", "reason", "executed"),
    [
        (suite_result(), "validation_succeeded", "all_tests_passed", 1),
        (
            suite_result(successes=1, skipped=2),
            "validation_succeeded",
            "all_tests_passed",
            1,
        ),
        (suite_result(successes=0), "no_executable_tests", "no_tests_executed", 0),
        (
            suite_result(successes=0, skipped=2),
            "no_executable_tests",
            "no_tests_executed",
            0,
        ),
        (suite_result(failures=1), "validation_failed", "repair_limit_reached", 2),
        (suite_result(errors=1), "validation_failed", "repair_limit_reached", 2),
        (
            suite_result(
                successes=0, compiler_errors=["harness failed"], tests_count=3
            ),
            "no_executable_tests",
            "test_build_failed",
            0,
        ),
        (
            suite_result(successes=0, execution_error="missing runner"),
            "no_executable_tests",
            "test_execution_failed",
            0,
        ),
        (
            CommandResult(1, suite_result().stdout),
            "no_executable_tests",
            "runner_failed",
            1,
        ),
        (
            CommandResult(0, "invalid JSON"),
            "no_executable_tests",
            "invalid_runner_output",
            None,
        ),
        (CommandResult(0, "[]"), "no_executable_tests", "invalid_runner_output", None),
        (
            suite_result(tests_count=True),
            "no_executable_tests",
            "invalid_runner_output",
            None,
        ),
        (
            suite_result(tests_count=0),
            "no_executable_tests",
            "invalid_runner_output",
            None,
        ),
        (
            suite_result(skipped_count=-1),
            "no_executable_tests",
            "invalid_runner_output",
            None,
        ),
    ],
)
def test_result_classification_and_exact_test_command(
    tmp_path, result, status, reason, executed
):
    rust = project(tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return result

    report = validate(tmp_path, rust, runner, max_validation_repairs=0)
    assert report["status"] == status
    assert report["reason"] == reason
    assert report["executed_count"] == executed
    assert report["validation_runs"] == 1
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "uv",
        "run",
        "--directory",
        str((STAGE_DIR / "../test-infrastructure").resolve()),
        "testinfra",
        "run",
        "--project-dir",
        str(rust),
        "--json",
    ]
    assert kwargs["timeout"] == 600
    assert kwargs["cwd"] == rust
    assert (
        tmp_path / "artifacts" / report["attempts"][0]["stdout_log"]
    ).read_text() == result.stdout


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("uv unavailable"),
        subprocess.TimeoutExpired("uv", 5, output=b"partial"),
    ],
)
def test_runner_failure_skips_repairs_and_keeps_unknown_counts(tmp_path, error):
    rust = project(tmp_path)

    def runner(*args, **kwargs):
        raise error

    report = validate(tmp_path, rust, runner, validation_repair_model=MODEL)
    assert report["status"] == "no_executable_tests"
    assert report["reason"] == (
        "timeout"
        if isinstance(error, subprocess.TimeoutExpired)
        else "invocation_failed"
    )
    assert report["tests_count"] is None
    assert report["repair_attempts"] == 0
    assert (rust / "lib.rs").read_text() == "original buildable translation\n"


def test_missing_repair_model_is_nonfatal_and_does_not_inherit_a_model(tmp_path):
    rust = project(tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return suite_result(failures=1)

    report = validate(tmp_path, rust, runner)
    assert report["reason"] == "repair_model_not_configured"
    assert report["status"] == "validation_failed"
    assert report["repair_model"] is None
    assert report["repair_attempts"] == 0
    assert len(calls) == 1


def test_default_budget_runs_three_agents_and_four_independent_validations(tmp_path):
    rust = project(tmp_path)
    calls = []
    builds = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[0] == "opencode":
            number = sum(call[0] == "opencode" for call in calls)
            (rust / "lib.rs").write_text(f"buildable repair {number}\n")
            assert command[command.index("--model") + 1] == MODEL
            return CommandResult(0, "agent claims success without running tests")
        return suite_result(
            failures=1,
            failing_testcases=[{"name": "wrong value", "message": "expected 3"}],
        )

    def build(path):
        builds.append(path.joinpath("lib.rs").read_text())
        return CommandResult(0)

    report = validate(
        tmp_path, rust, runner, build=build, validation_repair_model=MODEL
    )
    assert report["status"] == "validation_failed"
    assert report["reason"] == "repair_limit_reached"
    assert report["repair_attempts"] == 3
    assert report["validation_runs"] == 4
    assert len(builds) == 3
    assert (rust / "lib.rs").read_text() == "buildable repair 3\n"
    assert [command[0] for command in calls] == [
        "uv",
        "opencode",
        "uv",
        "opencode",
        "uv",
        "opencode",
        "uv",
    ]


@pytest.mark.parametrize("agent_exit", [0, 7, "timeout", "missing"])
def test_stage_owned_validation_accepts_success_even_after_agent_failure(
    tmp_path, agent_exit
):
    rust = project(tmp_path)
    tests = []

    def runner(command, **kwargs):
        if command[0] == "opencode":
            (rust / "lib.rs").write_text("fixed translation\n")
            if agent_exit == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            if agent_exit == "missing":
                raise FileNotFoundError("opencode unavailable")
            return CommandResult(agent_exit)
        tests.append(command)
        return suite_result(failures=1) if len(tests) == 1 else suite_result()

    report = validate(tmp_path, rust, runner, validation_repair_model=MODEL)
    assert report["status"] == "validation_succeeded"
    assert report["repair_attempts"] == 1
    assert report["validation_runs"] == 2
    assert (rust / "lib.rs").read_text() == "fixed translation\n"
    assert not (rust / ".validation-repair-prompt.md").exists()
    assert not (rust / "opencode.json").exists()


def test_uncompilable_split_edits_are_rolled_back_and_feedback_includes_build_errors(
    tmp_path,
):
    rust = project(tmp_path)
    (rust / "keep.rs").write_text("keep module\n")
    count = 0

    def runner(command, **kwargs):
        nonlocal count
        if command[0] == "opencode":
            count += 1
            if count == 1:
                (rust / "keep.rs").unlink()
                (rust / "new.rs").write_text("uncompilable module\n")
            else:
                assert (rust / "keep.rs").read_text() == "keep module\n"
                assert not (rust / "new.rs").exists()
                prompt = Path(command[command.index("-f") + 1]).read_text()
                assert "cannot resolve helper" in prompt
                (rust / "lib.rs").write_text("fixed\n")
            return CommandResult(0)
        return suite_result() if count == 2 else suite_result(failures=1)

    def build(path):
        return (
            CommandResult(1, "", "cannot resolve helper")
            if count == 1
            else CommandResult(0)
        )

    report = validate(
        tmp_path, rust, runner, build=build, validation_repair_model=MODEL
    )
    assert report["status"] == "validation_succeeded"
    assert report["attempts"][1]["repair"]["rolled_back"] is True
    assert report["repair_attempts"] == 2


@pytest.mark.parametrize("archive", [False, True])
def test_c_reference_prompt_permissions_and_existing_config_are_preserved(
    tmp_path, archive
):
    rust = project(tmp_path)
    c_project = tmp_path / "original-c"
    c_project.mkdir()
    (c_project / "main.c").write_text("int value(void) { return 3; }\n")
    if archive:
        c_project = tmp_path / "original-c.tar"
        with tarfile.open(c_project, "w") as bundle:
            data = b"int value(void) { return 3; }\n"
            info = tarfile.TarInfo("main.c")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    original = b'{"permission":{"external_directory":{"*":"deny"}},"keep":true}\n'
    (rust / "opencode.json").write_bytes(original)
    repaired = False

    def runner(command, **kwargs):
        nonlocal repaired
        if command[0] == "opencode":
            repaired = True
            reference = tmp_path / "work/test-validation/c-reference"
            assert (reference / "main.c").read_text().startswith("int value")
            prompt = Path(command[command.index("-f") + 1]).read_text()
            assert str(reference) in prompt
            assert "Only edit Rust implementation files" in prompt
            assert "Do not edit tests" in prompt
            assert "--json" in prompt and "testinfra" in prompt
            assert "expected 3" in prompt
            permission = json.loads((rust / "opencode.json").read_text())["permission"][
                "external_directory"
            ]
            assert permission[str(reference)] == "allow"
            assert (
                permission[str((STAGE_DIR / "../test-infrastructure").resolve())]
                == "allow"
            )
            return CommandResult(0)
        return (
            suite_result()
            if repaired
            else suite_result(failures=1, summary="expected 3")
        )

    report = validate(
        tmp_path, rust, runner, c_project=c_project, validation_repair_model=MODEL
    )
    assert report["status"] == "validation_succeeded"
    assert (rust / "opencode.json").read_bytes() == original
    assert not (rust / "c-reference").exists()
    assert (tmp_path / "original-c/main.c").read_text().startswith("int value")


def test_real_subprocess_timeout_stops_descendants(tmp_path):
    script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "print(child.pid, flush=True); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        _subprocess_run([sys.executable, "-c", script], cwd=tmp_path, timeout=0.3)
    child = int(caught.value.stdout)
    state = Path(f"/proc/{child}/stat")
    if state.exists():
        assert state.read_text().split()[2] == "Z"


@pytest.mark.parametrize("repair", [False, True])
def test_stage_preserves_generated_harnesses_and_publishes_validation_with_self_alias(
    tmp_path, repair
):
    value = stage_input(tmp_path, config={"validation_repair_model": MODEL})
    source = value.inputs.rust_project
    (source / "testinfra.toml").write_text('strategy = "cando2"\n')
    (source / "translated_rust").symlink_to(".", target_is_directory=True)
    harnesses = ("tests/int_tests.rs", "tests/runner/src/main.rs", "runner/src/main.rs")
    for name in harnesses:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixed oracle harness\n")

    class HarnessDeletingTools(FakeTools):
        def prepare(self, current, passes, use_print):
            super().prepare(current, passes, use_print)
            for name in harnesses:
                (current / name).unlink()

    tools = HarnessDeletingTools()
    called = []

    def runner(command, **kwargs):
        called.append(command[0])
        current = kwargs["cwd"]
        if command[0] == "opencode":
            (current / "lib.rs").write_text("repaired translation\n")
            return CommandResult(0)
        for name in harnesses:
            assert (current / name).read_text() == "fixed oracle harness\n"
        alias = current / "translated_rust"
        if not alias.exists():
            alias.symlink_to(".", target_is_directory=True)
        return (
            suite_result(failures=1)
            if repair and "opencode" not in called
            else suite_result()
        )

    output = run_stage(
        value, stage_dir=STAGE_DIR, tools=tools, validation_runner=runner
    )
    assert output.status == "success", output.error
    report = json.loads(
        (value.outputs.artifacts_dir / "test-validation.json").read_text()
    )
    assert report["status"] == "validation_succeeded"
    assert report["repair_attempts"] == int(repair)
    assert (value.outputs.rust_project / "translated_rust").is_symlink()
    assert os.readlink(value.outputs.rust_project / "translated_rust") == "."
    for name in harnesses:
        assert (
            value.outputs.rust_project / name
        ).read_text() == "fixed oracle harness\n"
    assert not (value.outputs.rust_project / "test-validation").exists()
    assert (source / "lib.rs").read_text() == "old\n"
    assert (source / "translated_rust").is_symlink()


@pytest.mark.parametrize(
    "outcome",
    ["no_manifest", "no_executable_tests", "validation_failed", "validation_succeeded"],
)
@pytest.mark.parametrize("artifacts", [False, True])
def test_stage_reports_every_validation_outcome_without_failing_translation(
    tmp_path, outcome, artifacts
):
    value = stage_input(
        tmp_path,
        artifacts=artifacts,
        config={"validation_repair_model": MODEL, "max_validation_repairs": 2},
    )
    if outcome != "no_manifest":
        (value.inputs.rust_project / "testinfra.toml").write_text('strategy="cando2"\n')

    def runner(command, **kwargs):
        if command[0] == "opencode":
            return CommandResult(7, "agent could not fix the bug")
        return suite_result(
            successes=0 if outcome == "no_executable_tests" else 1,
            failures=int(outcome == "validation_failed"),
        )

    output = run_stage(
        value, stage_dir=STAGE_DIR, tools=FakeTools(), validation_runner=runner
    )
    assert output.status == "success", output.error
    report_path = (
        value.outputs.artifacts_dir or value.framework.workdir
    ) / "test-validation.json"
    report = json.loads(report_path.read_text())
    assert report["status"] == outcome
    assert "schema_version" not in report
    assert (value.outputs.rust_project / "lib.rs").read_text() == "normalized\n"
    assert report["repair_attempts"] == (2 if outcome == "validation_failed" else 0)
    if outcome == "validation_failed":
        assert [(model.provider, model.model) for model in output.models] == [
            ("opencode", MODEL)
        ]


@pytest.mark.parametrize(
    "relative",
    [
        "work/test-validation/last-buildable/lib.rs",
        "work/test-validation/c-reference/main.c",
        "artifacts/test-validation.json",
        "artifacts/test-validation-logs/validation-000.stdout.log",
    ],
)
def test_validation_paths_cannot_overlap_usage_log(tmp_path, relative):
    value = stage_input(tmp_path)
    usage = tmp_path / relative
    usage.parent.mkdir(parents=True, exist_ok=True)
    usage.write_text("preserve usage\n")
    value = replace(value, framework=replace(value.framework, usage_log=usage))
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "usage log overlaps" in output.error
    assert usage.read_text() == "preserve usage\n"
    assert not tools.events


@pytest.mark.parametrize(
    "relative",
    [
        "work/test-validation",
        "work/test-validation/last-buildable",
        "work/test-validation/c-reference",
        "artifacts/test-validation-logs",
    ],
)
def test_validation_scratch_symlinks_are_rejected_before_mutation(tmp_path, relative):
    value = stage_input(tmp_path)
    target = tmp_path / "external"
    target.mkdir()
    (target / "sentinel").write_text("preserve\n")
    link = tmp_path / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "symlink" in output.error
    assert not tools.events
    assert (target / "sentinel").read_text() == "preserve\n"
    assert link.is_symlink()


@pytest.mark.parametrize("artifacts", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_validation_logs_cannot_overlap_project_destination(
    tmp_path, artifacts, nested
):
    value = stage_input(tmp_path, artifacts=artifacts)
    root = (
        value.outputs.artifacts_dir or value.framework.workdir
    ) / "test-validation-logs"
    destination = root / "rust" if nested else root
    value = replace(value, outputs=replace(value.outputs, rust_project=destination))
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "output Rust project overlaps test validation logs" in output.error
    assert not tools.events
    assert not root.exists()


@pytest.mark.parametrize("destination", ["work", "output", "artifacts"])
def test_c_source_input_is_protected_from_output_and_scratch_overlap(
    tmp_path, destination
):
    value = stage_input(tmp_path)
    c_project = tmp_path / "original-c"
    c_project.mkdir()
    (c_project / "main.c").write_text("int main(void) { return 0; }\n")
    value = replace(value, inputs=replace(value.inputs, c_project=c_project))
    if destination == "work":
        value = replace(
            value, framework=replace(value.framework, workdir=c_project / "scratch")
        )
    elif destination == "output":
        value = replace(
            value, outputs=replace(value.outputs, rust_project=c_project / "rust")
        )
    else:
        value = replace(
            value, outputs=replace(value.outputs, artifacts_dir=c_project / "artifacts")
        )
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "overlaps input C project" in output.error
    assert not tools.events
    assert (c_project / "main.c").read_text().startswith("int main")


def test_validation_report_publication_failure_cleans_all_success_products(
    tmp_path, monkeypatch
):
    import stage as stage_module

    value = stage_input(tmp_path)
    replace_file = stage_module.os.replace

    def fail_report(source, destination):
        if Path(destination).name == "test-validation.json":
            raise OSError("validation report publication failed")
        return replace_file(source, destination)

    monkeypatch.setattr(stage_module.os, "replace", fail_report)
    output = run_stage(value, stage_dir=STAGE_DIR, tools=FakeTools())
    assert output.status == "failure"
    assert "validation report publication failed" in output.error
    assert not value.outputs.rust_project.exists()
    for name in (
        "statement-pairs.md",
        "observations.json",
        "statistics.json",
        "test-validation.json",
    ):
        assert not (value.outputs.artifacts_dir / name).exists()
    assert not list(value.outputs.artifacts_dir.glob(".*.tmp"))


@pytest.mark.parametrize(
    "settings",
    [
        {"max_validation_repairs": -1},
        {"max_validation_repairs": True},
        {"validation_repair_model": ""},
        {"validation_repair_model": 1},
        {"validation_repair_agent": "unknown"},
        {"testinfra_dir": ""},
        {"validation_timeout_s": 0},
        {"validation_repair_timeout_s": True},
    ],
)
def test_invalid_validation_config_is_rejected(settings):
    with pytest.raises(StageFailure):
        _effective_config(settings, STAGE_DIR)


def test_pipeline_config_pins_openrouter_repair_model_separately():
    config = tomllib.loads(
        (
            STAGE_DIR.parents[1]
            / "configs/c2rust_crat_testgen_absrec_disrep_local.toml"
        ).read_text()
    )
    stage = config["stages"]["local_transformation"]
    assert stage["config"]["validation_repair_model"] == MODEL
    assert config["llm"]["model"] != MODEL
    assert stage["config"]["max_validation_repairs"] == 3
    assert stage["timeout_s"] >= 3600 + 3 * 900 + 4 * 600
