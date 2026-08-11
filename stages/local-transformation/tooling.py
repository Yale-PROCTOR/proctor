from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protocol import (
    extract_observations_command,
    make_skeleton_command,
    merge_observations_command,
    normalize_safety_command,
    replace_command,
    validate_command,
)


class StageFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


RunCommand = Callable[..., CommandResult]


def subprocess_run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


def crat_env(crat_dir: Path) -> dict[str, str]:
    result = subprocess.run(
        ["rustc", "--print", "sysroot"],
        cwd=crat_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise StageFailure(
            "rustc sysroot discovery failed "
            f"({result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    sysroot = result.stdout.strip()
    env = os.environ.copy()
    env["DIR"] = str(crat_dir)
    env["SYSROOT"] = sysroot
    paths = [str(Path(sysroot) / "lib")]
    cache = Path(
        os.environ.get("PROCTOR_CACHE_DIR", Path.home() / ".cache" / "proctor")
    )
    z3 = cache / "z3" / "bin"
    if z3.is_dir():
        paths.append(str(z3))
    if env.get("LD_LIBRARY_PATH"):
        paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(paths)
    return env


class CratTools:
    def __init__(
        self,
        log_path: Path,
        *,
        run_command: RunCommand = subprocess_run,
        environment_factory: Callable[[Path], dict[str, str]] = crat_env,
    ) -> None:
        self.log_path = log_path
        self.run_command = run_command
        self.environment_factory = environment_factory
        self.crat: Path | None = None
        self.crat_tool: Path | None = None
        self.environment: dict[str, str] | None = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _run(
        self,
        operation: str,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        redactions: tuple[tuple[str, str], ...] = (),
    ) -> CommandResult:
        rendered_command = " ".join(command)
        for sensitive, replacement in redactions:
            rendered_command = rendered_command.replace(sensitive, replacement)
        try:
            result = self.run_command(command, cwd=cwd, env=env)
        except Exception as exc:
            safe_error = str(exc)
            for sensitive, replacement in redactions:
                safe_error = safe_error.replace(sensitive, replacement)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(f"$ {rendered_command}\n")
                log.write(f"{type(exc).__name__}: {safe_error}\n")
            raise StageFailure(
                f"{operation} failed while invoking command: "
                f"{type(exc).__name__}: {safe_error}"
            ) from exc
        safe_stdout = result.stdout
        safe_stderr = result.stderr
        for sensitive, replacement in redactions:
            safe_stdout = safe_stdout.replace(sensitive, replacement)
            safe_stderr = safe_stderr.replace(sensitive, replacement)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {rendered_command}\n")
            log.write(safe_stdout)
            log.write(safe_stderr)
        if result.returncode != 0:
            raise StageFailure(
                f"{operation} failed with exit code {result.returncode}\n"
                f"stdout:\n{safe_stdout}\nstderr:\n{safe_stderr}"
            )
        return result

    def build_tools(self, crat_dir: Path) -> tuple[Path, Path]:
        crat = crat_dir / "target" / "release" / "crat"
        crat_tool = crat_dir / "target" / "release" / "crat-tool"
        marker = crat_dir / "target" / ".proctor-local-transformation-build-sha"
        head_result = self.run_command(
            ["git", "rev-parse", "HEAD"], cwd=crat_dir, env=None
        )
        head = head_result.stdout.strip() if head_result.returncode == 0 else "unknown"
        if not (
            crat.is_file()
            and crat_tool.is_file()
            and marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == head
        ):
            self._run(
                "deps_crate cargo build",
                ["cargo", "build"],
                cwd=crat_dir / "deps_crate",
            )
            self._run(
                "release cargo build --bin crat",
                ["cargo", "build", "--release", "--bin", "crat"],
                cwd=crat_dir,
            )
            self._run(
                "release cargo build --bin crat-tool",
                ["cargo", "build", "--release", "--bin", "crat-tool"],
                cwd=crat_dir,
            )
            if not crat.is_file() or not crat_tool.is_file():
                raise StageFailure("Crat build did not produce both release binaries")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(head + "\n", encoding="utf-8")
        self.crat = crat
        self.crat_tool = crat_tool
        self.environment = self.environment_factory(crat_dir)
        return crat, crat_tool

    def prepare(
        self,
        current_project: Path,
        passes: tuple[str, ...],
        use_print: bool,
    ) -> None:
        assert self.crat is not None
        command = [str(self.crat), "--inplace"]
        config = current_project / "config.toml"
        if config.is_file():
            command.extend(["--config", str(config)])
        command.extend(["--pass", ",".join(passes)])
        if use_print:
            command.append("--unexpand-use-print")
        command.append(str(current_project))
        self._run("ordinary Crat prepare", command, env=self.environment)

    @staticmethod
    def _clear_output(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _require_regular_output(operation: str, path: Path) -> None:
        if path.is_symlink() or not path.exists():
            raise StageFailure(f"{operation} did not create a regular output at {path}")
        if not stat.S_ISREG(path.stat().st_mode):
            raise StageFailure(f"{operation} output is not a regular file: {path}")

    def _output_operation(
        self,
        operation: str,
        command: list[str],
        output: Path,
        *,
        redactions: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._clear_output(output)
        self._run(operation, command, env=self.environment, redactions=redactions)
        self._require_regular_output(operation, output)

    def make_skeleton(
        self, current_project: Path, output: Path, rule_set: Path | None = None
    ) -> None:
        assert self.crat_tool is not None
        self._output_operation(
            "crat-tool make-skeleton",
            make_skeleton_command(self.crat_tool, current_project, output, rule_set),
            output,
            redactions=((str(rule_set), "<rule-set>"),) if rule_set is not None else (),
        )

    def merge_observations(self, inputs: tuple[Path, ...], output: Path) -> None:
        assert self.crat_tool is not None
        self._output_operation(
            "crat-tool merge-observations",
            merge_observations_command(self.crat_tool, output, inputs),
            output,
        )

    def normalize(self, library_source: Path, output: Path) -> None:
        assert self.crat_tool is not None
        self._output_operation(
            "crat-tool normalize-safety",
            normalize_safety_command(self.crat_tool, library_source, output),
            output,
        )

    def validate(self, request: Path, response: Path) -> tuple[str, dict[str, Any]]:
        assert self.crat_tool is not None
        self._output_operation(
            "crat-tool validate",
            validate_command(self.crat_tool, request, response),
            response,
        )
        raw = response.read_text(encoding="utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StageFailure(f"validator response is malformed JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise StageFailure("validator response must be a JSON object")
        return raw, value

    @staticmethod
    def _clear_replace_output(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            raise StageFailure(
                f"crat-tool replace output destination is not a regular file "
                f"or symlink: {path}"
            )

    def replace(
        self,
        current_project: Path,
        request: Path,
        output: Path,
        statement_pairs_output: Path,
        observation_source_output: Path | None = None,
        observation_metadata_output: Path | None = None,
    ) -> None:
        assert self.crat_tool is not None
        observation_source_output = observation_source_output or output.with_name(
            "replacement-observation.rs"
        )
        observation_metadata_output = observation_metadata_output or output.with_name(
            "replacement-observation-metadata.json"
        )
        paths = (
            output,
            statement_pairs_output,
            observation_source_output,
            observation_metadata_output,
        )
        if len(set(paths)) != len(paths):
            raise StageFailure(
                "crat-tool replace output paths must be pairwise distinct"
            )
        for path in paths:
            self._clear_replace_output(path)
        try:
            self._run(
                "crat-tool replace",
                replace_command(
                    self.crat_tool,
                    current_project,
                    request,
                    output,
                    statement_pairs_output,
                    observation_source_output,
                    observation_metadata_output,
                ),
                env=self.environment,
            )
            self._require_regular_output("crat-tool replace", output)
            self._require_regular_output(
                "crat-tool replace statement pairs", statement_pairs_output
            )
            self._require_regular_output(
                "crat-tool replace observation source", observation_source_output
            )
            self._require_regular_output(
                "crat-tool replace observation metadata", observation_metadata_output
            )
        except Exception:
            for path in paths:
                if path.is_symlink() or path.is_file():
                    path.unlink()
            raise

    def extract_observations(
        self,
        observation_source: Path,
        metadata: Path,
        output: Path,
    ) -> None:
        assert self.crat_tool is not None
        paths = (observation_source, metadata, output)
        if len(set(paths)) != len(paths):
            raise StageFailure(
                "crat-tool extract-observations paths must be pairwise distinct"
            )
        self._clear_replace_output(output)
        try:
            self._run(
                "crat-tool extract-observations",
                extract_observations_command(
                    self.crat_tool, observation_source, metadata, output
                ),
                env=self.environment,
            )
            self._require_regular_output("crat-tool extract-observations", output)
        except Exception:
            if output.is_symlink() or output.is_file():
                output.unlink()
            raise

    def cargo_build(self, current_project: Path) -> CommandResult:
        result = self.run_command(["cargo", "build"], cwd=current_project, env=None)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write("$ cargo build\n")
            log.write(result.stdout)
            log.write(result.stderr)
        return result


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def install_candidate_transaction(
    library_source: Path,
    candidate: Path,
    rollback_dir: Path,
    builder: Callable[[], CommandResult],
    *,
    atomic_replace: Callable[[Path, Path], None] = os.replace,
) -> CommandResult:
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rollback = rollback_dir / "library-source.rollback"
    if rollback.exists():
        rollback.unlink()
    shutil.copy2(library_source, rollback)
    atomic_replace(candidate, library_source)
    try:
        build_result = builder()
    except Exception as build_error:
        try:
            atomic_replace(rollback, library_source)
        except OSError as restore_error:
            raise StageFailure(
                "failed to restore library source after builder exception "
                f"{type(build_error).__name__}: {build_error}: {restore_error}"
            ) from restore_error
        raise
    if build_result.returncode == 0:
        rollback.unlink()
        return build_result
    try:
        atomic_replace(rollback, library_source)
    except OSError as restore_error:
        raise StageFailure(
            "failed to restore library source after cargo build failure "
            f"({build_result.returncode}); stdout: {build_result.stdout!r}; "
            f"stderr: {build_result.stderr!r}: {restore_error}"
        ) from restore_error
    return build_result
