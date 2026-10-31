from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tooling import CommandResult


def _subprocess_run(
    command: list[str], *, cwd: Path, timeout: float, **kwargs: Any
) -> CommandResult:
    with subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return CommandResult(process.returncode, stdout, stderr)


def _copy_project(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=lambda directory, names: (
            {"target"} if Path(directory) == source else set()
        ),
    )


def _run(
    command: list[str],
    project: Path,
    logs: Path,
    name: str,
    timeout_s: float,
    runner: Callable[..., Any],
) -> tuple[CommandResult, dict[str, Any]]:
    started = time.monotonic()
    error = None
    try:
        process = runner(
            command,
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        result = CommandResult(process.returncode, process.stdout, process.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        result = CommandResult(
            124,
            stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
            stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr,
        )
        error = "timeout"
    except OSError as exc:
        result = CommandResult(127, "", str(exc))
        error = "invocation_failed"
    logs.mkdir(parents=True, exist_ok=True)
    stdout_path = logs / f"{name}.stdout.log"
    stderr_path = logs / f"{name}.stderr.log"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    return result, {
        "command": command,
        "returncode": result.returncode,
        "error": error,
        "duration_s": time.monotonic() - started,
        "stdout_log": str(stdout_path.relative_to(logs.parent)),
        "stderr_log": str(stderr_path.relative_to(logs.parent)),
    }


def _evaluate(result: CommandResult, execution: dict[str, Any]) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "status": "no_executable_tests",
        "reason": execution["error"],
        **dict.fromkeys(
            (
                "tests_count",
                "successes_count",
                "failures_count",
                "errors_count",
                "skipped_count",
                "executed_count",
            )
        ),
    }
    if execution["error"]:
        return outcome
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        outcome["reason"] = "invalid_runner_output"
        return outcome
    fields = ("tests_count", "successes_count", "failures_count", "errors_count")
    if not isinstance(payload, dict) or any(
        type(payload.get(name)) is not int or payload[name] < 0 for name in fields
    ):
        outcome["reason"] = "invalid_runner_output"
        return outcome
    skipped = payload.get("skipped_count", 0)
    executed = sum(payload[name] for name in fields[1:])
    if (
        type(skipped) is not int
        or skipped < 0
        or executed + skipped > payload["tests_count"]
    ):
        outcome["reason"] = "invalid_runner_output"
        return outcome
    outcome.update({name: payload[name] for name in fields})
    outcome.update(skipped_count=skipped, executed_count=executed, results=payload)
    if not executed:
        outcome["reason"] = (
            "test_build_failed"
            if payload.get("compiler_errors")
            else "test_execution_failed"
            if payload.get("execution_error")
            else "no_tests_executed"
        )
    elif payload["failures_count"] or payload["errors_count"]:
        outcome.update(status="validation_failed", reason="tests_failed")
    elif (
        payload.get("passed") is True
        and result.returncode == 0
        and not payload.get("compiler_errors")
        and not payload.get("execution_error")
    ):
        outcome.update(status="validation_succeeded", reason="all_tests_passed")
    else:
        outcome["reason"] = "runner_failed"
    return outcome


@contextmanager
def _agent_files(
    project: Path, prompt: str, testinfra: Path, reference: Path | None
) -> Iterator[Path]:
    config_path = project / "opencode.json"
    if not config_path.exists() and (project / ".opencode.json").exists():
        config_path = project / ".opencode.json"
    prompt_path = project / ".validation-repair-prompt.md"
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (config_path, prompt_path)
    }
    try:
        configuration = json.loads(originals[config_path] or b"{}")
        permissions = configuration.setdefault("permission", {"*": "allow"})
        if isinstance(permissions, str):
            permissions = configuration["permission"] = {"*": permissions}
        external = permissions.setdefault(
            "external_directory", {"*": "deny", "/tmp/**": "allow"}
        )
        if isinstance(external, str):
            external = permissions["external_directory"] = {"*": external}
        for path in (testinfra, reference):
            if path is not None:
                external[str(path.resolve())] = "allow"
                external[str(path.resolve() / "**")] = "allow"
        config_path.write_text(json.dumps(configuration), encoding="utf-8")
        prompt_path.write_text(prompt, encoding="utf-8")
        yield prompt_path
    finally:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)


def validate_project(
    project: Path,
    *,
    workdir: Path,
    artifacts: Path,
    config: dict[str, Any],
    build: Callable[[Path], CommandResult],
    c_project: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    runner = runner or _subprocess_run
    report: dict[str, Any] = {
        "status": "no_manifest",
        "reason": "testinfra_manifest_missing",
        "validation_runs": 0,
        "repair_attempts": 0,
        "repair_limit": config["max_validation_repairs"],
        "repair_agent": config["validation_repair_agent"],
        "repair_model": config["validation_repair_model"],
        "tests_count": None,
        "successes_count": None,
        "failures_count": None,
        "errors_count": None,
        "skipped_count": None,
        "executed_count": None,
        "attempts": [],
    }
    if not (project / "testinfra.toml").exists():
        return report
    scratch = workdir / "test-validation"
    scratch.mkdir(parents=True, exist_ok=True)
    logs = artifacts / "test-validation-logs"
    testinfra = Path(config["testinfra_dir"])
    command = [
        "uv",
        "run",
        "--directory",
        str(testinfra),
        "testinfra",
        "run",
        "--project-dir",
        str(project.resolve()),
        "--json",
    ]
    backup = scratch / "last-buildable"
    _copy_project(project, backup)

    def test(repair: dict[str, Any] | None = None) -> None:
        index = report["validation_runs"]
        result, execution = _run(
            command,
            project,
            logs,
            f"validation-{index:03d}",
            config["validation_timeout_s"],
            runner,
        )
        outcome = _evaluate(result, execution)
        report.update(
            {key: value for key, value in outcome.items() if key != "results"}
        )
        report["validation_runs"] += 1
        report["attempts"].append({**execution, **outcome, "repair": repair})
        if outcome["status"] == "no_executable_tests":
            _copy_project(backup, project)

    test()
    if report["status"] != "validation_failed":
        return report
    if not config["validation_repair_model"] and config["max_validation_repairs"]:
        report["reason"] = "repair_model_not_configured"
        return report
    reference = None
    if c_project is not None and config["max_validation_repairs"]:
        reference = scratch / "c-reference"
        try:
            if reference.exists():
                shutil.rmtree(reference)
            if c_project.is_dir():
                shutil.copytree(c_project, reference)
            else:
                reference.mkdir()
                with tarfile.open(c_project, "r:*") as archive:
                    archive.extractall(reference, filter="data")
        except (OSError, tarfile.TarError) as exc:
            report["c_reference_error"] = str(exc)
            reference = None
    for index in range(1, config["max_validation_repairs"] + 1):
        prompt = f"""Repair the translated Rust project at {project.resolve()}.
Only edit Rust implementation files and necessary Cargo build configuration in that project.
Do not edit tests, test vectors, expected outputs, runners, testinfra.toml, proctor.toml,
Crat, pipeline tools, or any input/reference project. Do not weaken or bypass tests.
Original C source reference (read only): {reference.resolve() if reference else "unavailable"}.
Compare it with the Rust implementation when useful, but make every fix in the Rust project.

Build with `cargo build` from {project.resolve()}.
Run the existing test suite with this exact command (do not enable auto-completion):
{shlex.join(command)}
The test runner uses its own uv environment; preserve the existing oracle and harness.
Read the JSON failing_testcases, compiler_errors, execution_error and summary diagnostics.
Investigate, edit, and rerun the build and tests yourself. Finish only when all executed
tests pass if possible; the stage will independently build and retest your changes.
This is repair invocation {index} of {config["max_validation_repairs"]}.

Latest independent validation result:
{json.dumps(report["attempts"][-1], indent=2)}
"""
        logs.mkdir(parents=True, exist_ok=True)
        prompt_artifact = logs / f"repair-{index:03d}.prompt.md"
        prompt_artifact.write_text(prompt, encoding="utf-8")
        report["repair_attempts"] = index
        repair: dict[str, Any] = {
            "prompt": str(prompt_artifact.relative_to(artifacts)),
        }
        try:
            with _agent_files(project, prompt, testinfra, reference) as prompt_path:
                _, execution = _run(
                    [
                        "opencode",
                        "run",
                        "Follow the attached repair instructions.",
                        "-f",
                        str(prompt_path.resolve()),
                        "--format",
                        "json",
                        "--dir",
                        str(project.resolve()),
                        "--model",
                        config["validation_repair_model"],
                    ],
                    project,
                    logs,
                    f"repair-{index:03d}",
                    config["validation_repair_timeout_s"],
                    runner,
                )
                repair.update(execution)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            repair["error"] = str(exc)
        try:
            result = build(project)
            repair["build_returncode"] = result.returncode
            repair["build_diagnostics"] = (result.stdout + result.stderr)[-12000:]
            (logs / f"repair-{index:03d}.build.log").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            repair["build_log"] = str(
                (logs / f"repair-{index:03d}.build.log").relative_to(artifacts)
            )
            build_ok = result.returncode == 0
        except OSError as exc:
            repair["build_error"] = str(exc)
            build_ok = False
        if build_ok:
            _copy_project(project, backup)
        else:
            _copy_project(backup, project)
        repair["rolled_back"] = not build_ok
        test(repair)
        if report["status"] != "validation_failed":
            return report
    report["reason"] = "repair_limit_reached"
    return report
