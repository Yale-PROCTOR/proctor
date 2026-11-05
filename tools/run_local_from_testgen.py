#!/usr/bin/env python3

"""Run local transformation from successful non_local_* test-generation outputs."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _succeeded(run: Path) -> bool:
    last_event = {}
    for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            last_event = json.loads(line)
    return last_event.get("event") == "run_finished" and last_event.get("ok") is True


def _command(run: Path, name: str) -> list[str]:
    record = json.loads((run / "run.json").read_text(encoding="utf-8"))
    output_path = run / "stages" / "02-test_generation" / "stage_output.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    if output["status"] != "success":
        raise ValueError("test generation did not succeed")

    rust = run / output["outputs"]["rust_project"]
    c_project = run / record["inputs"]["c_project"]
    rule_set = run / record["inputs"]["rule_set"]
    if not rust.is_dir():
        raise ValueError(f"Rust project missing: {rust}")
    if not c_project.exists():
        raise ValueError(f"C project missing: {c_project}")
    if not rule_set.is_file():
        raise ValueError(f"rule set missing: {rule_set}")

    command = [
        "uv",
        "run",
        "proctor",
        "run",
        "-c",
        "configs/c2rust_crat_testgen_local.toml",
        "--set",
        'pipeline.order=["local_transformation"]',
        "--set",
        'run.provides=["c_project","rust_project","rule_set"]',
        "--input-rust",
        str(rust),
        "--input-c",
        str(c_project),
        "--rule-set",
        str(rule_set),
        "--name",
        name,
    ]
    if record.get("item") is not None:
        command.extend(["--item", record["item"]])
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create fresh local_* runs from the saved test-generation outputs "
            "of successful runs/non_local_* runs. Only local transformation executes."
        )
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print commands without executing them"
    )
    parser.add_argument(
        "--prefix", default="", help="filter source runs by name prefix"
    )
    args = parser.parse_args(argv)

    runs_directory = ROOT / "runs"
    existing_runs = (
        {path.name for path in runs_directory.iterdir() if path.is_dir()}
        if runs_directory.is_dir()
        else set()
    )
    skipped = 0
    errors: list[tuple[Path, str]] = []
    failures: list[tuple[list[str], str, str]] = []
    pending: list[tuple[Path, list[str]]] = []
    for run in sorted(runs_directory.glob("non_local_*")):
        if not run.is_dir() or not run.name.startswith(args.prefix):
            continue
        name = run.name.removeprefix("non_").rsplit("-", 2)[0]
        if any(run == name or run.startswith(f"{name}-") for run in existing_runs):
            skipped += 1
            continue
        try:
            if not _succeeded(run):
                skipped += 1
                continue
            command = _command(run, name)
        except (OSError, ValueError, KeyError) as exc:
            errors.append((run, str(exc)))
            continue
        pending.append((run, command))

    bar_width = 20
    count_width = len(str(len(pending)))
    label_width = max((len(run.name) for run, _ in pending), default=len("complete"))

    for index, (run, command) in enumerate(pending, start=1):
        if args.dry_run:
            print(shlex.join(command))
            continue
        completed_width = (index - 1) * bar_width // len(pending)
        bar = "#" * completed_width + "-" * (bar_width - completed_width)
        print(
            f"\r[{bar}] {index - 1:>{count_width}}/{len(pending)} "
            f"{run.name:<{label_width}}",
            end="",
            file=sys.stderr,
            flush=True,
        )
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            failures.append((command, "", str(exc)))
        else:
            if result.returncode != 0:
                failures.append((command, result.stdout, result.stderr))

    if pending and not args.dry_run:
        print(
            f"\r[{'#' * bar_width}] {len(pending)}/{len(pending)} "
            f"{'complete':<{label_width}}",
            file=sys.stderr,
            flush=True,
        )

    for run, error in errors:
        print(f"{run.name}: {error}")
    for command, stdout, stderr in failures:
        print(f"command: {shlex.join(command)}")
        print("stdout:")
        print(stdout, end="" if stdout.endswith("\n") else "\n")
        print("stderr:")
        print(stderr, end="" if stderr.endswith("\n") else "\n")

    action = "planned" if args.dry_run else "processed"
    succeeded = "" if args.dry_run else f"; succeeded: {len(pending) - len(failures)}"
    failure_count = len(errors) + len(failures)
    print(
        f"{action}: {len(pending)}{succeeded}; skipped: {skipped}; "
        f"failures: {failure_count}"
    )
    return int(failure_count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
