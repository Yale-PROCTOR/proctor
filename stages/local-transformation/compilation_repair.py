from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from final_validation import _copy_project, _run, _subprocess_run
from tooling import CommandResult


class CompilationRepair:
    def __init__(
        self,
        *,
        workdir: Path,
        artifacts: Path,
        config: dict[str, Any],
        build: Callable[[Path], CommandResult],
        analysis_project: Path,
        c_project: Path | None = None,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.workdir = workdir
        self.artifacts = artifacts
        self.config = config
        self.build = build
        self.analysis_project = analysis_project
        self.c_project = c_project
        self.runner = runner or _subprocess_run
        self.logs = artifacts / "compilation-repair-logs"
        self.report: dict[str, Any] = {
            "status": "not_checked",
            "reason": None,
            "compilation_runs": 0,
            "repair_attempts": 0,
            "repair_limit": config.get("max_compilation_repairs", 3),
            "repair_agent": config["validation_repair_agent"],
            "repair_model": config["validation_repair_model"],
            "attempts": [],
        }

    def _write_report(self) -> None:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        (self.artifacts / "compilation-repair.json").write_text(
            json.dumps(self.report, indent=2) + "\n", encoding="utf-8"
        )

    def _compile(
        self,
        project: Path,
        initial_result: CommandResult | None = None,
        repair: dict[str, Any] | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        error = None
        try:
            result = (
                initial_result if initial_result is not None else self.build(project)
            )
        except OSError as exc:
            result = CommandResult(127, "", str(exc))
            error = "invocation_failed"
        index = self.report["compilation_runs"]
        self.logs.mkdir(parents=True, exist_ok=True)
        stdout = self.logs / f"build-{index:03d}.stdout.log"
        stderr = self.logs / f"build-{index:03d}.stderr.log"
        stdout.write_text(result.stdout, encoding="utf-8")
        stderr.write_text(result.stderr, encoding="utf-8")
        self.report["compilation_runs"] += 1
        self.report["attempts"].append(
            {
                "project": str(project.resolve()),
                "returncode": result.returncode,
                "error": error,
                "duration_s": time.monotonic() - started,
                "stdout_log": str(stdout.relative_to(self.artifacts)),
                "stderr_log": str(stderr.relative_to(self.artifacts)),
                "diagnostics": result.stdout + result.stderr,
                "repair": repair,
            }
        )
        self.report.update(
            status="compilation_succeeded"
            if result.returncode == 0
            else "compilation_failed",
            reason="build_passed" if result.returncode == 0 else "build_failed",
        )
        self._write_report()
        return result

    def retain_failed_project(self, project: Path) -> Path | None:
        destination = self.artifacts / "compilation-repair-project"
        try:
            _copy_project(project, destination)
        except OSError as exc:
            self.report["partial_project_error"] = str(exc)
            self._write_report()
            return None
        self.report["partial_project"] = str(destination.relative_to(self.artifacts))
        self._write_report()
        return destination

    def ensure(
        self, project: Path, initial_result: CommandResult | None = None
    ) -> bool:
        result = self._compile(project, initial_result)
        while result.returncode != 0:
            if self.report["repair_attempts"] >= self.report["repair_limit"]:
                self.report["reason"] = "repair_limit_reached"
                self.retain_failed_project(project)
                return False
            if not self.config["validation_repair_model"]:
                self.report["reason"] = "repair_model_not_configured"
                self.retain_failed_project(project)
                return False
            index = self.report["repair_attempts"] + 1
            prompt = f"""Fix the compilation errors in the translated Rust project at {project.resolve()}.
Make the smallest changes necessary to make this project compile. Fix only compilation
errors. Preserve the translated behavior and retain existing implementations wherever
possible. Minimal changes are essential: avoid unrelated restructuring or cleanup,
and avoid replacing real implementations with stubs merely to obtain a successful build.

Use the original Rust analysis project as a behavioral reference: {self.analysis_project.resolve()}.
Original C source reference: {self.c_project.resolve() if self.c_project else "unavailable"}.
Build with `cargo build` from {project.resolve()}. Inspect the compiler diagnostics,
make minimal fixes, and rerun the build yourself. This task is compilation repair;
the separate test-based repair runs afterward. The stage will independently build
the resulting project to determine whether compilation repair succeeded.
This is compilation repair invocation {index} of {self.report["repair_limit"]}.

Latest independent compilation result:
{json.dumps(self.report["attempts"][-1], indent=2)}
"""
            prompt_path = self.logs / f"repair-{index:03d}.prompt.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            self.report["repair_attempts"] = index
            _, execution = _run(
                [
                    "opencode",
                    "run",
                    "Follow the attached compilation repair instructions.",
                    "--auto",
                    "-f",
                    str(prompt_path.resolve()),
                    "--format",
                    "json",
                    "--dir",
                    str(project.resolve()),
                    "--model",
                    self.config["validation_repair_model"],
                ],
                project,
                self.logs,
                f"repair-{index:03d}",
                self.config["validation_repair_timeout_s"],
                self.runner,
            )
            execution["prompt"] = str(prompt_path.relative_to(self.artifacts))
            result = self._compile(project, repair=execution)
        return True
