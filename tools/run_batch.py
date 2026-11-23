#!/usr/bin/env python3

"""Run a pipeline stage for tarballs or directories of tarballs."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path


def _source_arguments(run: Path, stage: str) -> list[str]:
    stage_id = {
        "testgen": "test_generation",
        "absrec": "abstraction_recovery",
        "disrep": "discipline_repair",
    }[stage]
    stage_directories = sorted((run / "stages").glob(f"*-{stage_id}"))
    if not stage_directories:
        raise ValueError(f"source run has no {stage_id} stage")
    stage_directory = stage_directories[-1]
    output = json.loads(
        (stage_directory / "stage_output.json").read_text(encoding="utf-8")
    )
    if output["status"] == "success":
        rust = run / (
            output["outputs"].get("rust_project") or stage_directory / "out" / "rust"
        )
    elif output["status"] in ("skipped", "failure"):
        stage_input = json.loads(
            (stage_directory / "stage_input.json").read_text(encoding="utf-8")
        )
        rust = run / stage_input["inputs"]["rust_project"]
    else:
        raise ValueError(f"source stage {stage_id} did not succeed")
    if not rust.is_dir():
        raise ValueError(f"Rust project missing: {rust}")

    record = json.loads((run / "run.json").read_text(encoding="utf-8"))
    inputs = {
        kind: run / path for kind, path in record["inputs"].items() if path is not None
    }
    inputs["rust_project"] = rust
    flags = {
        "c_project": "--input-c",
        "rust_project": "--input-rust",
        "test_package": "--tests",
        "rule_set": "--rule-set",
    }
    arguments = ["--set", f"run.provides={json.dumps(list(inputs))}"]
    for kind, path in inputs.items():
        if not path.exists():
            raise ValueError(f"{kind} missing: {path}")
        arguments.extend([flags[kind], str(path)])
    if record.get("item") is not None:
        arguments.extend(["--item", record["item"]])
    return arguments


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a pipeline stage for each .tar.gz file."
    )
    source_stages = {
        "testgen": (),
        "cratrep": ("testgen", "absrec", "disrep"),
        "absrec": ("testgen",),
        "disrep": ("absrec",),
        "local": ("testgen", "absrec", "disrep"),
    }
    parser.add_argument("stage", choices=source_stages, help="stage to run")
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="tarballs or directories containing tarballs",
    )
    parser.add_argument(
        "--from",
        dest="source_stage",
        choices=("testgen", "absrec", "disrep"),
        help="source stage (required except for testgen)",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="filter tarballs by filename prefix",
    )
    args = parser.parse_args()
    if args.stage == "testgen":
        if args.source_stage is not None:
            parser.error("--from must not be given for testgen")
    elif args.source_stage is None:
        parser.error(f"--from is required for {args.stage}")
    elif args.source_stage not in source_stages[args.stage]:
        parser.error(
            f"--from for {args.stage} must be one of: "
            f"{', '.join(source_stages[args.stage])}"
        )

    tarballs: list[Path] = []
    for path in args.paths:
        target = path.resolve()
        if target.is_file():
            if target.name.endswith(".tar.gz"):
                tarballs.append(target)
        elif target.is_dir():
            tarballs.extend(
                sorted(path for path in target.glob("*.tar.gz") if path.is_file())
            )
        else:
            parser.error(f"not a file or directory: {path}")

    proctor_root = Path(__file__).resolve().parent.parent
    runs_directory = proctor_root / "runs"
    existing_runs = (
        {path.name for path in runs_directory.iterdir() if path.is_dir()}
        if runs_directory.is_dir()
        else set()
    )
    run_prefix = args.stage
    if args.stage in ("cratrep", "local") and args.source_stage in ("absrec", "disrep"):
        run_prefix = f"{args.stage}_{args.source_stage}"
    failures: list[tuple[list[str], str, str]] = []
    pending: list[tuple[Path, str, Path | None]] = []
    for tarball in dict.fromkeys(tarballs):
        if not tarball.name.startswith(args.prefix):
            continue
        archive_name = tarball.name[: -len(".tar.gz")]
        case_name = f"{tarball.parent.name}_{archive_name}"
        name = f"{run_prefix}_{case_name}"
        if any(run == name or run.rsplit("-", 2)[0] == name for run in existing_runs):
            continue
        source_run = None
        if args.source_stage is not None:
            source_name = f"{args.source_stage}_{case_name}"
            matches = [
                run
                for run in existing_runs
                if run == source_name or run.rsplit("-", 2)[0] == source_name
            ]
            if not matches:
                continue
            source_run = runs_directory / max(matches)
        pending.append((tarball, name, source_run))

    config = (
        "configs/c2rust_crat_testgen.toml"
        if args.stage == "testgen"
        else f"configs/{args.stage}.toml"
    )

    bar_width = 20
    count_width = len(str(len(pending)))
    label_width = max(
        (len(tarball.name) for tarball, _, _ in pending), default=len("complete")
    )

    for index, (tarball, name, source_run) in enumerate(pending, start=1):
        completed_width = (index - 1) * bar_width // len(pending)
        bar = "#" * completed_width + "-" * (bar_width - completed_width)
        print(
            f"\r[{bar}] {index - 1:>{count_width}}/{len(pending)} "
            f"{tarball.name:<{label_width}}",
            end="",
            file=sys.stderr,
            flush=True,
        )
        command = [
            "uv",
            "run",
            "proctor",
            "run",
            "-c",
            config,
            "--name",
            name,
        ]
        if source_run is None:
            command.extend(
                ["--input-c", str(tarball), "--rule-set", "empty-rule-set.json"]
            )
        else:
            try:
                command.extend(_source_arguments(source_run, args.source_stage))
            except (OSError, ValueError, KeyError) as exc:
                failures.append((command, "", f"{source_run}: {exc}"))
                continue
        try:
            result = subprocess.run(
                command,
                cwd=proctor_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            failures.append((command, "", str(exc)))
        else:
            if result.returncode != 0:
                failures.append((command, result.stdout, result.stderr))

    if pending:
        print(
            f"\r[{'#' * bar_width}] {len(pending)}/{len(pending)} "
            f"{'complete':<{label_width}}",
            file=sys.stderr,
            flush=True,
        )

    for command, stdout, stderr in failures:
        print(f"command: {shlex.join(command)}")
        print("stdout:")
        print(stdout, end="" if stdout.endswith("\n") else "\n")
        print("stderr:")
        print(stderr, end="" if stderr.endswith("\n") else "\n")

    if failures:
        print(f"failures: {len(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
