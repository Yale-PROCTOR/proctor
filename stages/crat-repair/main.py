#!/usr/bin/env python3
"""Apply fixed Crat commands, accepting only projects that build."""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proctor.contracts import StageInput, StageOutput
from proctor.contracts.stage_io import (
    ModelInfo,
    OutputDestinations,
    PromptUse,
    UsageSummary,
)

STAGE_ID = "crat_repair"
STAGE_VERSION = "0.1.0"
STAGE_DIR = Path(__file__).resolve().parent
PASS_PLAN = (
    ("expand", ()),
    ("extern", ("--extern-ignore-return-type", "--extern-ignore-param-type")),
    ("preprocess", ()),
    ("outparam", ("--outparam-simplify",)),
    ("punning", ()),
    ("enum", ()),
    ("pointer-struct-arrays", ()),
    ("pointer-struct-param-fields", ()),
    ("pointer-epoch-split", ()),
    ("pointer-array-local-provenance", ()),
    ("pointer", ("--pointer-skip-preprocessing",)),
    ("io", ("--io-assume-to-str-ok",)),
    ("libc", ()),
    ("static", ()),
    ("simpl", ()),
    ("interface", ()),
    (
        "unsafe",
        (
            "--unsafe-remove-unused",
            "--unsafe-remove-no-mangle",
            "--unsafe-replace-pub",
            "--unsafe-remove-extern-c",
        ),
    ),
    ("unexpand", ("--unexpand-use-print",)),
    ("split", ()),
    ("bin", ()),
)
DEFAULTS = {
    "enable_interface": False,
    "agent": "opencode",
    "model": None,
    "max_repair_attempts": 3,
    "crat_timeout_s": 600,
    "build_timeout_s": 600,
    "agent_timeout_s": 900,
    "crat_dir": "../crat",
}


class StageFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def diagnostics(self) -> str:
        return f"stdout:\n{self.stdout}\nstderr:\n{self.stderr}"


def run_command(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float, log: Path
) -> CommandResult:
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"$ {shlex.join(command)}\n")
        stream.flush()
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:

            def kill_group() -> None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            def cancel(signum: int, _frame: Any) -> None:
                kill_group()
                raise SystemExit(128 + signum)

            previous_handlers = {
                signum: signal.signal(signum, cancel)
                for signum in (signal.SIGTERM, signal.SIGINT)
            }
            timed_out = False
            try:
                try:
                    stdout, stderr = process.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    kill_group()
                    stdout, stderr = process.communicate()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
            stream.write(stdout)
            stream.write(stderr)
            if timed_out:
                stream.write(f"\nTimed out after {timeout_s}s\n")
        return CommandResult(process.returncode, stdout, stderr, timed_out)


def require_success(result: CommandResult, operation: str) -> None:
    if result.returncode or result.timed_out:
        raise StageFailure(f"{operation} failed: {result.diagnostics}")


def resolve_config(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = raw.keys() - DEFAULTS.keys()
    if unknown:
        raise StageFailure(
            f"unknown crat-repair configuration: {', '.join(sorted(unknown))}"
        )
    config = {**DEFAULTS, **raw}
    if not isinstance(config["enable_interface"], bool):
        raise StageFailure("enable_interface must be a boolean")
    attempts = config["max_repair_attempts"]
    if type(attempts) is not int or attempts < 1:
        raise StageFailure("max_repair_attempts must be a positive integer")
    for name in ("crat_timeout_s", "build_timeout_s", "agent_timeout_s"):
        value = config[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise StageFailure(f"{name} must be a finite positive number")
    for name in ("agent", "crat_dir", "model"):
        value = config[name]
        if name == "model" and value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise StageFailure(f"{name} must be a nonempty string")
    config["crat_dir"] = str((STAGE_DIR / config["crat_dir"]).resolve())
    return config


def cargo_environment(target: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(target)
    env["CARGO_BUILD_BUILD_DIR"] = str(target)
    if "CARGO_ENCODED_RUSTFLAGS" in env:
        flags = env["CARGO_ENCODED_RUSTFLAGS"]
        env["CARGO_ENCODED_RUSTFLAGS"] = (
            f"{flags}\x1f-Awarnings" if flags else "-Awarnings"
        )
    else:
        key = "RUSTFLAGS" if "RUSTFLAGS" in env else "CARGO_BUILD_RUSTFLAGS"
        env[key] = f"{env.get(key, '')} -Awarnings".strip()
    return env


def build_crat(config: dict[str, Any], log: Path) -> tuple[Path, dict[str, str]]:
    root = Path(config["crat_dir"])
    env = os.environ.copy()
    timeout = config["build_timeout_s"]
    host = run_command(
        ["rustc", "--print", "host-tuple"],
        cwd=root,
        env=env,
        timeout_s=timeout,
        log=log,
    )
    require_success(host, "rustc host discovery")
    sysroot = run_command(
        ["rustc", "--print", "sysroot"], cwd=root, env=env, timeout_s=timeout, log=log
    )
    require_success(sysroot, "rustc sysroot discovery")
    for directory, command, target in (
        (
            root / "deps_crate",
            ["cargo", "build", "--target", host.stdout.strip()],
            root / "deps_crate" / "target",
        ),
        (
            root,
            [
                "cargo",
                "build",
                "--release",
                "--bin",
                "crat",
                "--target",
                host.stdout.strip(),
            ],
            root / "target",
        ),
    ):
        build_env = {
            **env,
            "CARGO_TARGET_DIR": str(target),
            "CARGO_BUILD_BUILD_DIR": str(target),
        }
        result = run_command(
            command, cwd=directory, env=build_env, timeout_s=timeout, log=log
        )
        require_success(result, "Crat tool build")
    binary = root / "target" / host.stdout.strip() / "release" / "crat"
    if not binary.is_file():
        raise StageFailure(f"Crat build produced no binary at {binary}")
    env["DIR"] = str(root)
    env["SYSROOT"] = sysroot.stdout.strip()
    libraries = [str(Path(env["SYSROOT"]) / "lib")]
    z3 = Path(env.get("PROCTOR_CACHE_DIR", Path.home() / ".cache/proctor")) / "z3/bin"
    if z3.is_dir():
        libraries.append(str(z3))
    if env.get("LD_LIBRARY_PATH"):
        libraries.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(libraries)
    return binary, env


def copy_project(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=lambda path, names: (
            {"target"} if Path(path) == source and "target" in names else set()
        ),
    )


def repair_prompt(plugin: str, result: CommandResult) -> str:
    return (
        f"The Crat pass `{plugin}` left this Rust project failing to compile.\n"
        "You can read and edit the full current project in your working directory.\n"
        "Make only the minimal edits needed to fix the compiler errors. Preserve "
        "the transformation, existing interfaces, and observable behavior. Do not "
        "replace implementations with stubs, disable code, or undo the pass. "
        "Do not edit config.toml or change build settings to bypass compilation.\n"
        "Verify with `cargo build --quiet`; the environment already suppresses "
        "warnings and selects the private Cargo cache.\n\n"
        f"Compiler diagnostics:\n{result.diagnostics}\n"
    )


def run_agent(
    config: dict[str, Any],
    project: Path,
    prompt: str,
    directory: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    request = directory / "request.json"
    result_path = directory / "agent.json"
    directory.mkdir()
    request.write_text(
        json.dumps(
            {
                "agent": config["agent"],
                "model": config["model"],
                "workspace": str(project),
                "prompt": prompt,
                "timeout_s": config["agent_timeout_s"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    generated_config = config["agent"] == "opencode" and not any(
        (project / name).exists() for name in ("opencode.json", ".opencode.json")
    )
    prompt_file = project / ".agent_prompt.md"
    original_prompt = prompt_file.read_bytes() if prompt_file.is_file() else None
    try:
        result = run_command(
            [
                sys.executable,
                str(STAGE_DIR / "repair_agent.py"),
                "--request",
                str(request),
                "--result",
                str(result_path),
            ],
            cwd=project,
            env=env,
            timeout_s=config["agent_timeout_s"],
            log=directory / "driver.log",
        )
    finally:
        if generated_config:
            (project / "opencode.json").unlink(missing_ok=True)
        if original_prompt is None:
            prompt_file.unlink(missing_ok=True)
        else:
            prompt_file.write_bytes(original_prompt)
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    if not result.timed_out:
        require_success(result, "repair agent driver")
    return {"metrics": {}, "timed_out": result.timed_out}


def write_manifest(
    project: Path, project_config: dict[str, Any], metadata: dict[str, Any]
) -> None:
    manifest = project / "Cargo.toml"
    package = next(
        (
            p
            for p in metadata["packages"]
            if Path(p["manifest_path"]).resolve() == manifest.resolve()
        ),
        None,
    )
    if package is None:
        raise StageFailure("Cargo metadata does not describe the transformed project")
    binary_name = project_config.get("bin", {}).get("name")
    if binary_name:
        targets = [
            t
            for t in package["targets"]
            if "bin" in t["kind"] and t["name"] == binary_name
        ]
    else:
        targets = [
            t
            for t in package["targets"]
            if any(
                k in {"lib", "rlib", "staticlib", "cdylib", "dylib"} for k in t["kind"]
            )
        ]
    if not targets or not Path(targets[0]["src_path"]).is_file():
        raise StageFailure(
            f"final project is missing its declared {'binary ' + binary_name if binary_name else 'library'} target"
        )
    api = [] if binary_name else project_config.get("c_exposed_fns", [])
    lines = [
        f'target_kind = "{"executable" if binary_name else "library"}"',
        f"target_name = {json.dumps(targets[0]['name'])}",
        f"api_functions = {json.dumps(api)}",
        "wrappers = []",
    ]
    (project / "proctor.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_stage(stage_input: StageInput) -> StageOutput:
    config: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    usage = {
        "calls": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
    }
    artifacts: Path | None = None
    destination: Path | None = None
    published = False
    error: str | None = None
    try:
        config = resolve_config(stage_input.config)
        source = stage_input.inputs.rust_project
        destination = stage_input.outputs.rust_project
        work = stage_input.framework.workdir
        if source is None or destination is None or work is None:
            raise StageFailure("crat-repair requires a Rust input, output, and workdir")
        artifacts_path = stage_input.outputs.artifacts_dir or work / "artifacts"
        source, destination, work, artifacts_path = (
            p.resolve() for p in (source, destination, work, artifacts_path)
        )
        protected = [
            p.resolve()
            for p in (
                stage_input.inputs.rust_project,
                stage_input.inputs.c_project,
                stage_input.inputs.test_package,
                stage_input.inputs.rule_set,
            )
            if p is not None
        ]
        for output in (destination, work, artifacts_path):
            if any(
                output == p or output.is_relative_to(p) or p.is_relative_to(output)
                for p in protected
            ):
                raise StageFailure(
                    "stage outputs and workdir must not overlap input artifacts"
                )
        if (
            destination.exists()
            or destination.is_symlink()
            or destination.is_relative_to(work)
            or work.is_relative_to(destination)
            or destination.is_relative_to(artifacts_path)
            or artifacts_path.is_relative_to(destination)
        ):
            raise StageFailure(
                "Rust output must be new and separate from work and artifacts"
            )
        project_config = tomllib.loads(
            (source / "config.toml").read_text(encoding="utf-8")
        )
        if project_config.get("passes") or project_config.get("analysis"):
            raise StageFailure(
                "project config.toml must not select Crat passes or analyses"
            )
        work.mkdir(parents=True, exist_ok=True)
        artifacts_path.mkdir(parents=True, exist_ok=True)
        if any(
            (artifacts_path / name).exists() or (artifacts_path / name).is_symlink()
            for name in ("commands.log", "statistics.json")
        ):
            raise StageFailure("stage artifacts already exist")
        artifacts = artifacts_path
        log = artifacts / "commands.log"
        log.touch()
        (work / "initial").mkdir()
        (work / "cargo-target").mkdir()
        env = cargo_environment(work / "cargo-target")
        current = work / "initial" / "project"
        copy_project(source, current)

        def build(project: Path, *target_args: str) -> CommandResult:
            result = run_command(
                ["cargo", "build", "--quiet", *target_args],
                cwd=project,
                env=env,
                timeout_s=config["build_timeout_s"],
                log=log,
            )
            if result.timed_out:
                raise StageFailure("Cargo build timed out")
            return result

        require_success(build(current), "initial Cargo build")
        binary, crat_env = build_crat(config, log)
        for plugin, flags in PASS_PLAN:
            if plugin == "interface" and not config["enable_interface"]:
                continue
            record = {"pass": plugin, "status": "failure", "repair_calls": 0}
            records.append(record)
            pass_root = work / plugin
            pass_root.mkdir()
            candidate = pass_root / current.name
            result = run_command(
                [
                    str(binary),
                    "--config",
                    str(current / "config.toml"),
                    "--pass",
                    plugin,
                    *flags,
                    "-o",
                    str(pass_root),
                    str(current),
                ],
                cwd=Path(config["crat_dir"]),
                env=crat_env,
                timeout_s=config["crat_timeout_s"],
                log=log,
            )
            if result.returncode or result.timed_out:
                shutil.rmtree(pass_root)
                record.update(
                    status="skipped",
                    returncode=result.returncode,
                    timed_out=result.timed_out,
                )
                require_success(build(current), f"Cargo build after skipping {plugin}")
                continue
            if not candidate.is_dir():
                raise StageFailure(f"Crat pass {plugin} produced no project")
            result = build(candidate)
            original_config = (candidate / "config.toml").read_bytes()
            for attempt in range(1, config["max_repair_attempts"] + 1):
                if result.returncode == 0:
                    break
                record["repair_calls"] += 1
                usage["calls"] += 1
                agent_result = run_agent(
                    config,
                    candidate,
                    repair_prompt(plugin, result),
                    artifacts / f"{plugin}-repair-{attempt}",
                    env,
                )
                metrics = agent_result.get("metrics", {})
                for output_key, metric_key in (
                    ("input_tokens", "input_tokens"),
                    ("cached_input_tokens", "cached_read_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("reasoning_tokens", "reasoning_tokens"),
                    ("cost_usd", "cost_usd"),
                ):
                    usage[output_key] += metrics.get(metric_key, 0) or 0
                if (candidate / "config.toml").read_bytes() != original_config:
                    raise StageFailure("repair agent changed protected config.toml")
                result = build(candidate)
            require_success(
                result, f"Cargo build after {plugin} ({record['repair_calls']} repairs)"
            )
            current = candidate
            record["status"] = "repaired" if record["repair_calls"] else "success"

        metadata = run_command(
            ["cargo", "metadata", "--quiet", "--no-deps", "--format-version", "1"],
            cwd=current,
            env=env,
            timeout_s=config["build_timeout_s"],
            log=log,
        )
        require_success(metadata, "final Cargo metadata")
        write_manifest(current, project_config, json.loads(metadata.stdout))
        binary_name = project_config.get("bin", {}).get("name")
        target_args = ("--bin", binary_name) if binary_name else ("--lib",)
        require_success(build(current, *target_args), "final declared target build")
        try:
            copy_project(current, destination)
        except Exception:
            if destination.exists():
                shutil.rmtree(destination)
            raise
        published = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    metrics = {
        "passes": len(records),
        "skipped_passes": sum(r["status"] == "skipped" for r in records),
        "repair_calls": usage["calls"],
    }
    if artifacts is not None:
        try:
            (artifacts / "statistics.json").write_text(
                json.dumps(
                    {"passes": records, "usage": usage, "error": error}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            error = f"failed to write stage statistics: {exc}"
    if error is not None and published:
        shutil.rmtree(destination)
        published = False
    return StageOutput(
        status="failure" if error else "success",
        stage_id=stage_input.stage_id,
        stage_version=STAGE_VERSION,
        outputs=OutputDestinations(rust_project=destination if published else None),
        config_used=config,
        models=(ModelInfo(provider=config["agent"], model=config["model"]),)
        if usage["calls"] and config.get("model")
        else (),
        usage=UsageSummary(**usage),
        prompts=(PromptUse(id="crat_compile_repair", version=1),)
        if usage["calls"]
        else (),
        metrics=metrics,
        logs=("commands.log", "statistics.json") if artifacts else (),
        metadata={"passes": records},
        error=error,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args(argv)
    if args.build_only:
        try:
            build_crat(resolve_config({}), STAGE_DIR / "build.log")
        except Exception as exc:
            print(f"Crat build failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.input is None or args.output is None:
        parser.error("--input and --output are required unless --build-only is used")
    try:
        output = run_stage(StageInput.read(args.input))
    except Exception as exc:
        output = StageOutput(
            status="failure",
            stage_id=STAGE_ID,
            stage_version=STAGE_VERSION,
            error=f"{type(exc).__name__}: {exc}",
        )
    output.write(args.output)
    return 0 if output.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
