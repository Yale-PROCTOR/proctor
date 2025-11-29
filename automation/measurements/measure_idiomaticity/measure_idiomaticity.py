#!/usr/bin/env python3

"""
Slightly modified version of pipeline-automation/idiomaticity/take_measurements.py
"""

import argparse
import json  # for loading compiler messages
import os
import re
import subprocess
from collections import Counter
from pathlib import Path


def check_lints(target: Path):
    # Separates out lints for each category.
    # Clippy lints from the correctness, suspicious, complexity, perf, and style
    # groups are counted by group.
    # rustc errors and warnings are also included.

    # rustc compiler errors + warnings will have (among other fields)
    # {"reason": "compiler-message",
    #   "message": {
    #     "code": { # if code is null, not warning/error
    #       "code": "unused_variables" # or other reason
    #     },
    #     "level": "warning" # or other level
    #   }
    # }
    # if the message is from Clippy, the "code" name will start with "clippy::"

    rustc_msgs = {
        "error": Counter(),
        "warning": Counter(),
    }
    done_with_compiler = False  # only count compiler errors once

    # each category of lints to separate out
    clippy_lint_args = (
        ("-D", "correctness"),
        ("-W", "suspicious"),
        ("-W", "complexity"),
        ("-W", "perf"),
        ("-W", "style"),
    )
    clippy_msgs = {kind[1]: Counter() for kind in clippy_lint_args}

    for clippy_lint_pair in clippy_lint_args:
        args = [
            "cargo",
            "clippy",  # note `cargo check` does not catch everything
            "--message-format",
            "json",
            "--manifest-path",
            str(target.resolve()),
            "--",
            "-A",
            "clippy::all",
            clippy_lint_pair[0],
            f"clippy::{clippy_lint_pair[1]}",
        ]
        res = subprocess.run(args, text=True, capture_output=True)
        if res.returncode != 0:
            print(f"stdout: {res.stdout}")
            print(f"stderr: {res.stderr}")

        # compiler messages with warnings/errors go to stdout
        for line in res.stdout.split("\n"):
            if line == "" or line[0] != "{":
                continue  # not a json message
            message = json.loads(line)
            if message["reason"] != "compiler-message":
                continue  # not a compiler message we care about

            code_dict = message["message"]["code"]
            level = message["message"]["level"]

            if code_dict and "code" in code_dict:
                kind = code_dict["code"]
            else:
                kind = "unknown"

            if kind.startswith("clippy::"):
                clippy_msgs[clippy_lint_pair[1]][kind] += 1
            elif not done_with_compiler:
                rustc_msgs[level][kind] += 1

        done_with_compiler = True  # only count c2rust errors once, not for every bucket

    lint_counts = {
        "rustc": {
            level: (
                rustc_msgs[level].total(),
                {kind: count for kind, count in rustc_msgs[level].items()},
            )
            for level in rustc_msgs
        },
        "clippy": {
            category: (
                clippy_msgs[category].total(),
                {kind: count for kind, count in clippy_msgs[category].items()},
            )
            for category in clippy_msgs
            if clippy_msgs[category].total()
        },
    }

    return lint_counts


cog_comp_pattern = re.compile(
    r"^the function has a cognitive complexity of \((\d+)/0\)$"
)


def cog_complexity_counts(target: Path):
    # check cyclomatic complexity as computed by clippy for everything
    args = [
        "cargo",
        "clippy",
        "--message-format",
        "json",
        "--manifest-path",
        str(target.resolve()),
        "--",
        "-A",
        "clippy::all",  # silence everything
        "-W",
        "clippy::cognitive_complexity",
    ]  # except cognitive complexity
    # print(" ".join(args))

    all_counts = Counter()

    my_env = os.environ.copy()
    # point to config that tells Clippy to report all cognitive complexity numbers
    # my_env["CLIPPY_CONF_DIR"] = Path("./clippy.toml").resolve()
    res = subprocess.run(args, env=my_env, text=True, capture_output=True)
    if res.returncode != 0:
        print(f"stdout: {res.stdout}")
        print(f"stderr: {res.stderr}")
        return dict(all_counts)

    for line in res.stdout.split("\n"):
        if line == "" or line[0] != "{":
            continue  # not a json message
        message = json.loads(line)
        if message["reason"] != "compiler-message":
            continue  # not a compiler message we care about

        code_dict = message["message"]["code"]

        if code_dict:
            kind = code_dict["code"]
            if kind == "clippy::cognitive_complexity":
                cog_printed = message["message"]["message"]
                cog_value = int(cog_comp_pattern.match(cog_printed).group(1))
                all_counts[cog_value] += 1

    return dict(all_counts)


def all_idiomaticity_measures(target: Path):
    results = {}

    results["cyclomatic_complexity_counts"] = cog_complexity_counts(target_manifest)
    results["lints"] = check_lints(target_manifest)

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Measure idiomaticity in a variety of ways."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="The Cargo.toml file for the translation to evaluate",
        nargs="?",
        default="/tmp/src/Cargo.toml",
    )

    parser.add_argument(
        "--output",
        type=Path,
        help="The file to place results in",
        nargs="?",
        default="out.json",
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--uid", type=int, help="The user ID to set on output files")
    parser.add_argument("--gid", type=int, help="The group ID to set on output files")

    args = parser.parse_args()

    target_manifest = args.manifest
    output = args.output.resolve()

    os.chdir(target_manifest.parent)
    target_manifest = Path(target_manifest.name)

    subprocess.run(
        ["rustup", "component", "add", "clippy"],
        text=True,
        check=True,
    )

    results = all_idiomaticity_measures(target_manifest)

    if args.debug:
        print("Complexity of functions:", results["cyclomatic_complexity_counts"])
        print("Lint counts:", results["lints"])

    with open(output, "w") as f:
        json.dump(results, f, indent=2)
        os.chown(output, args.uid, args.gid)

    target_dir = target_manifest.parent / "target"
    if target_dir.exists():
        os.chown(target_dir, args.uid, args.gid)
        for subpath in target_dir.rglob("*"):
            os.chown(subpath, args.uid, args.gid)
