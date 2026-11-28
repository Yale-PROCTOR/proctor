#!/usr/bin/env python3
"""Translate a TRACTOR C project under its build configurations and merge the results."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tomli_w

from proctor.config.load import ConfigError, deep_merge, load_config
from proctor.config.model import PipelineConfig, StageEntry
from proctor.orchestrator.run import RunError, make_run_id, start_run
from proctor.orchestrator.validate import validate_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent
type BuildValue = str | int | bool
type BuildParameters = dict[str, BuildValue]


def cmake_source(project: Path) -> Path:
    if (project / "CMakeLists.txt").is_file():
        return project
    return project / "test_case"


def prepare_project(source: Path, work: Path) -> tuple[Path, str]:
    case_name = source.name
    if source.is_dir():
        project = source.resolve()
        if case_name == "test_case":
            case_name = source.parent.name
    else:
        case_name = re.sub(
            r"\.(?:tar(?:\.(?:gz|bz2|xz))?|tgz|tbz2|txz)$", "", case_name
        )
        extracted = work / "extracted"
        with tarfile.open(source) as archive:
            archive.extractall(extracted, filter="data")
        project = extracted
        if not (cmake_source(project) / "CMakeLists.txt").is_file():
            candidates = [
                path
                for path in extracted.iterdir()
                if path.is_dir() and (cmake_source(path) / "CMakeLists.txt").is_file()
            ]
            if len(candidates) == 1:
                project = candidates[0]
    if not (cmake_source(project) / "CMakeLists.txt").is_file():
        raise RunError(f"{source} must contain one CMake project")

    # Some corpus cases share test_case/ via a sibling-relative symlink.
    if (project / "test_case").is_symlink():
        snapshot = work / "project"
        shutil.copytree(project, snapshot, symlinks=True)
        (snapshot / "test_case").unlink()
        shutil.copytree(project / "test_case", snapshot / "test_case", symlinks=True)
        project = snapshot
    return project, case_name


def read_parameters(project: Path) -> dict[str, list[BuildValue]]:
    path = project / "test_case" / "configuration.json"
    if not path.exists():
        path = project / "configuration.json"
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    variables = (
        document.get("configurable_variables") if isinstance(document, dict) else None
    )
    if not isinstance(variables, dict):
        raise ConfigError(f"{path} must contain a configurable_variables object")
    for name, values in variables.items():
        if name == "dir" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ConfigError(f"{path} has invalid build parameter name {name!r}")
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, (str, int, bool)) for value in values)
        ):
            raise ConfigError(
                f"{path} must list string, integer, or boolean values for {name}"
            )
        if len({str(value) for value in values}) != len(values):
            raise ConfigError(f"{path} lists duplicate values for {name}")
    return variables


def combinations(
    parameters: dict[str, list[BuildValue]],
) -> tuple[list[BuildParameters], bool]:
    ignore_boolean = math.prod(map(len, parameters.values())) > 32
    domains = dict(parameters)
    if ignore_boolean:
        for name, values in parameters.items():
            if all(isinstance(value, bool) for value in values):
                if True not in values:
                    raise ConfigError(f"build parameter {name} does not allow true")
                domains[name] = [True]
    selected = [dict(zip(domains, row)) for row in itertools.product(*domains.values())]
    return selected, ignore_boolean


def copy_merge_input(project: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        excluded = {"target", ".git"}
        if Path(directory) == project:
            excluded.update(
                (
                    ".scripts",
                    "tests",
                    "runner",
                    "test_vectors",
                    "testinfra.toml",
                    "proctor.toml",
                )
            )
        return excluded.intersection(names)

    shutil.copytree(project, destination, symlinks=True, ignore=ignore)


def run_merge(translations: Path, destination: Path, *, ignore_boolean: bool) -> None:
    crat = REPO_ROOT / "stages" / "crat"
    host = subprocess.check_output(
        ["rustc", "--print", "host-tuple"], cwd=crat, text=True
    ).strip()
    deps = crat / "deps_crate"
    if not (deps / "target" / host / "debug" / "deps").is_dir():
        subprocess.run(["cargo", "build", "--target", host], cwd=deps, check=True)
    command = ["cargo", "run", "--release", "--bin", "crat-merge", "--"]
    if ignore_boolean:
        command.append("--ignore-boolean")
    subprocess.run(
        [*command, str(translations), str(destination)], cwd=crat, check=True
    )


def merge_variants(
    crates: list[Path],
    configurations: list[BuildParameters],
    publication: Path,
    *,
    ignore_boolean: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix="attempt-", dir=publication) as temp:
        attempt = Path(temp)
        rows = []
        for index, (crate, values) in enumerate(
            zip(crates, configurations, strict=True)
        ):
            merge_input = attempt / "translations" / str(index) / "rust"
            copy_merge_input(crate, merge_input)
            rows.append({"dir": str(merge_input), **values})
        manifest = attempt / "translations.json"
        manifest.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        run_merge(manifest, attempt, ignore_boolean=ignore_boolean)
        (attempt / "rust").rename(publication / "rust")


def cmake_defaults(
    project: Path, work: Path, case_name: str, adapter: StageEntry
) -> tuple[dict[str, str], str | None]:
    snapshot = work / "default-source" / case_name / project.name
    shutil.copytree(
        project, snapshot, symlinks=True, ignore=shutil.ignore_patterns("target")
    )
    source = cmake_source(snapshot)
    build = work / "default-build"
    generator = "Ninja" if shutil.which("ninja") else "Unix Makefiles"
    command = ["cmake", "-S", str(source), "-B", str(build), "-G", generator]
    preset = None
    if (source / "CMakePresets.json").is_file():
        preset = str(adapter.config.get("preset", "test"))
        command.extend(("--preset", preset))
    for name, value in adapter.config.get("cmake_cache_variables", {}).items():
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        command.append(f"-D{name}={value}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"CMake default configure failed. Using the first translated values.\n"
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return {}, preset

    defaults = {}
    for line in (build / "CMakeCache.txt").read_text(encoding="utf-8").splitlines():
        if not line.startswith(("#", "//")):
            entry, separator, value = line.partition("=")
            if separator:
                defaults[entry.partition(":")[0]] = value
    return defaults, preset


def set_default_features(
    crate: Path,
    parameters: dict[str, list[BuildValue]],
    defaults: dict[str, str],
    preset: str | None,
    ignore_boolean: bool,
) -> None:
    cargo_file = crate / "Cargo.toml"
    cargo = tomllib.loads(cargo_file.read_text(encoding="utf-8"))
    features = cargo["features"]
    selected = []
    for name, values in parameters.items():
        cached = defaults.get(name)
        if all(isinstance(value, bool) for value in values):
            if ignore_boolean:
                continue
            enabled = values[0]
            if cached is not None:
                upper = cached.upper()
                enabled = upper not in {
                    "",
                    "0",
                    "OFF",
                    "NO",
                    "FALSE",
                    "N",
                    "IGNORE",
                    "NOTFOUND",
                } and not upper.endswith("-NOTFOUND")
            feature = name if enabled else None
        else:
            value = next((value for value in values if str(value) == cached), values[0])
            feature = f"{name}_{value}"
        if feature is not None and feature in features:
            selected.append(feature)
    if preset is not None:
        features.setdefault(f"{preset}_config", selected)
    features["default"] = list(dict.fromkeys([*features.get("default", []), *selected]))
    cargo_file.write_text(tomli_w.dumps(cargo), encoding="utf-8")


def orchestrate(
    config_files: list[Path],
    input_c: Path,
    rule_set: Path | None,
    output: Path,
    jobs: int,
) -> None:
    config = PipelineConfig.from_dict(load_config(config_files))
    inputs = {"c_project": input_c}
    if rule_set is not None:
        rule_set = rule_set.resolve(strict=True)
        if not rule_set.is_file():
            raise ConfigError(f"rule set must be a file: {rule_set}")
        inputs["rule_set"] = rule_set
    missing = set(config.run.provides).difference(inputs)
    if missing:
        raise ConfigError(
            f"pipeline requires inputs that were not supplied: {', '.join(sorted(missing))}"
        )
    if output.exists() or output.is_symlink():
        raise RunError(f"output already exists: {output}")
    validation, stages = validate_pipeline(config, REPO_ROOT)
    if not validation.ok:
        raise RunError("\n".join(validation.errors))

    with tempfile.TemporaryDirectory(prefix="proctor-configurations-") as temp:
        work = Path(temp)
        project, case_name = prepare_project(input_c, work)
        inputs["c_project"] = project
        parameters = read_parameters(project)
        selected, ignore_boolean = combinations(parameters)
        adapters = [
            stage.resolved.entry for stage in stages if stage.manifest.id == "c2rust"
        ]
        c2rust_ids = {entry.id for entry in adapters}
        adapter = None
        if parameters:
            if len(adapters) != 1:
                raise ConfigError(
                    "build configurations require exactly one enabled C2Rust adapter"
                )
            adapter = adapters[0]
            if not isinstance(adapter.config.get("cmake_cache_variables", {}), dict):
                raise ConfigError("C2Rust cmake_cache_variables must be a table")

        run_root = (
            REPO_ROOT / config.run.output_dir / make_run_id(case_name)
        ).resolve()
        if run_root.is_relative_to(project):
            raise RunError("pipeline run directory must be outside the C project")
        print(f"Translating {len(selected)} configuration(s) in {run_root}", flush=True)

        def translate(
            item: tuple[int, BuildParameters],
        ) -> tuple[Path, dict[str, Path]]:
            index, values = item
            variant_config = config
            if adapter is not None:
                overlay = {
                    "stages": {
                        adapter.id: {"config": {"cmake_cache_variables": values}}
                    }
                }
                variant_config = PipelineConfig.from_dict(
                    deep_merge(config.raw, overlay)
                )
            run_dir = run_root / f"variant-{index}"
            result = start_run(
                variant_config,
                REPO_ROOT,
                name=case_name,
                supplied_inputs=inputs,
                config_files=config_files,
                overrides=[],
                item=case_name,
                run_dir=run_dir,
            )
            c2rust_succeeded = any(
                stage.stage_id in c2rust_ids and stage.status == "success"
                for stage in result.stages
            )
            if not (c2rust_succeeded if c2rust_ids else result.ok):
                raise RunError(f"translation failed for {values} in {run_dir}")
            rust_outputs = {
                stage.stage_id: stage.rust_project
                for stage in result.stages
                if stage.rust_project is not None
                and (stage.rust_project / "Cargo.toml").is_file()
            }
            crate = result.final.get("rust_project")
            if crate is None or not (crate / "Cargo.toml").is_file():
                crate = next(reversed(rust_outputs.values()), None)
            if crate is None:
                raise RunError(
                    f"translation in {run_dir} did not produce a Cargo project"
                )
            if not result.ok:
                failed = ", ".join(
                    stage.stage_id
                    for stage in result.stages
                    if stage.status == "failure"
                )
                print(
                    f"Using Rust output for {values} after failure in {failed}. "
                    f"See {run_dir} for details.",
                    file=sys.stderr,
                    flush=True,
                )
            return crate, rust_outputs

        with ThreadPoolExecutor(max_workers=min(jobs, len(selected))) as workers:
            translations = list(workers.map(translate, enumerate(selected, start=1)))

        defaults, preset = (
            cmake_defaults(project, work, case_name, adapter) if adapter else ({}, None)
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".proctor-merge-", dir=output.parent
        ) as temp:
            publication = Path(temp).resolve()
            merged_crate = publication / "rust"
            crates = [crate for crate, _ in translations]
            if adapter is None:
                copy_merge_input(crates[0], merged_crate)
            else:
                attempts = [("latest", crates)]
                for stage_id in reversed(
                    config.order[config.order.index(adapter.id) :]
                ):
                    if all(stage_id in outputs for _, outputs in translations):
                        shared = [outputs[stage_id] for _, outputs in translations]
                        if all(shared != previous for _, previous in attempts):
                            attempts.append((stage_id, shared))
                for index, (label, projects) in enumerate(attempts):
                    if index:
                        print(f"Retrying merge with {label} outputs", file=sys.stderr)
                    try:
                        merge_variants(
                            projects,
                            selected,
                            publication,
                            ignore_boolean=ignore_boolean,
                        )
                    except subprocess.CalledProcessError:
                        if index == len(attempts) - 1:
                            raise
                    else:
                        break
                set_default_features(
                    merged_crate, parameters, defaults, preset, ignore_boolean
                )
            if output.exists() or output.is_symlink():
                raise RunError(f"output already exists: {output}")
            merged_crate.rename(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", action="append", required=True, type=Path)
    parser.add_argument("--input-c", required=True, type=Path)
    parser.add_argument("--rule-set", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    cpu_count = (
        len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count()
    )
    parser.add_argument("--jobs", type=int, default=cpu_count or 1)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    try:
        orchestrate(
            args.config,
            args.input_c.absolute(),
            args.rule_set,
            args.output.absolute(),
            args.jobs,
        )
    except (
        RunError,
        ValueError,
        OSError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"translated Rust crate: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
