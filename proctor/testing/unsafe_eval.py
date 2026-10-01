"""Measure `unsafe` usage of a translated Rust project.

We drive DARPA's own scorer — the vendored `measure_unsafety` (a `syn` AST
visitor under ``tools/measure_unsafety``), the same tool performers are
scored with — not a reimplementation. It's syntactic, so it needs no
toolchain and works even if the crate doesn't compile.

The tool's project mode only scans ``<project>/src``, but c2rust/crat output
often keeps the library in a root ``lib.rs``; to avoid undercounting we run
it in single-file mode over every ``*.rs`` (excluding ``target/``) and sum,
which reproduces its own per-file aggregation and skips files it can't parse.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TOOL_DIR = Path(__file__).resolve().parent.parent.parent / "tools" / "measure_unsafety"
_BIN = _TOOL_DIR / "target" / "release" / "measure_unsafety"

#: integer Stats fields emitted by measure_unsafety (all summable).
_STAT_FIELDS = (
    "total_files",
    "total_lines",
    "total_tokens",
    "total_statements",
    "unsafe_score",
    "unsafe_statements",
    "unsafe_fns",
    "unsafe_blocks",
    "unsafe_impls",
    "unsafe_other",
)


class UnsafeEvalError(RuntimeError):
    """measure_unsafety could not be built or run (not an unsafe finding)."""


@dataclass(frozen=True)
class UnsafeReport:
    """DARPA measure_unsafety stats for a crate. ``score`` is the headline
    ``unsafe_score``; the rest is the breakdown."""

    score: int
    statements: int
    fns: int
    blocks: int
    impls: int
    other: int
    total_lines: int
    total_statements: int
    files_scanned: int = 0
    files_skipped: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def per_kloc(self) -> float:
        """unsafe_score per 1000 source lines (size-normalized)."""
        return (
            round(1000 * self.score / self.total_lines, 1) if self.total_lines else 0.0
        )

    def summary(self) -> str:
        skip = f", {self.files_skipped} unparsed" if self.files_skipped else ""
        return (
            f"unsafe score {self.score}  ({self.per_kloc}/KLOC)  "
            f"[{self.blocks} blocks, {self.fns} fns, {self.impls} impls, "
            f"{self.statements} stmts; {self.total_lines} LOC{skip}]"
        )

    @classmethod
    def from_stats(
        cls, d: dict[str, Any], *, files_scanned: int = 0, files_skipped: int = 0
    ) -> UnsafeReport:
        return cls(
            score=d.get("unsafe_score", 0),
            statements=d.get("unsafe_statements", 0),
            fns=d.get("unsafe_fns", 0),
            blocks=d.get("unsafe_blocks", 0),
            impls=d.get("unsafe_impls", 0),
            other=d.get("unsafe_other", 0),
            total_lines=d.get("total_lines", 0),
            total_statements=d.get("total_statements", 0),
            files_scanned=files_scanned,
            files_skipped=files_skipped,
            raw=d,
        )


def ensure_built(*, timeout_s: int = 900) -> Path:
    """Build the vendored measure_unsafety binary on demand (stable rust)."""
    if _BIN.is_file():
        return _BIN
    proc = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=str(_TOOL_DIR),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0 or not _BIN.is_file():
        raise UnsafeEvalError(
            f"failed to build measure_unsafety:\n{(proc.stderr or proc.stdout)[-1500:]}"
        )
    return _BIN


def _rust_files(rust_project: Path) -> list[Path]:
    return sorted(p for p in rust_project.rglob("*.rs") if "target" not in p.parts)


def measure_unsafe(rust_project: Path, *, timeout_s: int = 300) -> UnsafeReport:
    """Score a crate's `unsafe` usage. ``rust_project`` is a Cargo project dir
    (e.g. a stage's ``out/rust``); every ``*.rs`` under it is scanned."""
    binary = ensure_built()
    files = _rust_files(rust_project)
    if not files:
        raise UnsafeEvalError(f"no .rs files under {rust_project}")

    total: dict[str, int] = dict.fromkeys(_STAT_FIELDS, 0)
    scanned = skipped = 0
    for f in files:
        try:
            proc = subprocess.run(
                [str(binary), "--file", str(f)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            # a pathological file hanging the scorer shouldn't sink the crate.
            skipped += 1
            continue
        if proc.returncode != 0:
            # measure_unsafety panics (non-zero) on a file syn can't parse.
            skipped += 1
            continue
        try:
            d = json.loads(proc.stdout)
        except json.JSONDecodeError:
            skipped += 1
            continue
        for k in _STAT_FIELDS:
            total[k] += int(d.get(k, 0))
        scanned += 1

    if scanned == 0:
        raise UnsafeEvalError(
            f"measure_unsafety parsed none of {len(files)} file(s) under {rust_project}"
        )
    return UnsafeReport.from_stats(total, files_scanned=scanned, files_skipped=skipped)
