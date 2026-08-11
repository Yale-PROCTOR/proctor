#!/usr/bin/env python3

"""Aggregate local-transformation statistics JSON files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _path_text(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "<root>"


def _aggregate(values: list[Any], path: tuple[str, ...] = ()) -> Any:
    first = values[0]
    if isinstance(first, dict):
        keys = list(first)
        expected = set(keys)
        for value in values[1:]:
            if not isinstance(value, dict) or set(value) != expected:
                raise ValueError(f"inconsistent object fields at {_path_text(path)}")
        return {
            key: _aggregate([value[key] for value in values], (*path, key))
            for key in keys
        }

    if path == ("schema_version",):
        if isinstance(first, bool) or not isinstance(first, int):
            raise ValueError("schema_version must be an integer")
        if any(value != first for value in values[1:]):
            raise ValueError("input schema_version values do not match")
        return first

    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in values
    ):
        raise ValueError(f"expected numbers at {_path_text(path)}")
    return sum(values)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sum numeric fields across statistics JSON files."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="input JSON files")
    parser.add_argument("-o", "--output", type=Path, help="output JSON file")
    args = parser.parse_args()

    try:
        result = _aggregate([_load(path) for path in args.inputs])
        rendered = json.dumps(result, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.write_text(rendered, encoding="utf-8")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
