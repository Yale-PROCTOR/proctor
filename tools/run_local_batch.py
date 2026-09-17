#!/usr/bin/env python3

"""Run the local C2Rust/Crat configuration for a directory of tarballs."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the local C2Rust/Crat configuration for each .tar.gz file."
    )
    parser.add_argument("directory", type=Path, help="directory containing tarballs")
    parser.add_argument("--rule-set", type=Path, help="rule set JSON file")
    args = parser.parse_args()

    directory = args.directory.resolve()
    if not directory.is_dir():
        parser.error(f"not a directory: {args.directory}")

    if args.rule_set is None:
        rule_set = "empty-rule-set.json"
        name_prefix = ""
    else:
        rule_set_path = args.rule_set.resolve()
        if not rule_set_path.is_file():
            parser.error(f"not a file: {args.rule_set}")
        rule_set = str(rule_set_path)
        name_prefix = f"rule_{rule_set_path.stem}_"

    proctor_root = Path(__file__).resolve().parent.parent
    runs_directory = proctor_root / "runs"
    existing_runs = (
        {path.name for path in runs_directory.iterdir() if path.is_dir()}
        if runs_directory.is_dir()
        else set()
    )
    failures: list[tuple[list[str], str, str]] = []
    tarballs = sorted(path for path in directory.glob("*.tar.gz") if path.is_file())
    pending: list[tuple[Path, str]] = []
    for tarball in tarballs:
        archive_name = tarball.name[: -len(".tar.gz")]
        name = f"{name_prefix}{directory.name}_{archive_name}"
        if not any(
            run == name or run.startswith(f"{name}-") for run in existing_runs
        ):
            pending.append((tarball, name))

    bar_width = 20
    count_width = len(str(len(pending)))
    label_width = max(
        (len(tarball.name) for tarball, _ in pending), default=len("complete")
    )

    for index, (tarball, name) in enumerate(pending, start=1):
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
            "configs/c2rust_crat_local.toml",
            "--input-c",
            str(tarball),
            "--name",
            name,
            "--rule-set",
            rule_set,
        ]
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
