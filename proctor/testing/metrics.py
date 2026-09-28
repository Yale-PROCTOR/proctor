"""Simple CLI: measure `unsafe` + idiomaticity of a translated Rust project,
or per-stage across a run dir.

    python -m proctor.testing.metrics <crate-or-run-dir> [--complexity]
                                      [--no-idiomaticity] [--json out.json]

- Given a Cargo project (a dir with Cargo.toml), report both metrics for it.
- Given a run dir (a dir with a stages/ subtree, e.g. a bench case dir),
  report both per stage and the reduction vs the first stage.

Unsafe is always measured (DARPA measure_unsafety, source-only). Idiomaticity
needs the crate to build under clippy; if it can't, that line is marked
skipped rather than failing the whole report.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proctor.testing.idiomaticity_eval import (
    IdiomEvalError,
    IdiomReport,
    measure_idiomaticity,
)
from proctor.testing.unsafe_eval import UnsafeEvalError, UnsafeReport, measure_unsafe


@dataclass
class StageMetrics:
    stage: str
    unsafe: UnsafeReport | None = None
    unsafe_error: str = ""
    idiom: IdiomReport | None = None
    idiom_error: str = ""


def _measure(crate: Path, *, do_idiom: bool, complexity: bool) -> StageMetrics:
    m = StageMetrics(stage=crate.name)
    try:
        m.unsafe = measure_unsafe(crate)
    except (UnsafeEvalError, OSError) as exc:
        m.unsafe_error = str(exc)
    if do_idiom:
        loc = m.unsafe.total_lines if m.unsafe else 0
        with tempfile.TemporaryDirectory() as td:
            try:
                m.idiom = measure_idiomaticity(
                    crate, workdir=Path(td), loc=loc, include_complexity=complexity
                )
            except (IdiomEvalError, OSError) as exc:
                m.idiom_error = str(exc)
    return m


def _pct(now: int, base: int) -> str:
    if base <= 0:
        return ""
    return f" ({100 * (now - base) / base:+.0f}%)"


def _print_one(crate: Path, m: StageMetrics) -> None:
    print(f"crate: {crate}")
    if m.unsafe:
        print(f"  unsafe:       {m.unsafe.summary()}")
    else:
        print(f"  unsafe:       (error: {m.unsafe_error})")
    if m.idiom:
        print(f"  idiomaticity: {m.idiom.summary()}")
        top = m.idiom.top_lints()
        if top:
            print("    top lints:  " + ", ".join(f"{lint} ×{c}" for lint, c in top))
    elif m.idiom_error:
        print(f"  idiomaticity: (skipped — {m.idiom_error.splitlines()[0][:80]})")


def _print_stage_table(rows: list[StageMetrics]) -> None:
    u_scores = [r.unsafe.score for r in rows if r.unsafe]
    base_u = u_scores[0] if u_scores else 0
    i_totals = [r.idiom.total for r in rows if r.idiom]
    base_i = i_totals[0] if i_totals else 0

    print(f"{'stage':<22}{'unsafe (score/KLOC)':<26}{'clippy (lints)':<22}{'LOC':>6}")
    print("-" * 76)
    for r in rows:
        if r.unsafe:
            us = f"{r.unsafe.score} ({r.unsafe.per_kloc}/K){_pct(r.unsafe.score, base_u)}"
            loc = r.unsafe.total_lines
        else:
            us, loc = "error", 0
        if r.idiom:
            it = f"{r.idiom.total}{_pct(r.idiom.total, base_i)}"
        elif r.idiom_error:
            it = "skipped"
        else:
            it = "-"
        print(f"{r.stage:<22}{us:<26}{it:<22}{loc:>6}")
    print("-" * 76)
    print(
        "(unsafe: DARPA measure_unsafety, syntactic; clippy lints: fewer = more idiomatic;"
    )
    print(" % is vs the first stage — negative is an improvement)")


def _to_json(rows: list[StageMetrics]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d: dict[str, Any] = {"stage": r.stage}
        if r.unsafe:
            d["unsafe"] = {
                "score": r.unsafe.score,
                "per_kloc": r.unsafe.per_kloc,
                "blocks": r.unsafe.blocks,
                "fns": r.unsafe.fns,
                "impls": r.unsafe.impls,
                "statements": r.unsafe.statements,
                "total_lines": r.unsafe.total_lines,
            }
        elif r.unsafe_error:
            d["unsafe_error"] = r.unsafe_error
        if r.idiom:
            d["idiomaticity"] = {
                "total": r.idiom.total,
                "per_kloc": r.idiom.per_kloc,
                "by_group": r.idiom.group_totals(),
                "max_complexity": r.idiom.max_complexity,
            }
        elif r.idiom_error:
            d["idiomaticity_error"] = r.idiom_error.splitlines()[0]
        out.append(d)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="metrics",
        description="Measure unsafe + idiomaticity of a Rust translation, or per stage.",
    )
    ap.add_argument(
        "path", type=Path, help="a Cargo project dir, or a run dir with stages/"
    )
    ap.add_argument(
        "--no-idiomaticity",
        action="store_true",
        help="skip clippy (unsafe only; no build needed)",
    )
    ap.add_argument(
        "--complexity",
        action="store_true",
        help="also collect the cognitive-complexity histogram (slower)",
    )
    ap.add_argument(
        "--json", type=Path, default=None, help="also write results as JSON"
    )
    args = ap.parse_args(argv)

    path = args.path.resolve()
    do_idiom = not args.no_idiomaticity

    if (path / "Cargo.toml").is_file():
        m = _measure(path, do_idiom=do_idiom, complexity=args.complexity)
        _print_one(path, m)
        rows = [m]
    elif (path / "stages").is_dir():
        from proctor.testing.vector_compare import stage_rust_outputs

        outputs = stage_rust_outputs(path)
        if not outputs:
            print(f"no stage Rust outputs under {path}/stages", file=sys.stderr)
            return 1
        print(f"per-stage metrics for {path.name}\n")
        rows = []
        for stage_id, rust in outputs:
            m = _measure(rust, do_idiom=do_idiom, complexity=args.complexity)
            m.stage = stage_id
            rows.append(m)
        _print_stage_table(rows)
    else:
        print(
            f"{path} is neither a Cargo project nor a run dir (no Cargo.toml / stages/)",
            file=sys.stderr,
        )
        return 2

    if args.json:
        args.json.write_text(
            json.dumps(_to_json(rows), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
