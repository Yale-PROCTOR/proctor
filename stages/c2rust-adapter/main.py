#!/usr/bin/env python3
"""Envelope adapter for c2rust transpilation — the first half of the
component spec's Translation component (the crat stage is the second).

Ports the single-configuration path of the legacy scripts/translate.py:
cmake file-api configure → API-function discovery (libclang over the
project headers) → c2rust-transpile → Cargo project fix-ups →
crat's config.toml. Parameter sets / CMakePresets matrices / crat-merge
are deliberately out of scope (spec assumes one target, one config).

Transpiler resolution order:
  1. `c2rust-transpile` on PATH
  2. prebuilt under $PROCTOR_CACHE_DIR/c2rust/{bin,lib} (see tests/e2e/README.md)
  3. built from the stages/c2rust submodule (needs clang/LLVM dev libs)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STAGE_ID = "c2rust"
STAGE_VERSION = "0.2.0"
SCHEMA_VERSION = 1


class StageFailure(Exception):
    pass


def _cache_dir() -> Path:
    return Path(os.environ.get("PROCTOR_CACHE_DIR", Path.home() / ".cache" / "proctor"))


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


def resolve_transpiler(log_file: Path) -> tuple[Path, dict[str, str]]:
    """Locate or build c2rust-transpile; returns (binary, env)."""
    env = os.environ.copy()

    on_path = shutil.which("c2rust-transpile")
    if on_path:
        return Path(on_path), env

    cached = _cache_dir() / "c2rust" / "bin" / "c2rust-transpile"
    if cached.is_file():
        lib_dir = _cache_dir() / "c2rust" / "lib"
        if lib_dir.is_dir():
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = (
                f"{lib_dir}:{existing}" if existing else str(lib_dir)
            )
        return cached, env

    submodule = (Path(__file__).resolve().parent / "../c2rust").resolve()
    if not submodule.is_dir():
        raise StageFailure(
            "c2rust-transpile not found on PATH, not in "
            f"{cached.parent}, and no submodule at {submodule} "
            "(run: git submodule update --init stages/c2rust)"
        )
    binary = submodule / "target" / "release" / "c2rust-transpile"
    if not binary.is_file():
        run_logged(
            ["cargo", "build", "--release", "--bin", "c2rust-transpile"],
            log_file,
            cwd=submodule,
        )
    if not binary.is_file():
        raise StageFailure(f"c2rust build produced no binary at {binary}")
    return binary, env


def find_c_root(c_project: Path) -> Path:
    """The directory holding CMakeLists.txt — TRACTOR cases keep the
    buildable project under test_case/."""
    for candidate in (c_project, c_project / "test_case"):
        if (candidate / "CMakeLists.txt").is_file():
            return candidate
    raise StageFailure(f"no CMakeLists.txt under {c_project} or its test_case/")


def restore_case_name(source_dir: Path, item: str | None, work: Path) -> Path:
    """Stage the C source under a parent directory named after the case.

    TRACTOR CMakeLists derive the library target name from the C source's
    PARENT directory (``cmake_path(GET CMAKE_CURRENT_SOURCE_DIR
    PARENT_PATH ...)``). The framework records the C input under
    ``inputs/c/``, so that derivation would name the library ``c`` and
    build ``libc.so`` — but the cando runner dlopens ``lib<case>.so``.
    When the case name is known (envelope ``item``), copy the source
    under a parent named after it so the built cdylib matches. Cases
    whose CMakeLists name the target explicitly are unaffected.
    """
    if not item:
        return source_dir
    case_name = Path(item).name
    if not case_name or source_dir.resolve().parent.name == case_name:
        return source_dir
    staged = work / "named" / case_name / source_dir.name
    if not staged.exists():
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, staged)
    return staged


def prepare_c_root(c_project: Path, workdir: Path) -> Path:
    """Return the buildable C project root, extracting a tar input if needed."""
    if c_project.is_dir():
        return find_c_root(c_project)
    if not c_project.is_file():
        raise StageFailure(f"C project input {c_project} does not exist")

    extracted = workdir / "c-project"
    extracted.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(c_project, mode="r:*") as archive:
            archive.extractall(extracted, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise StageFailure(
            f"C project input {c_project} is not a readable tar archive: {exc}"
        ) from exc

    if (extracted / "CMakeLists.txt").is_file():
        return extracted

    containers = [extracted, *(path for path in extracted.iterdir() if path.is_dir())]
    roots: list[Path] = []
    for container in containers:
        for candidate in (container, container / "test_case"):
            if (candidate / "CMakeLists.txt").is_file():
                roots.append(candidate)
    roots = _unique(roots)
    if len(roots) == 1:
        return roots[0]
    if not roots:
        raise StageFailure(
            f"tar archive {c_project} contains no CMakeLists.txt at its root, "
            "under test_case/, or under one top-level directory"
        )
    raise StageFailure(
        f"tar archive {c_project} contains multiple C project roots: "
        + ", ".join(str(root.relative_to(extracted)) for root in roots)
    )


def pick_generator() -> str:
    if shutil.which("ninja"):
        return "Ninja"
    if shutil.which("make"):
        return "Unix Makefiles"
    raise StageFailure("neither ninja nor make is available for cmake")


@dataclass(frozen=True)
class Artifact:
    name: str
    artifact_type: str
    sources: list[Path]
    link_args: list[str]


def _unique[T](values: list[T]) -> list[T]:
    return list(dict.fromkeys(values))


def read_targets(build_dir: Path) -> list[Artifact]:
    """Targets, transitive C sources, and link args from the cmake
    file-api codemodel (ported from legacy translate.py)."""
    reply_dir = build_dir / ".cmake" / "api" / "v1" / "reply"
    index_path = next(reply_dir.glob("index-*.json"))
    index = json.loads(index_path.read_text(encoding="utf-8"))
    codemodel = json.loads(
        (reply_dir / index["reply"]["codemodel-v2"]["jsonFile"]).read_text(
            encoding="utf-8"
        )
    )
    source_dir = Path(codemodel["paths"]["source"])
    target_entries = codemodel["configurations"][0]["targets"]
    targets = {
        entry["id"]: json.loads(
            (reply_dir / entry["jsonFile"]).read_text(encoding="utf-8")
        )
        for entry in target_entries
    }

    cache: dict[str, list[Path]] = {}

    def resolve_sources(target_id: str) -> list[Path]:
        if target_id in cache:
            return cache[target_id]
        target = targets[target_id]
        sources = [
            source_dir / source["path"]
            for source in target.get("sources", [])
            if "path" in source and Path(source["path"]).suffix == ".c"
        ]
        for dependency in target.get("dependencies", []):
            if dependency["id"] in targets:
                sources.extend(resolve_sources(dependency["id"]))
        cache[target_id] = _unique(sources)
        return cache[target_id]

    def resolve_link_args(target_id: str) -> list[str]:
        return _unique(
            [
                fragment["fragment"]
                for fragment in targets[target_id]
                .get("link", {})
                .get("commandFragments", [])
                if fragment.get("role") == "libraries"
                and fragment["fragment"].startswith("-l")
            ]
        )

    artifacts: list[Artifact] = []
    for target in targets.values():
        name, kind = str(target["name"]), str(target["type"])
        if name == "sphincs_core":  # legacy skip, kept verbatim
            continue
        if kind not in {"EXECUTABLE", "SHARED_LIBRARY"}:
            continue
        artifacts.append(
            Artifact(
                name,
                kind,
                resolve_sources(target["id"]),
                resolve_link_args(target["id"]),
            )
        )
    return artifacts


def exposed_functions(
    compile_commands: list[dict[str, Any]], source_dir: Path
) -> list[str]:
    """Functions declared in the project's own headers — the external
    API a library must preserve. Requires the libclang bindings (this
    stage's venv); skipped when the project has no headers."""
    headers = sorted(source_dir.rglob("*.h"))
    if not headers:
        return []
    from clang.cindex import Cursor, CursorKind, Index

    def preserve_option(option: str) -> bool:
        return option.startswith(("-D", "-I", "-std=", "-m"))

    def command_args(command: dict[str, Any]) -> list[str]:
        arguments = command.get("arguments")
        if isinstance(arguments, list):
            values = [str(value) for value in arguments[1:]]
        else:
            values = shlex.split(str(command["command"]))[1:]
        return [value for value in values if preserve_option(value)]

    parse_args = [
        "-x",
        "c-header",
        *_unique(
            [arg for command in compile_commands for arg in command_args(command)]
        ),
    ]
    names: set[str] = set()

    def visit(node: Cursor) -> None:
        if node.kind == CursorKind.FUNCTION_DECL and node.location.file is not None:
            decl_file = Path(node.location.file.name).resolve()
            if decl_file.is_relative_to(source_dir) and decl_file.suffix == ".h":
                names.add(node.spelling)
        for child in node.get_children():
            visit(child)

    index = Index.create()
    for header in headers:
        visit(index.parse(str(header), args=parse_args).cursor)
    return sorted(names)


def add_link_args_to_build_rs(build_rs: Path, link_args: list[str]) -> None:
    if not link_args or not build_rs.is_file():
        return
    lines = build_rs.read_text(encoding="utf-8").splitlines(keepends=True)
    insert_at = next(
        (i + 1 for i, line in enumerate(lines) if line.strip() == "fn main() {"),
        None,
    )
    if insert_at is None:
        return
    for link_arg in reversed(link_args):
        lines.insert(insert_at, f'    println!("cargo:rustc-link-arg={link_arg}");\n')
    build_rs.write_text("".join(lines), encoding="utf-8")


def add_cdylib_crate_type(cargo_toml: Path) -> None:
    """Append cdylib to [lib] crate-type (text-level, preserving the
    c2rust-generated file otherwise)."""
    if not cargo_toml.is_file():
        return
    data = tomllib.loads(cargo_toml.read_text(encoding="utf-8"))
    crate_types = data.get("lib", {}).get("crate-type")
    if not isinstance(crate_types, list) or "cdylib" in crate_types:
        return
    text = cargo_toml.read_text(encoding="utf-8")
    old = json.dumps(crate_types)[1:-1]  # e.g. '"staticlib", "rlib"'
    quoted_old = ", ".join(f'"{t}"' for t in crate_types)
    for needle in (quoted_old, old):
        if needle in text:
            text = text.replace(needle, needle + ', "cdylib"', 1)
            cargo_toml.write_text(text, encoding="utf-8")
            return
    raise StageFailure(f"could not add cdylib crate-type to {cargo_toml}")


def run_stage(envelope: dict[str, Any]) -> dict[str, Any]:
    config = envelope.get("config", {})
    c_project = envelope["inputs"]["c_project"]
    dst = envelope["outputs"]["rust_project"]
    workdir = envelope.get("framework", {}).get("workdir")
    artifacts_dir = envelope["outputs"].get("artifacts_dir")
    if c_project is None or dst is None or workdir is None:
        raise StageFailure("c2rust adapter needs c_project, rust_project out, workdir")

    work = Path(workdir)
    log_file = Path(artifacts_dir or workdir) / "c2rust.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    source_dir = prepare_c_root(Path(c_project), work)
    source_dir = restore_case_name(source_dir, envelope.get("item"), work)
    timings: dict[str, float] = {}

    # 1. cmake configure with the file API enabled
    started = time.monotonic()
    build_dir = work / "cmake-build"
    query_dir = build_dir / ".cmake" / "api" / "v1" / "query" / "codemodel-v2"
    query_dir.mkdir(parents=True, exist_ok=True)
    preset_name = config.get("preset", "test")
    preset_flag = (
        ["--preset", str(preset_name)]
        if (source_dir / "CMakePresets.json").is_file()
        else []
    )
    run_logged(
        [
            "cmake",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=1",
            "-S",
            str(source_dir),
            "-B",
            str(build_dir),
            "-G",
            pick_generator(),
            *preset_flag,
        ],
        log_file,
    )
    timings["configure_s"] = round(time.monotonic() - started, 2)

    commands_file = build_dir / "compile_commands.json"
    compile_commands = json.loads(commands_file.read_text(encoding="utf-8"))

    # 2. external API discovery (libraries preserve these signatures)
    started = time.monotonic()
    exposed = exposed_functions(compile_commands, source_dir)
    timings["api_discovery_s"] = round(time.monotonic() - started, 2)

    # 3. keep only the compile commands belonging to real targets
    targets = read_targets(build_dir)
    if not targets:
        raise StageFailure("cmake codemodel reports no executable/library targets")
    wanted = {source for target in targets for source in target.sources}
    filtered = [
        command
        for command in compile_commands
        if Path(str(command["file"])).resolve() in wanted
    ]
    commands_file.write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")

    bin_name = next((t.name for t in targets if t.artifact_type == "EXECUTABLE"), None)
    target_name = bin_name or targets[0].name
    shared_names = [t.name for t in targets if t.artifact_type == "SHARED_LIBRARY"]
    extra_libs = (
        shared_names if bin_name else [n for n in shared_names if n != target_name]
    )

    # 4. transpile
    started = time.monotonic()
    transpiler, env = resolve_transpiler(log_file)
    rust_dir = work / "rust" / target_name
    rust_dir.mkdir(parents=True)
    run_logged(
        [str(transpiler), "-o", str(rust_dir), "-e", str(commands_file)],
        log_file,
        env=env,
    )
    timings["transpile_s"] = round(time.monotonic() - started, 2)

    # 5. project fix-ups (legacy translate.py behavior)
    link_args = sorted({arg for target in targets for arg in target.link_args})
    add_link_args_to_build_rs(rust_dir / "build.rs", link_args)
    add_cdylib_crate_type(rust_dir / "Cargo.toml")

    config_lines = [f"c_exposed_fns = {json.dumps(exposed)}"]
    if bin_name:
        config_lines += ["", "[bin]", f'name = "{bin_name}"']
    (rust_dir / "config.toml").write_text(
        "\n".join(config_lines) + "\n", encoding="utf-8"
    )
    (rust_dir / "libs.json").write_text(json.dumps(extra_libs) + "\n", encoding="utf-8")

    # 6. optional build verification
    if config.get("check_build", True):
        started = time.monotonic()
        run_logged(
            ["cargo", "build"],
            log_file,
            cwd=rust_dir,
            env={**os.environ, "RUSTFLAGS": "-Awarnings"},
        )
        shutil.rmtree(rust_dir / "target", ignore_errors=True)
        timings["check_build_s"] = round(time.monotonic() - started, 2)

    shutil.copytree(rust_dir, dst)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "stage_id": STAGE_ID,
        "stage_version": STAGE_VERSION,
        "outputs": {"rust_project": dst, "rule_set": None},
        "config_used": {
            "check_build": bool(config.get("check_build", True)),
            "preset": preset_name if preset_flag else None,
        },
        "metrics": {
            "transpiler": str(transpiler),
            "targets": len(targets),
            "target_name": target_name,
            "target_kind": "executable" if bin_name else "library",
            "c_sources": len(filtered),
            "exposed_fns": len(exposed),
            **timings,
        },
        "logs": ["c2rust.log"],
        "metadata": {},
        "error": None,
    }


def build_only() -> int:
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    try:
        transpiler, _ = resolve_transpiler(cache / "c2rust-build.log")
    except StageFailure as exc:
        print(f"c2rust warmup failed: {exc}", file=sys.stderr)
        return 1
    print(f"c2rust-transpile ready: {transpiler}")
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
