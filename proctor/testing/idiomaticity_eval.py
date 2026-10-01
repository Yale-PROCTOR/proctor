"""Measure how idiomatic a translated Rust project is.

We drive Yale's `measure_idiomaticity` (vendored under
``tools/measure_idiomaticity``), which runs ``cargo clippy`` and buckets each
lint by its clippy group — clippy *is* the Rust idiom authority, so we don't
reimplement it. Unlike the unsafe scorer this needs the crate to **build**
(clippy compiles it) with clippy present for the crate's toolchain; the tool
runs ``rustup component add clippy`` in the crate dir (which respects the
crate's ``rust-toolchain`` override) to arrange that.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TOOL_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tools" / "measure_idiomaticity"
)
_SCRIPT = _TOOL_DIR / "measure_idiomaticity.py"
#: holds the cached lint->group map and clippy.toml; used for both
#: clippy_lint_map's JSON_DIR and clippy's own config lookup.
_CONF_DIR = _TOOL_DIR / ".cache"


class IdiomEvalError(RuntimeError):
    """measure_idiomaticity could not be run (setup/build problem)."""


@dataclass(frozen=True)
class IdiomReport:
    """Clippy lint counts grouped by clippy category (style / complexity /
    perf / pedantic / …) plus an optional cognitive-complexity distribution.
    Lower is more idiomatic."""

    by_group: dict[str, dict[str, int]]  # {group: {lint: count}}
    complexity: dict[int, int] = field(default_factory=dict)  # {cog-complexity: n fns}
    loc: int = 0  # source lines, for per-KLOC normalization (0 = unknown)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(sum(lints.values()) for lints in self.by_group.values())

    @property
    def per_kloc(self) -> float:
        return round(1000 * self.total / self.loc, 1) if self.loc else 0.0

    def group_totals(self) -> dict[str, int]:
        return {g: sum(lints.values()) for g, lints in self.by_group.items()}

    def top_lints(self, n: int = 6) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for lints in self.by_group.values():
            for lint, c in lints.items():
                counts[lint] = counts.get(lint, 0) + c
        return sorted(counts.items(), key=lambda kv: -kv[1])[:n]

    @property
    def max_complexity(self) -> int:
        return max(self.complexity) if self.complexity else 0

    def summary(self) -> str:
        gt = self.group_totals()
        groups = ", ".join(
            f"{g} {c}" for g, c in sorted(gt.items(), key=lambda kv: -kv[1])
        )
        norm = f" ({self.per_kloc}/KLOC)" if self.loc else ""
        cc = f"; max cog-complexity {self.max_complexity}" if self.complexity else ""
        return f"{self.total} clippy lints{norm}  [{groups or 'none'}]{cc}"


def measure_idiomaticity(
    rust_project: Path,
    *,
    workdir: Path,
    loc: int = 0,
    include_complexity: bool = False,
    timeout_s: int = 900,
) -> IdiomReport:
    """Run clippy on ``rust_project`` (a Cargo project dir) and group the
    lints. ``workdir`` holds the intermediate ``idiomaticity.json``; ``loc``
    (if known, e.g. from the unsafe pass) enables per-KLOC normalization.
    ``include_complexity`` also collects the cognitive-complexity histogram
    (slower — clippy warns on every function)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out_json = (workdir / "idiomaticity.json").resolve()

    env = os.environ.copy()
    # clippy_lint_map reads CLIPPY_CONF_DIR for the cached map; clippy reads it
    # for clippy.toml — we keep both in _CONF_DIR.
    env["CLIPPY_CONF_DIR"] = str(_CONF_DIR)
    env["PYTHONPATH"] = str(_TOOL_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [
        sys.executable,
        str(_SCRIPT),
        str(Path(rust_project).resolve()),
        "--output",
        str(out_json),
    ]
    if include_complexity:
        cmd.append("--include_ccc")

    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as e:
        raise IdiomEvalError(f"clippy timed out after {timeout_s}s") from e
    if not out_json.is_file():
        raise IdiomEvalError(
            f"measure_idiomaticity produced no output (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout)[-1500:]}"
        )
    try:
        d = json.loads(out_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise IdiomEvalError(f"measure_idiomaticity wrote invalid JSON: {e}") from e
    # a crate that doesn't compile yields no lints; that is NOT "0 lints / maximally
    # idiomatic" — surface it so the caller marks the stage skipped, not a win.
    if d.get("build_error"):
        raise IdiomEvalError(
            f"crate did not build under clippy: {str(d['build_error'])[:500]}"
        )
    by_group = {g: dict(lints) for g, lints in (d.get("clippy") or {}).items()}
    ccc = {
        int(k): int(v) for k, v in (d.get("cyclomatic_complexity_counts") or {}).items()
    }
    return IdiomReport(by_group=by_group, complexity=ccc, loc=loc, raw=d)
