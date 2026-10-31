#!/usr/bin/env python3

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess


FEATURES = (
    "transmute",
    "union",
    "deref",
    "offset",
    "alloc",
    "std",
    "lib",
    "static",
    "fnptr",
)
ROOT = Path(__file__).resolve().parents[1]


def classify(name: str) -> str:
    if name == "DerefOfRawPointer":
        return "deref"
    if name == "UseOfMutableStatic":
        return "static"
    if name == "AccessToUnionField":
        return "union"
    if name == "CallToUnsafeFunction(None)":
        return "fnptr"
    if name == "transmute":
        return "transmute"
    if name in {"offset", "offset_from"}:
        return "offset"
    if name in {"calloc", "free", "malloc", "realloc"}:
        return "alloc"
    if name in {"as_mut", "as_ref", "from_ptr", "from_raw_parts", "from_raw_parts_mut"}:
        return "std"
    return "lib"


def find_unsafe(directory: Path) -> list[str]:
    result = subprocess.run(
        ["./stages/crat/crat-finder", "unsafe", str(directory)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate unsafe operations across PROCTOR run directories."
    )
    parser.add_argument(
        "--prefix", default="", help="collect runs with this name prefix"
    )
    args = parser.parse_args()

    directories = []
    for run in sorted(ROOT.glob("runs/*")):
        if not run.is_dir() or not run.name.startswith(args.prefix):
            continue
        stage = max(
            (
                path
                for path in (run / "stages").glob("[0-9]*-*")
                if path.is_dir() and path.name.partition("-")[0].isdigit()
            ),
            key=lambda path: int(path.name.partition("-")[0]),
            default=None,
        )
        if stage is not None:
            directory = stage / "out" / "rust"
            if directory.is_dir():
                directories.append(directory)

    counts: Counter[str] = Counter()
    if directories:
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as executor:
            for names in executor.map(find_unsafe, directories):
                counts.update(classify(name) for name in names if name)

    print(f"Collected directories: {len(directories)}")
    print("\t".join(feature[:7] for feature in FEATURES))
    print("\t".join(str(counts[feature]) for feature in FEATURES))


if __name__ == "__main__":
    main()
