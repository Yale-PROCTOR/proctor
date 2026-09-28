"""Combined per-case scorecard for a bench_no_falco run: vectors (from
verify.json) plus the `unsafe` + idiomaticity of the translation. Driven by
./bench_report.sh.

Two views:
  default       one row per case, metrics of the FINAL-stage translation
  --per-stage   a table per case with EVERY stage (c2rust -> crat -> ...) and
                the reduction vs the first stage, plus suite totals

Unsafe is source-only (fast). Idiomaticity runs clippy, which builds each
crate — skip it with --no-idiomaticity for a quick vectors+unsafe view.
Vectors come from verify.json, which records the FINAL translation only.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from proctor.testing.idiomaticity_eval import IdiomEvalError, measure_idiomaticity
from proctor.testing.metrics import StageMetrics, _measure, _pct
from proctor.testing.unsafe_eval import UnsafeEvalError, measure_unsafe
from proctor.testing.vector_compare import stage_rust_outputs


def _vectors_cell(c: dict[str, Any]) -> str:
    total = c.get("passed", 0) + c.get("skipped", 0) + c.get("failed", 0)
    cell = f"{c.get('passed', 0)}/{total}"
    skip = c.get("skipped", 0)
    if skip:
        fs = c.get("fs_skipped", 0)
        cell += f" ({fs} fs-skip)" if fs == skip else f" ({skip} skip)"
    if c.get("failed", 0):
        cell += " FAIL"
    return cell


def _sorted_cases(d: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(d.get("cases", []), key=lambda c: c["case"])


def _header(vj: Path, d: dict[str, Any], note: str) -> None:
    print(f"verify + metrics: {vj}")
    print(
        f"{d.get('suite', '?')}   {d.get('cases_ok', 0)}/{d.get('cases_total', 0)} "
        f"cases clean   {note}"
    )
    print("=" * 78)


def _final_report(vj: Path, d: dict[str, Any], do_idiom: bool) -> int:
    """One row per case: vectors + the final-stage translation's metrics."""
    _header(vj, d, "(metrics: final stage)")
    print(f"{'case':<26}{'vectors':<18}{'unsafe (score/KLOC)':<24}{'clippy':<8}")
    print("-" * 78)

    tot_pass = tot_fail = tot_skip = tot_unsafe = tot_lints = 0
    for c in _sorted_cases(d):
        leaf = c["case"].split("/")[-1]
        tot_pass += c.get("passed", 0)
        tot_fail += c.get("failed", 0)
        tot_skip += c.get("skipped", 0)

        us = it = "-"
        outputs = stage_rust_outputs(vj.parent / leaf)
        if outputs:
            _, final = outputs[-1]
            loc = 0
            try:
                u = measure_unsafe(final)
                us = f"{u.score} ({u.per_kloc}/K)"
                tot_unsafe += u.score
                loc = u.total_lines
            except (UnsafeEvalError, OSError):
                us = "err"
            if do_idiom:
                with tempfile.TemporaryDirectory() as td:
                    try:
                        i = measure_idiomaticity(final, workdir=Path(td), loc=loc)
                        it = str(i.total)
                        tot_lints += i.total
                    except (IdiomEvalError, OSError):
                        it = "skip"
        print(f"{leaf:<26}{_vectors_cell(c):<18}{us:<24}{it:<8}")

    print("-" * 78)
    idiom = f"clippy {tot_lints}" if do_idiom else "clippy (skipped)"
    print(
        f"totals: vectors {tot_pass} pass / {tot_fail} fail / {tot_skip} skip   "
        f"unsafe {tot_unsafe}   {idiom}"
    )
    print(
        "(unsafe/clippy are for each case's FINAL-stage translation; lower is better)"
    )
    return 0


def _print_case_stages(leaf: str, vcell: str, rows: list[StageMetrics]) -> None:
    """Compact per-stage table for one case (no repeated footer)."""
    u_scores = [r.unsafe.score for r in rows if r.unsafe]
    base_u = u_scores[0] if u_scores else 0
    i_totals = [r.idiom.total for r in rows if r.idiom]
    base_i = i_totals[0] if i_totals else 0

    print(f"== {leaf} ==   vectors {vcell}")
    print(f"  {'stage':<22}{'unsafe (score/KLOC)':<26}{'clippy':<18}{'LOC':>6}")
    for r in rows:
        if r.unsafe:
            us = f"{r.unsafe.score} ({r.unsafe.per_kloc}/K){_pct(r.unsafe.score, base_u)}"
            loc = r.unsafe.total_lines
        else:
            us, loc = "err", 0
        if r.idiom:
            it = f"{r.idiom.total}{_pct(r.idiom.total, base_i)}"
        elif r.idiom_error:
            it = "skip"
        else:
            it = "-"
        print(f"  {r.stage:<22}{us:<26}{it:<18}{loc:>6}")
    print()


def _per_stage_report(vj: Path, d: dict[str, Any], do_idiom: bool) -> int:
    """A table per case with every stage, plus suite totals. Vectors (final
    stage only) are shown in each case header."""
    _header(vj, d, "(metrics: every stage; vectors = final stage)")
    tot_pass = tot_fail = tot_skip = 0
    first_u = last_u = first_i = last_i = 0
    for c in _sorted_cases(d):
        leaf = c["case"].split("/")[-1]
        tot_pass += c.get("passed", 0)
        tot_fail += c.get("failed", 0)
        tot_skip += c.get("skipped", 0)

        outputs = stage_rust_outputs(vj.parent / leaf)
        if not outputs:
            print(f"== {leaf} ==   vectors {_vectors_cell(c)}")
            print("  (no stage outputs found)\n")
            continue
        rows = []
        for stage_id, rust in outputs:
            m = _measure(rust, do_idiom=do_idiom, complexity=False)
            m.stage = stage_id
            rows.append(m)
        _print_case_stages(leaf, _vectors_cell(c), rows)

        if rows[0].unsafe and rows[-1].unsafe:
            first_u += rows[0].unsafe.score
            last_u += rows[-1].unsafe.score
        if do_idiom and rows[0].idiom and rows[-1].idiom:
            first_i += rows[0].idiom.total
            last_i += rows[-1].idiom.total

    print("=" * 78)
    print("suite totals (first stage -> final stage):")
    print(f"  unsafe   {first_u} -> {last_u}{_pct(last_u, first_u)}")
    if do_idiom:
        print(f"  clippy   {first_i} -> {last_i}{_pct(last_i, first_i)}")
    else:
        print("  clippy   (skipped)")
    print(
        f"  vectors  {tot_pass} pass / {tot_fail} fail / {tot_skip} skip (final stage)"
    )
    print("(% is vs the first stage; negative is an improvement)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="suite_report")
    ap.add_argument(
        "run_dir", type=Path, help="a bench_no_falco run dir (has verify.json)"
    )
    ap.add_argument(
        "--per-stage",
        action="store_true",
        help="metrics for every stage (c2rust -> crat -> ...), not just the final",
    )
    ap.add_argument(
        "--no-idiomaticity",
        action="store_true",
        help="skip clippy (vectors + unsafe only; no per-case build)",
    )
    args = ap.parse_args(argv)

    vj = args.run_dir / "verify.json"
    if not vj.is_file():
        print(f"no verify.json in {args.run_dir}", file=sys.stderr)
        return 1
    d = json.loads(vj.read_text(encoding="utf-8"))
    do_idiom = not args.no_idiomaticity
    if args.per_stage:
        return _per_stage_report(vj, d, do_idiom)
    return _final_report(vj, d, do_idiom)


if __name__ == "__main__":
    raise SystemExit(main())
