#!/usr/bin/env python3
from enum import StrEnum, auto

import shutil
import sys
import tarfile
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from translate import translate
from transform import transform
from typing import Any
from utils import (
    copy_translated_rust,
    dump_json,
    dump_toml,
    get_name_without_suffix,
    load_json,
    load_toml,
    should_show_help,
    print_help,
    run,
    unique,
    ParamVal,
    Parameters,
)


class Mode(StrEnum):
    C2RUST = auto()
    C2RUST_CFIX = auto()
    C2RUST_CRAT = auto()
    C2RUST_CRAT_CFIX = auto()

    def uses_cfix(self) -> bool:
        return self in [Mode.C2RUST_CFIX, Mode.C2RUST_CRAT_CFIX]

    def uses_crat(self) -> bool:
        return self in [Mode.C2RUST_CRAT, Mode.C2RUST_CRAT_CFIX]


@dataclass(frozen=True)
class ConfigInfo:
    config_vars: dict[str, list[ParamVal]]
    presets: list[tuple[str, dict[str, str]]]


def _extract_config_vars(archive_file: Path) -> ConfigInfo | None:
    temp_dir = Path(
        tempfile.mkdtemp(prefix="tmp-", dir=tempfile.gettempdir())
    ).resolve()
    with tarfile.open(archive_file) as tar:
        tar.extractall(temp_dir)
    test_case_dir = temp_dir / "test_case"
    if not test_case_dir.exists():
        test_case_dir = temp_dir
    config_file = test_case_dir / "configuration.json"
    if not config_file.exists():
        return

    config_vars: dict[str, list[ParamVal]] = load_json(config_file)[
        "configurable_variables"
    ]

    preset_file = temp_dir / "CMakePresets.json"
    presets = []
    if preset_file.exists():
        preset_data = load_json(preset_file)
        build_presets: list[dict[str, str]] = preset_data["buildPresets"]
        configure_presets: list[dict[str, Any]] = preset_data["configurePresets"]

        def extract_cache_variable(
            build_preset: dict[str, str],
        ) -> tuple[str, dict[str, str]]:
            name = build_preset["name"]
            configure_preset_name = build_preset["configurePreset"]
            configure_preset = next(
                preset
                for preset in configure_presets
                if preset["name"] == configure_preset_name
            )
            return (name, configure_preset["cacheVariables"])

        presets = [
            extract_cache_variable(build_preset) for build_preset in build_presets
        ]

    shutil.rmtree(temp_dir)
    return ConfigInfo(config_vars, presets)


def _build_parameter_sets(config_vars: dict[str, list[ParamVal]]) -> list[Parameters]:
    parameter_sets: list[Parameters] = [[]]
    for name, values in config_vars.items():
        parameter_sets = [
            [*current, (name, value)] for current in parameter_sets for value in values
        ]
    return parameter_sets


FIX_FLAGS: list[str] = [
    "--workspace",
    "--all-targets",
    "--allow-no-vcs",
    "--allow-dirty",
]


def _apply_fix(rust_dir: Path) -> None:
    stdout_log = rust_dir / "stdout.log"
    stderr_log = rust_dir / "stderr.log"

    for _ in range(10):
        command = ["cargo", "fix"]
        command.extend(FIX_FLAGS)
        run(command, stdout_log=stdout_log, stderr_log=stderr_log, cwd=rust_dir)
        command = ["cargo", "check", "--workspace", "--all-targets"]
        result = run(
            command, stdout_log=stdout_log, stderr_log=stderr_log, cwd=rust_dir
        )
        if "run `cargo fix" not in result.stderr.decode(errors="replace"):
            break

    for _ in range(10):
        command = ["cargo", "clippy", "--fix"]
        command.extend(FIX_FLAGS)
        result = run(
            command, stdout_log=stdout_log, stderr_log=stderr_log, cwd=rust_dir
        )
        if "run `cargo clippy --fix" not in result.stderr.decode(errors="replace"):
            break


def _format(
    rust_dir: Path, stdout_log: Path | None = None, stderr_log: Path | None = None
) -> None:
    stdout_log = stdout_log if stdout_log else rust_dir / "stdout.log"
    stderr_log = stderr_log if stderr_log else rust_dir / "stderr.log"
    command = ["cargo", "fmt"]
    run(command, stdout_log=stdout_log, stderr_log=stderr_log, cwd=rust_dir)


def _translate_and_transform(
    workspace: Path, archive_file: Path, mode: Mode
) -> tuple[bool, Path]:
    tc_name = get_name_without_suffix(archive_file)
    final_dir = workspace / "c2rust" / tc_name
    try:
        is_final = not mode.uses_crat()
        translate(archive_file, workspace / "c2rust" / tc_name, is_final=is_final)
        if mode.uses_crat():
            transform(workspace, tc_name)
            final_dir = workspace / "bin" / tc_name
        if mode.uses_cfix():
            _apply_fix(final_dir)
        _format(final_dir)
        return True, final_dir
    except Exception as e:
        print(f"Exception: {e}")
        return False, final_dir


def _translate_and_transform_with_parameters(
    arg: tuple[Path, Path, Parameters, Mode],
) -> tuple[bool, Parameters, Path]:
    workspace, archive_file, parameters, mode = arg
    tc_name = get_name_without_suffix(archive_file)
    name = "_".join([str(value) for _, value in parameters])
    final_dir = workspace / name / "c2rust" / tc_name
    try:
        is_final = not mode.uses_crat()
        translate(
            archive_file,
            workspace / name / "c2rust" / tc_name,
            parameters,
            is_final=is_final,
        )
        if mode.uses_crat():
            transform(workspace / name, tc_name)
            final_dir = workspace / name / "bin" / tc_name
        if mode.uses_cfix():
            _apply_fix(final_dir)
        return (True, parameters, final_dir)
    except:
        return (False, parameters, final_dir)


def _combine_logs(log_paths: list[Path], destination: Path) -> None:
    with destination.open("a", encoding="utf-8") as out:
        for path in log_paths:
            if path.exists():
                content = path.read_text(encoding="utf-8")
                out.write(content)
                if content and not content.endswith("\n"):
                    out.write("\n")
            out.write("\n")


def orchestrate(archive_file: Path, dst_dir: Path, mode: Mode) -> None:
    tc_name = get_name_without_suffix(archive_file)
    workspace = Path(
        tempfile.mkdtemp(prefix="tmp-", dir=tempfile.gettempdir())
    ).resolve()

    try:
        config_info = _extract_config_vars(archive_file)
        if config_info:
            parameter_sets = _build_parameter_sets(config_info.config_vars)
            args = [
                (workspace, archive_file, parameters, mode)
                for parameters in parameter_sets
            ]
            successes: list[tuple[Parameters, Path]] = []
            failures: list[Parameters] = []
            with ProcessPoolExecutor() as executor:
                for success, parameters, final_dir in executor.map(
                    _translate_and_transform_with_parameters, args
                ):
                    if success:
                        successes.append((parameters, final_dir))
                    else:
                        failures.append(parameters)

            if failures:
                print("Failure parameters:")
                for parameters in failures:
                    print(parameters)
                raise

            json_file = workspace / f"translations.json"
            dump_json(
                [
                    {
                        "dir": str(final_dir),
                        **{name: value for name, value in parameters},
                    }
                    for parameters, final_dir in successes
                ],
                json_file,
            )

            stdout_logs = [final_dir / "stdout.log" for _, final_dir in successes]
            stderr_logs = [final_dir / "stderr.log" for _, final_dir in successes]
            stdout_log = workspace / "stdout.log"
            stderr_log = workspace / "stderr.log"
            _combine_logs(stdout_logs, stdout_log)
            _combine_logs(stderr_logs, stderr_log)

            command = ["crat-merge", str(json_file), str(workspace)]
            run(command, stdout_log=stdout_log, stderr_log=stderr_log)

            def make_feature(name: str, val: str) -> str:
                if val == "ON":
                    return name
                else:
                    return f"{name}_{val}"

            final_dir = workspace / tc_name
            cargo_toml_path = final_dir / "Cargo.toml"
            cargo_toml_data = load_toml(cargo_toml_path)
            cargo_toml_features = cargo_toml_data["features"]
            for name, parameters in config_info.presets:
                features = [
                    make_feature(name, val)
                    for name, val in parameters.items()
                    if val != "OFF"
                ]
                cargo_toml_features[f"{name}_config"] = features
                cargo_toml_features[f"default"] = features
            dump_toml(cargo_toml_data, cargo_toml_path)

            libs = unique(
                [
                    lib
                    for _, final_dir in successes
                    if (final_dir / "libs.json").exists()
                    for lib in load_json(final_dir / "libs.json")
                ]
            )
            dump_json(libs, final_dir / "libs.json")
            _format(final_dir, stdout_log=stdout_log, stderr_log=stderr_log)
            copy_translated_rust(final_dir, dst_dir)
            shutil.copy2(stdout_log, dst_dir)
            shutil.copy2(stderr_log, dst_dir)

        else:
            ok, final_dir = _translate_and_transform(workspace, archive_file, mode)
            if ok:
                copy_translated_rust(final_dir, dst_dir)
            else:
                print(f"Run failed: {archive_file=} {dst_dir=} {mode=}")

    finally:
        shutil.rmtree(workspace)


def _usage() -> str:
    return (
        f"Usage: {sys.argv[0]} <test_case_tarball> <dst_dir> [<mode>]\n"
        f"Available modes: {[mode.value for mode in Mode]}\n"
        f"Default mode: {Mode.C2RUST_CRAT_CFIX}"
    )


def main() -> None:
    if should_show_help(sys.argv):
        print_help(_usage())
        sys.exit(0)

    if len(sys.argv) < 3:
        print_help(_usage())
        sys.exit(1)

    archive_file = Path(sys.argv[1])
    dst_dir = Path(sys.argv[2])
    mode = Mode(sys.argv[3]) if len(sys.argv) == 4 else Mode.C2RUST_CRAT_CFIX
    orchestrate(archive_file, dst_dir, mode)


if __name__ == "__main__":
    main()
