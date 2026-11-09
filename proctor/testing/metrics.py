"""Simple CLI: measure `unsafe` + idiomaticity of a translated Rust project,
or per-stage across a run dir.

    python -m proctor.testing.metrics <crate-or-run-dir> [--complexity]
                                      [--idiomaticity all|first_last|none]
                                      [--json out.json]

- Given a Cargo project (a dir with Cargo.toml), report both metrics for it.
- Given a run dir (a dir with a stages/ subtree, e.g. a bench case dir),
  report them per stage, the reduction vs the first stage, and what each stage
  changed relative to the stage before it (files, lines, ``extern "C"`` ABI).

Unsafe is always measured (DARPA measure_unsafety, source-only), and so is the
per-stage diff (tools/diff_metrics, source-only). Idiomaticity needs the crate
to build under clippy (two builds per crate); if it can't, that line is marked
skipped rather than failing the whole report. ``first_last`` runs clippy only
on the first and last stage, the cheap way to see the whole pipeline's effect.

The same measurements are recorded during a run when ``[metrics]
after_each_stage`` is on: the orchestrator calls :func:`record_stage` after each
stage and :func:`record_run` at the end.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proctor.testing.diff_eval import (
    DiffEvalError,
    DiffReport,
    PublicApi,
    measure_diff,
    public_api,
)
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
    #: what this stage changed vs the previous stage's output (None for the
    #: first stage, or when the diff tool is unavailable — see ``diff_error``).
    diff: DiffReport | None = None
    diff_error: str = ""


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


def _attach_diff(
    m: StageMetrics, prev: Path | None, crate: Path, api: PublicApi | None
) -> None:
    """Fill ``m.diff`` with what changed from ``prev`` to ``crate`` (fail-open)."""
    if prev is None:
        return
    try:
        m.diff = measure_diff(prev, crate, api=api)
    except (DiffEvalError, OSError) as exc:
        m.diff_error = str(exc)


def measure_run(
    run_dir: Path, *, idiomaticity: str = "first_last", complexity: bool = False
) -> list[StageMetrics]:
    """Measure every stage that produced a Rust project in ``run_dir``.
    ``idiomaticity`` is ``all``, ``first_last`` or ``none`` (as in ``[metrics]``;
    ``first-last`` is also accepted); the ABI diff is
    restricted to the C headers' public API when the run recorded a C input."""
    from proctor.testing.vector_compare import stage_rust_outputs

    idiomaticity = idiomaticity.replace("-", "_")
    outputs = stage_rust_outputs(run_dir)
    c_project = run_dir / "inputs" / "c"
    try:
        api = public_api(c_project) if c_project.is_dir() else None
    except OSError:
        api = None  # unscoped: stricter, never falsely clean
    rows: list[StageMetrics] = []
    prev: Path | None = None
    for i, (stage_id, rust) in enumerate(outputs):
        clippy = idiomaticity == "all" or (
            idiomaticity == "first_last" and i in (0, len(outputs) - 1)
        )
        m = _measure(rust, do_idiom=clippy, complexity=complexity)
        m.stage = stage_id
        _attach_diff(m, prev, rust, api)
        rows.append(m)
        prev = rust
    return rows


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

    print(
        f"{'stage':<22}{'unsafe (score/KLOC)':<26}{'clippy (lints)':<22}{'LOC':>6}"
        f"  {'changed vs prev':<28}"
    )
    print("-" * 106)
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
        if r.diff:
            ch = (
                f"{r.diff.files_changed}f +{r.diff.lines_added} -{r.diff.lines_removed}"
            )
            if not r.diff.abi_ok:
                ch += " ABI!"
        elif r.diff_error:
            ch = "error"
        else:
            ch = "-"
        print(f"{r.stage:<22}{us:<26}{it:<22}{loc:>6}  {ch:<28}")
    print("-" * 106)
    print(
        "(unsafe: DARPA measure_unsafety, syntactic; clippy lints: fewer = more idiomatic;"
    )
    print(" % is vs the first stage — negative is an improvement;")
    print(
        " changed vs prev: files, lines +added -removed (rustfmt-normalized); ABI! = a"
    )
    print(' public extern "C" fn was removed or re-typed)')


def to_json(rows: list[StageMetrics]) -> list[dict[str, Any]]:
    """JSON-ready rows (public; the orchestrator writes these per run)."""
    return _to_json(rows)


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
        if r.diff:
            d["diff"] = r.diff.to_dict()
        elif r.diff_error:
            d["diff_error"] = r.diff_error.splitlines()[0]
        out.append(d)
    return out


# ---- recording during a run (the orchestrator's ``[metrics]`` hook) ----------
#
# After each successful stage, record_stage() writes stages/NN-<id>/metrics.json
# (unsafe + what the stage changed vs the previous stage). At run end,
# record_run() collects those into <run>/metrics.json, adding clippy on the
# stages ``idiomaticity`` selects and a first-to-final summary. Both are
# fail-open: an error is written into the file and never fails the run.

METRICS_FILE = "metrics.json"


def measure_stage(
    prev: Path | None, rust: Path, *, api: PublicApi | None
) -> dict[str, Any]:
    """Unsafe + diff-vs-previous for one stage's Rust output (no clippy)."""
    m = _measure(rust, do_idiom=False, complexity=False)
    _attach_diff(m, prev, rust, api)
    return _to_json([m])[0]


def record_stage(
    stage_run_dir: Path,
    stage_id: str,
    prev: Path | None,
    rust: Path,
    *,
    api: PublicApi | None,
) -> dict[str, Any] | None:
    """Measure a finished stage and write its ``metrics.json``. Never raises."""
    try:
        row = measure_stage(prev, rust, api=api)
    except Exception as exc:  # fail-open: metrics never sink a run
        row = {"metrics_error": f"{type(exc).__name__}: {exc}"}
    row["stage"] = stage_id
    try:
        (stage_run_dir / METRICS_FILE).write_text(
            json.dumps(row, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        return None
    return row


def _clippy(rust: Path, loc: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        try:
            r = measure_idiomaticity(rust, workdir=Path(td), loc=loc)
        except (IdiomEvalError, OSError) as exc:
            return {
                "idiomaticity_error": str(exc).splitlines()[0] if str(exc) else "error"
            }
    return {
        "idiomaticity": {
            "total": r.total,
            "per_kloc": r.per_kloc,
            "by_group": r.group_totals(),
        }
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [r["unsafe"]["score"] for r in rows if "unsafe" in r]
    lints = [r["idiomaticity"]["total"] for r in rows if "idiomaticity" in r]
    diffs = [r["diff"] for r in rows if "diff" in r]
    return {
        "stages_measured": len(rows),
        "unsafe_first": scores[0] if scores else None,
        "unsafe_final": scores[-1] if scores else None,
        "clippy_first": lints[0] if lints else None,
        "clippy_final": lints[-1] if len(lints) > 1 else None,
        "lines_added": sum(d["lines_added"] for d in diffs),
        "lines_removed": sum(d["lines_removed"] for d in diffs),
        "abi_ok": all(d["abi_ok"] for d in diffs),
    }


def record_run(
    run_dir: Path, *, idiomaticity: str = "first_last", api: PublicApi | None = None
) -> dict[str, Any] | None:
    """Roll the stages' ``metrics.json`` up into ``<run>/metrics.json``, adding
    clippy per ``idiomaticity`` (``first_last`` / ``all`` / ``none``). A stage
    without a recorded row (e.g. a resumed run) is measured now. Never raises."""
    from proctor.testing.vector_compare import stage_rust_outputs

    try:
        outputs = stage_rust_outputs(run_dir)
        rows: list[dict[str, Any]] = []
        prev: Path | None = None
        for i, (stage_id, rust) in enumerate(outputs):
            f = rust.parent.parent / METRICS_FILE
            try:
                row = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                row = record_stage(
                    rust.parent.parent, stage_id, prev, rust, api=api
                ) or {"stage": stage_id}
            last = i == len(outputs) - 1
            if idiomaticity == "all" or (
                idiomaticity == "first_last" and (i == 0 or last)
            ):
                loc = row.get("unsafe", {}).get("total_lines", 0)
                row.update(_clippy(rust, loc))
            rows.append(row)
            prev = rust
        out = {"stages": rows, "summary": _summary(rows)}
        (run_dir / METRICS_FILE).write_text(
            json.dumps(out, indent=2) + "\n", encoding="utf-8"
        )
        return out
    except Exception:  # fail-open
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="metrics",
        description="Measure unsafe + idiomaticity of a Rust translation, or per stage.",
    )
    ap.add_argument(
        "path", type=Path, help="a Cargo project dir, or a run dir with stages/"
    )
    ap.add_argument(
        "--idiomaticity",
        type=lambda s: s.replace("-", "_"),
        choices=("all", "first_last", "none"),
        default="all",
        help="which stages get clippy in a run dir (default all; first_last = the "
        "first and last stage only; none = no build needed)",
    )
    ap.add_argument(
        "--no-idiomaticity",
        action="store_true",
        help="skip clippy (same as --idiomaticity none)",
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
    idiom_mode = "none" if args.no_idiomaticity else args.idiomaticity
    do_idiom = idiom_mode != "none"

    if (path / "Cargo.toml").is_file():
        m = _measure(path, do_idiom=do_idiom, complexity=args.complexity)
        _print_one(path, m)
        rows = [m]
    elif (path / "stages").is_dir():
        rows = measure_run(path, idiomaticity=idiom_mode, complexity=args.complexity)
        if not rows:
            print(f"no stage Rust outputs under {path}/stages", file=sys.stderr)
            return 1
        print(f"per-stage metrics for {path.name}\n")
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
