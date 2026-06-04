#!/usr/bin/env python3

import os
import shlex
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from utils import (
    dump_json,
    dump_toml,
    get_name_without_suffix,
    load_json,
    load_toml,
    print_help,
    run,
    should_show_help,
    unique,
    ParamVal,
    Parameters,
)


@dataclass(frozen=True)
class Artifact:
    name: str
    artifact_type: str
    sources: list[Path]
    link_args: list[str]


def _get_target(build_dir: Path) -> list[Artifact]:
    cmake_reply_dir = build_dir / ".cmake" / "api" / "v1" / "reply"

    index_path = next(cmake_reply_dir.glob("index-*.json"))
    index = load_json(index_path)

    codemodel_filename = index["reply"]["codemodel-v2"]["jsonFile"]
    codemodel_path = cmake_reply_dir / codemodel_filename
    codemodel = load_json(codemodel_path)
    source_dir = Path(codemodel["paths"]["source"])

    target_entries = codemodel["configurations"][0]["targets"]
    targets = {
        entry["id"]: load_json(cmake_reply_dir / entry["jsonFile"])
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
            dependency_id = dependency["id"]
            if dependency_id in targets:
                sources.extend(resolve_sources(dependency_id))
        resolved = unique(sources)
        cache[target_id] = resolved
        return resolved

    def resolve_link_args(target_id: str) -> list[str]:
        target = targets[target_id]
        fragments = [
            fragment["fragment"]
            for fragment in target.get("link", {}).get("commandFragments", [])
            if fragment.get("role") == "libraries"
            and fragment["fragment"].startswith("-l")
        ]
        return unique(fragments)

    artifacts: list[Artifact] = []
    for target in targets.values():
        artifact_name = str(target["name"])
        artifact_type = str(target["type"])
        if "sphincs_core" == artifact_name:
            continue
        if artifact_type not in {"EXECUTABLE", "SHARED_LIBRARY"}:
            continue
        artifacts.append(
            Artifact(
                artifact_name,
                artifact_type,
                resolve_sources(target["id"]),
                resolve_link_args(target["id"]),
            )
        )
    return artifacts


def _add_link_args_to_build_rs(build_rs: Path, link_args: list[str]) -> None:
    if not link_args:
        return
    lines = build_rs.read_text(encoding="utf-8").splitlines(keepends=True)
    insert_at = next(
        index + 1 for index, line in enumerate(lines) if line.strip() == "fn main() {"
    )
    for link_arg in reversed(link_args):
        lines.insert(insert_at, f'    println!("cargo:rustc-link-arg={link_arg}");\n')
    build_rs.write_text("".join(lines), encoding="utf-8")


def _get_exposed_fns(
    compile_commands: list[dict[str, object]], source_dir: Path
) -> list[str]:
    from clang.cindex import Cursor, CursorKind, Index

    def preserve_option(option: str) -> bool:
        return (
            option.startswith("-D")
            or option.startswith("-I")
            or option.startswith("-std=")
            or option.startswith("-m")
        )

    def command_args(command: dict[str, object]) -> list[str]:
        arguments = command.get("arguments")
        if isinstance(arguments, list):
            values = [str(value) for value in arguments[1:]]
        else:
            values = shlex.split(str(command["command"]))[1:]
        return [value for value in values if preserve_option(value)]

    parse_args = [
        "-x",
        "c-header",
        *unique([arg for command in compile_commands for arg in command_args(command)]),
    ]
    names: set[str] = set()

    def visit_header_functions(node: Cursor) -> None:
        if node.kind == CursorKind.FUNCTION_DECL and node.location.file is not None:
            decl_file = Path(node.location.file.name).resolve()
            if decl_file.is_relative_to(source_dir) and decl_file.suffix == ".h":
                names.add(node.spelling)
        for child in node.get_children():
            visit_header_functions(child)

    index = Index.create()
    for header in sorted(source_dir.rglob("*.h")):
        translation_unit = index.parse(str(header), args=parse_args)
        visit_header_functions(translation_unit.cursor)
    return sorted(names)


def translate(
    archive_file: Path,
    dst_dir: Path,
    parameters: Parameters | None = None,
    is_final: bool = False,
) -> None:
    tc_name = get_name_without_suffix(archive_file)
    temp_dir = Path(
        tempfile.mkdtemp(prefix="tmp-", suffix=f"-{tc_name}", dir=tempfile.gettempdir())
    ).resolve()

    stdout_log = temp_dir / "stdout.log"
    stderr_log = temp_dir / "stderr.log"

    try:
        workspace = temp_dir / tc_name
        source_dir = workspace / "c"
        source_dir.mkdir(parents=True)
        with tarfile.open(archive_file) as tar:
            tar.extractall(source_dir)

        build_dir = source_dir / "build"
        query_dir = build_dir / ".cmake" / "api" / "v1" / "query" / "codemodel-v2"
        preset_flag = (
            ["--preset", "test"]
            if not parameters and (source_dir / "CMakePresets.json").exists()
            else []
        )

        def flag_value(value: ParamVal) -> str:
            if value is True:
                return "ON"
            elif value is False:
                return "OFF"
            else:
                return str(value)

        parameter_flags = (
            [f"-D{name}={flag_value(value)}" for name, value in parameters]
            if parameters
            else []
        )
        query_dir.mkdir(parents=True)
        command = [
            "cmake",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=1",
            "-S",
            str(source_dir),
            "-B",
            str(build_dir),
            "-G",
            "Ninja",
            *preset_flag,
            *parameter_flags,
        ]
        run(command, stdout_log=stdout_log, stderr_log=stderr_log)

        commands_file = build_dir / "compile_commands.json"
        compile_commands = load_json(commands_file)
        exposed_fns = _get_exposed_fns(compile_commands, source_dir)

        targets = _get_target(build_dir)
        sources = set([source for target in targets for source in target.sources])
        filtered_commands = [
            command
            for command in compile_commands
            if Path(str(command["file"])).resolve() in sources
        ]
        dump_json(filtered_commands, commands_file)

        bin_name = next(
            (target.name for target in targets if target.artifact_type == "EXECUTABLE"),
            None,
        )
        target_name = bin_name or targets[0].name
        shared_library_names = [
            target.name
            for target in targets
            if target.artifact_type == "SHARED_LIBRARY"
        ]
        extra_lib_names = (
            shared_library_names
            if bin_name
            else [name for name in shared_library_names if name != target_name]
        )
        rust_dir = temp_dir / "rust" / target_name
        if rust_dir.exists():
            shutil.rmtree(rust_dir)
        rust_dir.mkdir(parents=True)
        command = [
            "c2rust-transpile",
            "-o",
            str(rust_dir),
            "-e",
            str(commands_file),
        ]

        is_final_bin = is_final and bin_name is not None
        if is_final_bin:
            bin_file = "main"
            if "P01_sphincs_plus" in str(archive_file):
                bin_file = "PQCgenKAT_sign"
            command.extend(["--binary", bin_file])

        run(command, stdout_log=stdout_log, stderr_log=stderr_log)

        link_args = sorted(set([arg for target in targets for arg in target.link_args]))
        _add_link_args_to_build_rs(rust_dir / "build.rs", link_args)

        cargo_toml_path = rust_dir / "Cargo.toml"
        cargo_toml = load_toml(cargo_toml_path)
        if is_final_bin:
            cargo_toml["bin"][0]["name"] = bin_name
        else:
            cargo_toml["lib"]["crate-type"].append("cdylib")
        dump_toml(cargo_toml, cargo_toml_path)

        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(rust_dir, dst_dir)

        if "CHECK_BUILD" in os.environ:
            command = ["cargo", "build"]
            env = {
                **dict(os.environ),
                "RUSTFLAGS": "-Awarnings",
            }
            run(
                command,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                cwd=dst_dir,
                env=env,
            )
            shutil.rmtree(dst_dir / "target")

        config_file = dst_dir / "config.toml"
        config_data: dict[str, object] = {"c_exposed_fns": exposed_fns}
        if bin_name:
            config_data["bin"] = {"name": bin_name}
        dump_toml(config_data, config_file)
        dump_json(extra_lib_names, dst_dir / "libs.json")

    finally:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(stdout_log, dst_dir)
        shutil.copy(stderr_log, dst_dir)
        shutil.rmtree(temp_dir)


def translate_tc(tc_dir: Path) -> None:
    project_dir = Path(__file__).resolve().parent.parent

    tc_dir = tc_dir.resolve()
    tc_name = tc_dir.name
    tc_p_dir_name = tc_dir.parent.name
    tc_pp_dir_name = tc_dir.parent.parent.name

    archive_file = (
        project_dir / "bundles" / tc_pp_dir_name / tc_p_dir_name / f"{tc_name}.tar.gz"
    )
    dst_dir = (
        project_dir / "c2rust-translated" / tc_pp_dir_name / tc_p_dir_name / tc_name
    )
    translate(archive_file, dst_dir)


def _usage() -> str:
    return f"Usage: {sys.argv[0]} <test_case_dir>"


def main() -> None:
    if should_show_help(sys.argv):
        print_help(
            _usage(),
            f"Example: {sys.argv[0]} Test-Corpus/Public-Tests/B01_organic/bin2hex_lib",
        )
        sys.exit(0)

    if len(sys.argv) != 2:
        print_help(
            _usage(),
            f"Example: {sys.argv[0]} Test-Corpus/Public-Tests/B01_organic/bin2hex_lib",
        )
        sys.exit(1)

    tc_dir = Path(sys.argv[1])
    translate_tc(tc_dir)


if __name__ == "__main__":
    main()
