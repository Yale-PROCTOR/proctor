"""Stage subprocess invocation.

Python stages (a ``pyproject.toml`` at the stage root) run through
``uv run --project <stage-dir>`` so their own pinned dependencies are
available; anything else runs its ``exec`` directly. The working
directory is the stage root (contract §Invocation); all data paths
flow through the envelope.

Timeouts kill the whole process group — stages spawn cargo and rustc
children that must not outlive them.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InvocationResult:
    returncode: int
    duration_s: float
    timed_out: bool


def build_command(
    stage_dir: Path, exec_argv: tuple[str, ...], input_file: Path, output_file: Path
) -> list[str]:
    command = list(exec_argv)
    if (stage_dir / "pyproject.toml").is_file() and shutil.which("uv"):
        command = ["uv", "run", "--project", str(stage_dir), *command]
    command.extend(["--input", str(input_file), "--output", str(output_file)])
    return command


def invoke_stage(
    stage_dir: Path,
    exec_argv: tuple[str, ...],
    input_file: Path,
    output_file: Path,
    *,
    stdout_log: Path,
    stderr_log: Path,
    timeout_s: int | None = None,
) -> InvocationResult:
    command = build_command(stage_dir, exec_argv, input_file, output_file)
    started = time.monotonic()
    timed_out = False
    env = os.environ.copy()
    if timeout_s is not None:
        # The child can budget its work against the same clock, including launcher time.
        env["PROCTOR_STAGE_DEADLINE_MONOTONIC"] = str(started + timeout_s)

    with stdout_log.open("ab") as out, stderr_log.open("ab") as err:
        process = subprocess.Popen(
            command,
            cwd=stage_dir,
            stdout=out,
            stderr=err,
            env=env,
            start_new_session=True,  # own process group, so timeout kills children
        )
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait()

    returncode = process.returncode
    return InvocationResult(
        returncode=returncode if returncode is not None else -1,
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
    )
