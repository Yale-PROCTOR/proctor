# © 2026 Massachusetts Institute of Technology
# MIT License

import sys
from enum import StrEnum
from typing import Iterable, Tuple


class Color(StrEnum):
    GREEN = "32"
    RED = "31"
    YELLOW = "33"


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


def _status_to_color(status_in: Status) -> Color:
    match status_in:
        case Status.PASS:
            return Color.GREEN
        case Status.FAIL:
            return Color.RED
        case Status.SKIP:
            return Color.YELLOW


def _color(s: str, code: str, colorize: bool) -> str:
    if not sys.stdout.isatty() or not colorize:
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def print_step(
    status_in: Status,
    rel_name: str,
    message: str,
    colorize: bool = True,
    where=sys.stdout,
) -> None:
    prefix = _color(f"[{status_in}]", _status_to_color(status_in), colorize)
    print(f"{prefix} {rel_name}: {message}", file=where)


def print_summary(
    total_test_cases: int,
    skipped_test_cases: int,
    tested_test_cases: int,
    failed_test_cases: int,
    passed_test_vectors: int,
    skipped_test_vectors: int,
    failed_test_vectors: int,
    colorize: bool = True,
) -> None:

    test_case_failed_status = Status.PASS if (failed_test_cases == 0) else Status.FAIL
    vector_skipped_status = Status.PASS if (skipped_test_vectors == 0) else Status.SKIP
    vector_failed_status = Status.PASS if (failed_test_vectors == 0) else Status.FAIL

    print("\nSummary:")
    print(f"- Test Cases Discovered:      {total_test_cases}")
    print(f"- Test Cases Skipped:         {skipped_test_cases}")
    print(f"- Test Cases Tested:          {tested_test_cases}")
    print(
        _color(
            f"- Test Cases Failed:          {failed_test_cases}",
            _status_to_color(test_case_failed_status),
            colorize,
        )
    )
    print(
        _color(
            f"- Test Vectors Passed:        {passed_test_vectors}",
            _status_to_color(Status.PASS),
            colorize,
        )
    )
    print(
        _color(
            f"- Test Vectors Skipped:       {skipped_test_vectors}",
            _status_to_color(vector_skipped_status),
            colorize,
        )
    )
    print(
        _color(
            f"- Test Vectors Failed:        {failed_test_vectors}",
            _status_to_color(vector_failed_status),
            colorize,
        )
    )


def print_failures(failures: Iterable[Tuple[str, str]]) -> None:
    # failures: iterable of (test_case_rel_name, message)
    fails = list(failures)
    if not fails:
        return
    print("\nSummary of failures:")
    for rel, msg in fails:
        print(f"- {rel}: {msg}")
