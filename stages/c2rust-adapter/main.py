#!/usr/bin/env python3
"""Envelope adapter for c2rust transpilation — the first half of the
component spec's Translation component (the crat stage is the second).

Ports the single-configuration path of the legacy scripts/translate.py:
cmake file-api configure → API-function discovery (libclang over the
project headers) → c2rust-transpile → Cargo project fix-ups →
crat's config.toml. The adapter handles one configuration per invocation.

Transpiler resolution order:
  1. `c2rust-transpile` on PATH
  2. prebuilt under $PROCTOR_CACHE_DIR/c2rust/{bin,lib} (see tests/e2e/README.md)
  3. built from the stages/c2rust submodule (needs clang/LLVM dev libs)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path
from typing import Any, NoReturn

STAGE_ID = "c2rust"
STAGE_VERSION = "0.2.0"
SCHEMA_VERSION = 1
REPAIR_TIMEOUT_S = 900


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
    input_data: bytes | None = None,
) -> None:
    with log_file.open("ab") as log:
        log.write(f"$ {' '.join(command)}\n".encode())
        log.flush()
        result = subprocess.run(
            command, cwd=cwd, env=env, input=input_data, stdout=log, stderr=log
        )
    if result.returncode != 0:
        raise command_failure(command, result.returncode, log_file)


def command_failure(
    command: list[str], returncode: int, log_file: Path
) -> StageFailure:
    tail = log_file.read_text(encoding="utf-8", errors="replace")[-3000:]
    return StageFailure(
        f"command failed ({returncode}): {' '.join(command)}\n...{tail}"
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


@dataclass(frozen=True)
class TargetSelection:
    target_name: str
    target_kind: str  # "executable" | "library"
    bin_name: str | None
    extra_libs: list[str]


def _is_library_case(item: str | None, c_project: Path | None) -> bool:
    """TRACTOR marks library cases with a '_lib' suffix or a runner/ directory."""
    if item and item.endswith("_lib"):
        return True
    if c_project:
        for p in (c_project, c_project.parent):
            if (p / "runner").is_dir():
                return True
    return False


def _find_runner_library(c_project: Path | None) -> str | None:
    """Read expected library from runner/src/main.rs (library: "<name>")."""
    if not c_project:
        return None
    for candidate in (
        c_project / "runner" / "src" / "main.rs",
        c_project.parent / "runner" / "src" / "main.rs",
    ):
        if candidate.is_file():
            content = candidate.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'library:\s*"([^"]+)"', content)
            if m:
                return m.group(1)
    return None


def _resolve_intrinsic(
    executables: list[Artifact], libraries: list[Artifact]
) -> TargetSelection | None:
    """Layer 1: Resolve if project files unambiguously build exactly one artifact."""
    if len(executables) == 1 and len(libraries) == 0:
        return TargetSelection(
            target_name=executables[0].name,
            target_kind="executable",
            bin_name=executables[0].name,
            extra_libs=[],
        )
    if len(libraries) == 1 and len(executables) == 0:
        return TargetSelection(
            target_name=libraries[0].name,
            target_kind="library",
            bin_name=None,
            extra_libs=[],
        )
    return None


def _resolve_from_config(
    targets: list[Artifact],
    libraries: list[Artifact],
    config: dict[str, Any],
) -> TargetSelection | None:
    """Layer 2a: Resolve using explicit configuration overrides."""
    cfg_name = config.get("target_name")
    if not (isinstance(cfg_name, str) and cfg_name):
        return None
    matching = next((t for t in targets if t.name == cfg_name), None)
    if not matching:
        return None

    cfg_kind = config.get("target_kind")
    kind = cfg_kind or (
        "executable" if matching.artifact_type == "EXECUTABLE" else "library"
    )
    bin_name = matching.name if kind == "executable" else None
    extra_libs = [t.name for t in libraries if t.name != matching.name]
    return TargetSelection(
        target_name=matching.name,
        target_kind=kind,
        bin_name=bin_name,
        extra_libs=extra_libs,
    )


def _match_by_name(candidates: list[Artifact], item: str | None) -> str | None:
    """Match candidate targets against benchmark item name (exact or token match)."""
    if not item:
        return None
    # Exact match (e.g. bin2hex_lib)
    exact = next((t.name for t in candidates if t.name == item), None)
    if exact is not None:
        return exact
    # Word-boundary or token match (e.g. 'blake' in '053_blake_...')
    matching = [
        t.name
        for t in candidates
        if re.search(rf"\b{re.escape(t.name)}\b", item) or f"_{t.name}_" in f"_{item}_"
    ]
    return max(matching, key=len) if matching else None


def _disambiguate_library_target(
    libraries: list[Artifact],
    item: str | None,
    c_project: Path | None,
) -> str | None:
    """Disambiguate primary library target among multiple shared libraries."""
    # 1. Runner declaration
    runner_lib = _find_runner_library(c_project)
    if runner_lib:
        matching = next((t.name for t in libraries if t.name == runner_lib), None)
        if matching is not None:
            return matching

    # 2. Item name match
    name_match = _match_by_name(libraries, item)
    if name_match is not None:
        return name_match

    # 3. Single library among hybrid targets
    if len(libraries) == 1:
        return libraries[0].name

    return None


def _disambiguate_executable_target(
    executables: list[Artifact],
    item: str | None,
) -> str | None:
    """Disambiguate primary executable target among executable candidates."""
    if len(executables) == 1:
        return executables[0].name
    return _match_by_name(executables, item)


def _resolve_by_conventions(
    executables: list[Artifact],
    libraries: list[Artifact],
    item: str | None,
    c_project: Path | None,
) -> TargetSelection | None:
    """Layer 2b: Resolve multi-target projects using benchmark intent conventions."""
    is_lib = _is_library_case(item, c_project)
    if not is_lib and len(executables) == 0 and len(libraries) > 0:
        is_lib = True

    if is_lib:
        if not libraries:
            raise StageFailure(
                f"Benchmark '{item or 'unknown'}' indicates a library case, "
                f"but project builds only executables: {[t.name for t in executables]}"
            )
        target_name = _disambiguate_library_target(libraries, item, c_project)
        if target_name:
            extra_libs = [t.name for t in libraries if t.name != target_name]
            return TargetSelection(
                target_name=target_name,
                target_kind="library",
                bin_name=None,
                extra_libs=extra_libs,
            )
    else:
        if not executables:
            raise StageFailure(
                f"Benchmark '{item or 'unknown'}' indicates an executable case, "
                f"but project builds only libraries: {[t.name for t in libraries]}"
            )
        target_name = _disambiguate_executable_target(executables, item)
        if target_name:
            extra_libs = [t.name for t in libraries]
            return TargetSelection(
                target_name=target_name,
                target_kind="executable",
                bin_name=target_name,
                extra_libs=extra_libs,
            )

    return None


def _fail_ambiguous(targets: list[Artifact], item: str | None) -> NoReturn:
    """Layer 3: Fail fast when ambiguous targets cannot be resolved."""
    target_descriptions = [f"{t.name} ({t.artifact_type})" for t in targets]
    raise StageFailure(
        f"Ambiguous targets in C project: {', '.join(target_descriptions)}. "
        f"Could not disambiguate target under test using config, runner, or naming conventions "
        f"for case '{item or 'unknown'}'."
    )


def select_target(
    targets: list[Artifact],
    item: str | None = None,
    c_project: Path | None = None,
    config: dict[str, Any] | None = None,
) -> TargetSelection:
    """Select the primary target to transpile and categorize target kind.

    Layer 1 (Project Files Alone):
      If the C project unambiguously builds only one executable (and 0 libraries)
      or only one library (and 0 executables), resolve immediately without consulting
      any external conventions or metadata.

    Layer 2 (External Disambiguation):
      If the C project is multi-target (e.g. builds both executables and shared libraries,
      or multiple libraries), disambiguate the intended benchmark target using:
        1. Explicit configuration (config["target_name"] / config["target_kind"])
        2. Benchmark intent conventions:
           - Directory suffix '_lib' or presence of runner/ package -> library
           - Runner package specification (runner/src/main.rs: library: "<name>")
           - Item name token matching (e.g. 'blake' in '053_blake_...')
           - Binary intent when no '_lib' suffix and executables exist

    Layer 3 (Fail Fast):
      If targets remain ambiguous and cannot be resolved, raise StageFailure.
    """
    if not targets:
        raise StageFailure("cmake codemodel reports no executable or library targets")

    executables = [t for t in targets if t.artifact_type == "EXECUTABLE"]
    libraries = [t for t in targets if t.artifact_type == "SHARED_LIBRARY"]

    # Layer 1: Intrinsic project files (unambiguous)
    selection = _resolve_intrinsic(executables, libraries)
    if selection is not None:
        return selection

    # Layer 2: External disambiguation (multi-target / hybrid)
    if config:
        selection = _resolve_from_config(targets, libraries, config)
        if selection is not None:
            return selection

    selection = _resolve_by_conventions(executables, libraries, item, c_project)
    if selection is not None:
        return selection

    # Layer 3: Fail fast
    _fail_ambiguous(targets, item)


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


def restore_c2rust_module_sources(rust_dir: Path) -> None:
    lib_rs = rust_dir / "lib.rs"
    if not lib_rs.is_file():
        return

    lines = lib_rs.read_text(encoding="utf-8").splitlines(keepends=True)
    existing_lines = {line.strip() for line in lines}
    output: list[str] = []
    module_path = Path()
    for line in lines:
        output.append(line)
        entry = line.strip()
        if entry.startswith("} // mod "):
            module_path = module_path.parent
            continue
        # C2Rust emits inline modules for directories, even when a same-name file exists.
        module = re.fullmatch(r"pub mod (?:r#)?(\w+) \{", entry)
        if module is None:
            continue
        module_path /= module[1]
        directory = rust_dir / module_path
        source = directory.with_suffix(".rs")
        if not directory.is_dir() or not source.is_file():
            continue
        if f'include!("{source.relative_to(rust_dir).as_posix()}");' in existing_lines:
            continue

        renamed = source.with_stem(f"{source.stem}_c2rust")
        suffix = 2
        while renamed.exists() or renamed.with_suffix("").exists():
            renamed = source.with_stem(f"{source.stem}_c2rust_{suffix}")
            suffix += 1
        source.rename(renamed)
        # Declare the renamed file beside the inline module so they have separate namespaces.
        output.insert(len(output) - 1, f"pub mod {renamed.stem};\n")

    if output != lines:
        lib_rs.write_text("".join(output), encoding="utf-8")


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


def translated_rust_source(relative: Path) -> bool:
    return relative.suffix == ".rs" and relative != Path("build.rs")


def cargo_build_errors(project: Path, log_file: Path, env: dict[str, str]) -> list[str]:
    command = ["cargo", "build", "--message-format=json"]
    errors: list[str] = []
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"$ {shlex.join(command)}\n")
        log.flush()
        result = subprocess.run(
            command,
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=log,
            encoding="utf-8",
            errors="replace",
        )
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.write(line + "\n")
                continue
            if not isinstance(event, dict) or event.get("reason") != "compiler-message":
                continue
            diagnostic = event["message"]
            rendered = diagnostic.get("rendered") or diagnostic["message"] + "\n"
            log.write(rendered)
            if diagnostic["level"] != "error":
                continue
            target = event["target"]
            if "custom-build" in target["kind"] or not Path(
                target["src_path"]
            ).resolve().is_relative_to(project):
                continue
            primary_sources = (
                (project / span["file_name"]).resolve()
                for span in diagnostic["spans"]
                if span["is_primary"]
            )
            if any(
                path.is_relative_to(project)
                and translated_rust_source(path.relative_to(project))
                and path.is_file()
                for path in primary_sources
            ):
                errors.append(rendered)
    if result.returncode == 0:
        return []
    if result.returncode < 0 or not errors:
        raise command_failure(command, result.returncode, log_file)
    return errors


def repair_workspace_state(project: Path) -> dict[Path, tuple[int, bytes | None]]:
    if project.is_symlink() or not project.is_dir():
        raise StageFailure("compilation repair workspace must be a directory")
    state: dict[Path, tuple[int, bytes | None]] = {
        Path(): (project.stat().st_mode, None),
    }
    for path in project.rglob("*"):
        relative = path.relative_to(project)
        info = path.lstat()
        contents = None
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if not translated_rust_source(relative):
                with path.open("rb") as stream:
                    contents = file_digest(stream, "sha256").digest()
        elif not stat.S_ISDIR(info.st_mode):
            raise StageFailure(
                f"unsupported compilation repair workspace entry: {path}"
            )
        state[relative] = (info.st_mode, contents)
    return state


def build_with_repair(
    project: Path, log_file: Path, env: dict[str, str], model: str
) -> int:
    project = project.resolve()
    errors = cargo_build_errors(project, log_file, env)
    if not errors:
        return 0

    timeout_s = float(REPAIR_TIMEOUT_S)
    if (deadline := os.environ.get("PROCTOR_STAGE_DEADLINE_MONOTONIC")) is not None:
        timeout_s = min(timeout_s, float(deadline) - time.monotonic() - 5)
    if timeout_s <= 0:
        raise StageFailure(f"stage deadline leaves no time for repair. See {log_file}")

    before = repair_workspace_state(project)
    agent_project = Path(__file__).resolve().parent.parent / "crat-repair"
    request = {
        "agent": "opencode",
        "model": model,
        "workspace": str(project),
        "timeout_s": timeout_s,
        "prompt": (
            "Make the smallest edits to the existing generated Rust source needed "
            "to resolve the reported compiler errors. Preserve program behavior, "
            "the C-facing ABI, and all build and Crat configuration. Keep "
            "all translated implementations and compilation checks enabled.\n\n"
            + "\n".join(errors)
        ),
    }
    with tempfile.TemporaryDirectory(
        prefix="repair-target-", dir=Path(env["CARGO_TARGET_DIR"]).parent
    ) as agent_target:
        run_logged(
            [
                "uv",
                "run",
                "--project",
                str(agent_project),
                "python3",
                str(agent_project / "repair_agent.py"),
            ],
            log_file,
            cwd=project,
            env={
                **env,
                "CARGO_TARGET_DIR": agent_target,
                "CARGO_BUILD_BUILD_DIR": agent_target,
            },
            input_data=json.dumps(request).encode("utf-8"),
        )
    after = repair_workspace_state(project)
    changed = sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )
    if changed:
        paths = ", ".join(map(str, changed))
        raise StageFailure(
            f"compilation repair changed protected project paths: {paths}"
        )
    run_logged(["cargo", "build"], log_file, cwd=project, env=env)
    return 1


def run_stage(envelope: dict[str, Any]) -> dict[str, Any]:
    config = envelope.get("config", {})
    cache_variables = config.get("cmake_cache_variables", {})
    if not isinstance(cache_variables, dict):
        raise StageFailure("cmake_cache_variables must be a table")
    cache_flags = []
    for name, value in cache_variables.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise StageFailure(f"invalid CMake cache variable name: {name!r}")
        if not isinstance(value, (str, int, bool)):
            raise StageFailure(
                f"CMake cache variable {name} must be a string, integer, or boolean"
            )
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        cache_flags.append(f"-D{name}={value}")
    repair_model = config.get("repair_model")
    if repair_model is not None and (
        not isinstance(repair_model, str) or not repair_model.strip()
    ):
        raise StageFailure("repair_model must be a nonempty string")
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
            *cache_flags,
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

    selection = select_target(
        targets,
        item=envelope.get("item"),
        c_project=Path(c_project),
        config=config,
    )
    target_name = selection.target_name
    bin_name = selection.bin_name
    extra_libs = selection.extra_libs

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

    # 5. project fix-ups
    restore_c2rust_module_sources(rust_dir)
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
    repair_calls = 0
    if config.get("check_build", True):
        started = time.monotonic()
        build_env = {**os.environ, "RUSTFLAGS": "-Awarnings"}
        target_dir = rust_dir / "target"
        if repair_model is None:
            run_logged(["cargo", "build"], log_file, cwd=rust_dir, env=build_env)
        else:
            target_dir = work / "cargo-target"
            build_env["CARGO_TARGET_DIR"] = str(target_dir.resolve())
            build_env["CARGO_BUILD_BUILD_DIR"] = build_env["CARGO_TARGET_DIR"]
            repair_calls = build_with_repair(
                rust_dir, log_file, build_env, repair_model
            )
        shutil.rmtree(target_dir, ignore_errors=True)
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
            **({"cmake_cache_variables": cache_variables} if cache_variables else {}),
            **({"repair_model": repair_model} if repair_model is not None else {}),
        },
        "metrics": {
            "transpiler": str(transpiler),
            "targets": len(targets),
            "target_name": target_name,
            "target_kind": selection.target_kind,
            "c_sources": len(filtered),
            "exposed_fns": len(exposed),
            "compilation_repair_calls": repair_calls,
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
