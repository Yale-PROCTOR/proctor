#!/usr/bin/env python3
"""Envelope adapter for CRAT (stages/crat submodule).

Builds crat once per submodule commit (cached via a marker file), then
runs the pass chain over the input Rust project, feeding each pass's
output to the next. The default pass list and flags mirror the legacy
scripts/transform.py and can be overridden in stage config. Purely
symbolic: no LLM usage to report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

STAGE_ID = "crat"
STAGE_VERSION = "0.2.0"
SCHEMA_VERSION = 1

# pass -> (previous pass, extra flags); "c2rust" marks the chain root.
PLUGINS: dict[str, tuple[str, list[str]]] = {
    "expand": ("c2rust", []),
    "extern": ("expand", ["--extern-ignore-return-type", "--extern-ignore-param-type"]),
    "preprocess": ("extern", []),
    "outparam": ("preprocess", ["--outparam-simplify"]),
    "punning": ("outparam", []),
    "enum": ("punning", []),
    "prepare": ("enum", []),
    "pointer": ("enum", []),
    "io": ("pointer", ["--io-assume-to-str-ok"]),
    "libc": ("io", []),
    "static": ("libc", []),
    "simpl": ("static", []),
    "interface": ("simpl", []),
    "unsafe": (
        "interface",
        [
            "--unsafe-remove-unused",
            "--unsafe-remove-no-mangle",
            "--unsafe-replace-pub",
            "--unsafe-remove-extern-c",
        ],
    ),
    "unexpand": ("unsafe", ["--unexpand-use-print"]),
    "split": ("unexpand", []),
    "bin": ("split", []),
}


class StageFailure(Exception):
    pass


def plugin_chain(final_pass: str) -> list[str]:
    if not isinstance(final_pass, str) or final_pass not in PLUGINS:
        raise StageFailure(f"unknown crat pass {final_pass!r}")
    chain = [final_pass]
    while PLUGINS[chain[-1]][0] != "c2rust":
        chain.append(PLUGINS[chain[-1]][0])
    chain.reverse()
    return chain


def resolve_pass_plan(config: dict) -> list[tuple[str, list[str]]]:
    if "passes" in config:
        if "final_pass" in config:
            raise StageFailure("crat config cannot set both passes and final_pass")
        passes = config["passes"]
        if (
            not isinstance(passes, list)
            or not passes
            or not all(
                isinstance(plugin, str) and plugin in PLUGINS for plugin in passes
            )
        ):
            raise StageFailure(
                f"passes must be a non-empty list of known CRAT passes: "
                f"{', '.join(PLUGINS)}"
            )
        if len(set(passes)) != len(passes):
            raise StageFailure("passes must not contain duplicates")
    else:
        passes = plugin_chain(config.get("final_pass", "bin"))

    pass_args = config.get("pass_args", {})
    if not isinstance(pass_args, dict):
        raise StageFailure("pass_args must be a table of pass-name to argument list")
    unknown = [name for name in pass_args if name not in PLUGINS]
    if unknown:
        raise StageFailure(
            f"pass_args contains unknown CRAT passes: {', '.join(unknown)}"
        )
    unselected = [name for name in pass_args if name not in passes]
    if unselected:
        raise StageFailure(
            f"pass_args contains passes not selected for this run: "
            f"{', '.join(unselected)}"
        )

    plan: list[tuple[str, list[str]]] = []
    for plugin in passes:
        args = pass_args.get(plugin, PLUGINS[plugin][1])
        if not isinstance(args, list) or not all(
            isinstance(arg, str) and arg for arg in args
        ):
            raise StageFailure(f"pass_args.{plugin} must be a list of strings")
        plan.append((plugin, list(args)))
    return plan


def run_logged(
    command: list[str],
    log_file: Path,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    with log_file.open("ab") as log:
        log.write(f"$ {' '.join(command)}\n".encode())
        log.flush()
        result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=log)
    if result.returncode != 0:
        tail = log_file.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise StageFailure(
            f"command failed ({result.returncode}): {' '.join(command)}\n...{tail}"
        )


def git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def ensure_crat_built(crat_dir: Path, log_file: Path) -> Path:
    """Build crat once per commit; rust-toolchain.toml pins the nightly
    and components, so rustup handles toolchain setup on first build."""
    binary = crat_dir / "target" / "release" / "crat"
    marker = crat_dir / "target" / ".proctor-build-sha"
    head = git_head(crat_dir)
    if binary.is_file() and marker.is_file() and marker.read_text().strip() == head:
        return binary
    run_logged(["cargo", "build"], log_file, cwd=crat_dir / "deps_crate")
    run_logged(["cargo", "build", "--release", "--bin", "crat"], log_file, cwd=crat_dir)
    if not binary.is_file():
        raise StageFailure(f"crat build produced no binary at {binary}")
    marker.write_text(head + "\n", encoding="utf-8")
    return binary


def crat_env(crat_dir: Path) -> dict[str, str]:
    sysroot = subprocess.check_output(
        ["rustc", "--print", "sysroot"], cwd=crat_dir, text=True
    ).strip()
    env = os.environ.copy()
    env["DIR"] = str(crat_dir)
    env["SYSROOT"] = sysroot
    # Library search path: rustc sysroot (rustc_private libs), plus the
    # proctor cache's userspace z3 (tests/e2e/README.md recipe) when
    # present, plus whatever the caller already set.
    paths = [str(Path(sysroot) / "lib")]
    cache_z3 = (
        Path(os.environ.get("PROCTOR_CACHE_DIR", Path.home() / ".cache" / "proctor"))
        / "z3"
        / "bin"
    )
    if cache_z3.is_dir():
        paths.append(str(cache_z3))
    if env.get("LD_LIBRARY_PATH"):
        paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(paths)
    return env


def run_pass(
    crat_bin: Path,
    env: dict[str, str],
    plugin: str,
    flags: list[str],
    input_dir: Path,
    output_root: Path,
    log_file: Path,
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    command = [
        str(crat_bin),
        "-o",
        str(output_root),
        "--config",
        str(input_dir / "config.toml"),
        "--pass",
        plugin,
        *flags,
        str(input_dir),
    ]
    run_logged(command, log_file, env=env)
    produced = output_root / input_dir.name
    if not produced.is_dir():
        raise StageFailure(f"pass {plugin!r} produced nothing at {produced}")
    return produced


def emit_proctor_toml(project: Path) -> dict:
    """Write the project manifest (component spec §3), derived from
    crat's config.toml and Cargo.toml. The crat stage is the last stage
    of the Translation component, so this is where downstream stages'
    proctor.toml is born — with an empty wrapper list.
    """
    config_file = project / "config.toml"
    cfg: dict = {}
    if config_file.is_file():
        cfg = tomllib.loads(config_file.read_text(encoding="utf-8"))
    exposed = [f for f in cfg.get("c_exposed_fns", []) if isinstance(f, str)]
    bin_table = cfg.get("bin")
    bin_name = bin_table.get("name") if isinstance(bin_table, dict) else None

    if isinstance(bin_name, str) and bin_name:
        kind, name, api = "executable", bin_name, []
    else:
        cargo = tomllib.loads((project / "Cargo.toml").read_text(encoding="utf-8"))
        lib_table = cargo.get("lib")
        lib_name = lib_table.get("name") if isinstance(lib_table, dict) else None
        name = lib_name or cargo.get("package", {}).get("name", "unknown")
        kind, api = "library", exposed

    lines = [
        f'target_kind = "{kind}"',
        f'target_name = "{name}"',
        f"api_functions = {json.dumps(api)}",
        "wrappers = []",
    ]
    (project / "proctor.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"target_kind": kind, "target_name": name, "api_functions": len(api)}


def run_stage(envelope: dict) -> dict:
    config = envelope.get("config", {})
    src = envelope["inputs"]["rust_project"]
    dst = envelope["outputs"]["rust_project"]
    workdir = envelope.get("framework", {}).get("workdir")
    artifacts = envelope["outputs"].get("artifacts_dir")
    if src is None or dst is None or workdir is None:
        raise StageFailure("crat adapter needs rust_project in/out and a workdir")

    src_dir = Path(src)
    if not (src_dir / "config.toml").is_file():
        raise StageFailure(
            f"{src_dir} has no config.toml (crat needs the c2rust-stage config)"
        )
    plan = resolve_pass_plan(config)
    check_build = config.get("check_build", True)
    if not isinstance(check_build, bool):
        raise StageFailure("check_build must be a boolean")

    log_file = Path(artifacts or workdir) / "crat.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    adapter_dir = Path(__file__).resolve().parent
    crat_dir = (adapter_dir / config.get("crat_dir", "../crat")).resolve()
    if not crat_dir.is_dir():
        raise StageFailure(
            f"crat checkout not found at {crat_dir} "
            f"(run: git submodule update --init stages/crat)"
        )

    build_started = time.monotonic()
    crat_bin = ensure_crat_built(crat_dir, log_file)
    build_s = round(time.monotonic() - build_started, 1)
    env = crat_env(crat_dir)

    pass_seconds: dict[str, float] = {}
    current = src_dir
    for plugin, flags in plan:
        started = time.monotonic()
        current = run_pass(
            crat_bin, env, plugin, flags, current, Path(workdir) / plugin, log_file
        )
        pass_seconds[plugin] = round(time.monotonic() - started, 2)

    check_metrics: dict[str, float] = {}
    if check_build:
        started = time.monotonic()
        run_logged(
            ["cargo", "build"],
            log_file,
            cwd=current,
            env={**os.environ, "RUSTFLAGS": "-Awarnings"},
        )
        shutil.rmtree(current / "target", ignore_errors=True)
        check_metrics["check_build_s"] = round(time.monotonic() - started, 2)

    shutil.copytree(current, dst)
    manifest = emit_proctor_toml(Path(dst))

    config_used = {
        "passes": [plugin for plugin, _ in plan],
        "pass_args": {plugin: flags for plugin, flags in plan},
        "crat_dir": str(crat_dir),
        "check_build": check_build,
    }
    if "passes" not in config:
        config_used["final_pass"] = config.get("final_pass", "bin")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "stage_id": STAGE_ID,
        "stage_version": STAGE_VERSION,
        "outputs": {"rust_project": dst, "rule_set": None},
        "config_used": config_used,
        "metrics": {
            "crat_commit": git_head(crat_dir),
            "build_s": build_s,
            "passes": len(plan),
            **manifest,
            **check_metrics,
            **{f"pass_s.{name}": secs for name, secs in pass_seconds.items()},
        },
        "logs": ["crat.log"],
        "metadata": {},
        "error": None,
    }


def build_only() -> int:
    """Warmup entry point: build crat, no pipeline work."""
    adapter_dir = Path(__file__).resolve().parent
    crat_dir = (adapter_dir / "../crat").resolve()
    cache = Path(
        os.environ.get("PROCTOR_CACHE_DIR", Path.home() / ".cache" / "proctor")
    )
    cache.mkdir(parents=True, exist_ok=True)
    log_file = cache / "crat-build.log"
    try:
        binary = ensure_crat_built(crat_dir, log_file)
    except StageFailure as exc:
        print(f"crat build failed: {exc}", file=sys.stderr)
        return 1
    print(f"crat ready: {binary}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.build_only:
        return build_only()
    if args.input is None or args.output is None:
        parser.error("--input and --output are required unless --build-only")

    envelope = json.loads(args.input.read_text(encoding="utf-8"))
    try:
        output = run_stage(envelope)
    except StageFailure as exc:
        output = {
            "schema_version": SCHEMA_VERSION,
            "status": "failure",
            "stage_id": STAGE_ID,
            "stage_version": STAGE_VERSION,
            "error": str(exc),
        }
    except Exception as exc:  # never die without an envelope
        output = {
            "schema_version": SCHEMA_VERSION,
            "status": "failure",
            "stage_id": STAGE_ID,
            "stage_version": STAGE_VERSION,
            "error": f"{type(exc).__name__}: {exc}",
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return 0 if output["status"] != "failure" else 1


if __name__ == "__main__":
    sys.exit(main())
