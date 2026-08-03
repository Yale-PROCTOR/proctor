from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Never

from model import load_observations, rules_to_json
from rule_synthesis import synthesize_rules


class _ArgumentFailure(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise _ArgumentFailure(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="extract_rules.py")
    parser.add_argument("--output", required=True, metavar="OUTPUT")
    parser.add_argument("observations", nargs="+", metavar="OBSERVATIONS")
    return parser


def _validated_paths(observations: list[str], output: str) -> tuple[list[Path], Path]:
    inputs: list[Path] = []
    resolved_inputs: set[Path] = set()
    for supplied in observations:
        path = Path(supplied)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValueError(f"cannot inspect input {supplied!r}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"input {supplied!r} must be a regular nonsymlink file")
        resolved = path.resolve(strict=False)
        if resolved in resolved_inputs:
            raise ValueError(f"input {supplied!r} resolves to a repeated path")
        resolved_inputs.add(resolved)
        inputs.append(path)
    output_path = Path(output)
    resolved_output = output_path.resolve(strict=False)
    if resolved_output in resolved_inputs:
        raise ValueError("output path aliases an input path")
    return inputs, output_path


def _publish_output(path: Path, text: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise OSError(f"cannot inspect output {path}: {exc}") from exc
    else:
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise OSError(f"output {path} must be a regular file or symlink")

    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = None
            stream.write(text)
            stream.flush()
        os.replace(temporary, path)
        temporary = None
    except Exception as primary:
        cleanup_errors: list[str] = []
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_errors.append(f"descriptor close failed: {exc}")
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(f"temporary cleanup failed: {exc}")
        if cleanup_errors:
            raise OSError(f"{primary}; {'; '.join(cleanup_errors)}") from primary
        raise


def _error_line(error: Exception) -> str:
    message = " ".join(str(error).splitlines())
    return message or error.__class__.__name__


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        arguments = parser.parse_args(argv)
    except _ArgumentFailure as exc:
        print(f"extract_rules: {_error_line(exc)}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        inputs, output = _validated_paths(arguments.observations, arguments.output)
        documents = []
        for path in inputs:
            text = path.read_text(encoding="utf-8")
            documents.append(load_observations(text))
        rules = synthesize_rules(tuple(documents))
        serialized = rules_to_json(rules)
        _publish_output(output, serialized)
    except Exception as exc:
        print(f"extract_rules: {_error_line(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
