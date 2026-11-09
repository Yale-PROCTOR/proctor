r"""Stacked bars of the lines one pipeline stage changed, one bar per case.

The script runs tools/diff_metrics itself on every pair of crates, building
it first if needed. It does not read existing diff_metrics output, such as the
metrics.json a run writes with `[metrics] after_each_stage`, so the chart
always reflects the crates on disk and the current tool.

Usage (from the repo root; needs rustfmt on PATH):

    # one stage, compared with the stage right before it
    uv run --with matplotlib python graphs/lines_changed.py \
        out/<bench>/<case>/stages/02-abstraction_recovery -o lines.png

    # every case in a bench run, choosing the stage and what to compare with
    uv run --with matplotlib python graphs/lines_changed.py \
        out/<bench> --stage abstraction_recovery --before c2rust -o lines.png

    # any two crates or stage dirs, before then after
    uv run --with matplotlib python graphs/lines_changed.py \
        --pair ~/ex1 ~/ex2 -o lines.png

Each path is a stage dir (.../stages/NN-<id>), a case run dir or a bench dir;
pass several for several bars. Options:

    --pair A B       compare A (before) with B (after), each a crate dir or a
                     stage dir; repeat for more bars. A plain crate has no C
                     headers, so its bar has no C API segment
    --stage ID       stage to chart in case/bench dirs (default: each case's
                     last stage that produced Rust)
    --before ID      stage to compare against (default: the one right before)
    -o, --out PNG    image to write (default: lines_changed.png)
    --json FILE      also write the per-case counts
    --title TEXT     replace the default title

Each bar is the crate's lines before the stage, split into kept and removed,
followed by the lines the stage added. Kept + removed is the size before the
stage; kept + added is the size after. Kept lines are three greys, so colour
stays on what changed:

- darkest: lines that preserve the C API (the signatures of the functions the
  C headers declare, exported globals, and the types the headers define);
- mid: lines of files the stage left identical;
- lightest: the other kept lines of the files it edited.

Lines are counted the way tools/diff_metrics counts them: .rs files outside
target/ and dot dirs, excluding build.rs, rustfmt-formatted, trimmed and
non-blank. Added and removed come from diff_metrics itself.

Each bar ends with how many of the crate's .rs files the stage changed, also
from diff_metrics. The title names the stage compared against.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from proctor.testing.diff_eval import PublicApi, measure_diff, public_api  # noqa: E402

# Reference palette, light mode.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0"
# Kept lines: darkest for the C API, mid for files the stage never touched,
# lightest for the rest of the files it edited. Neighbouring greys are about
# 15 OKLab ΔE apart; the lightest is over 20 from orange and blue.
API, UNTOUCHED, KEPT = "#6f6d68", "#b4b1aa", "#e3e1db"
# Softened blue and orange; adjacent segments stay at least 15 OKLab ΔE apart,
# including under simulated protanopia and deuteranopia.
ADDED, REMOVED = "#5f8fc9", "#e08b60"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 8,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_2,
        "xtick.color": INK_2,
        "ytick.color": INK,
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
    }
)


@dataclass(frozen=True)
class Target:
    """One bar: a crate before and after, with names for the chart."""

    case: str  # bar label
    case_dir: Path | None  # run dir holding inputs/c, if any
    after_name: str  # e.g. the stage id
    before_name: str
    before: Path
    after: Path


@dataclass
class Row:
    case: str
    lines: int  # before the stage
    added: int
    removed: int
    api: int  # kept lines that preserve the C API
    untouched: int  # other kept lines, in files the stage left identical
    files: int  # .rs files in either crate
    files_changed: int

    @property
    def kept(self) -> int:
        return self.lines - self.removed

    @property
    def edited(self) -> int:
        return self.kept - self.api - self.untouched


# ---- finding the stages ------------------------------------------------------


def _rust(stage_dir: Path) -> Path | None:
    rust = stage_dir / "out" / "rust"
    return rust if (rust / "Cargo.toml").is_file() else None


def _stage_id(stage_dir: Path) -> str:
    return stage_dir.name.split("-", 1)[-1]


def _target(stage_dir: Path, before_stage: str | None) -> Target:
    """The stage at ``stage_dir`` and the earlier stage to compare it with:
    ``before_stage`` by id, or else the last earlier stage that produced Rust."""
    after = _rust(stage_dir)
    if after is None:
        raise SystemExit(f"{stage_dir}: no out/rust (the stage produced no crate)")
    earlier = sorted(
        (p for p in stage_dir.parent.iterdir() if p.name < stage_dir.name and _rust(p)),
        key=lambda p: p.name,
    )
    if before_stage is not None:
        earlier = [p for p in earlier if _stage_id(p) == before_stage]
    if not earlier:
        wanted = f"stage {before_stage!r}" if before_stage else "stage"
        raise SystemExit(
            f"{stage_dir}: no earlier {wanted} produced Rust to compare to"
        )
    before_dir = earlier[-1]
    case_dir = stage_dir.parent.parent
    return Target(
        case_dir.name,
        case_dir,
        _stage_id(stage_dir),
        _stage_id(before_dir),
        before_dir / "out" / "rust",
        after,
    )


def _crate(path: Path) -> tuple[Path, str, Path | None]:
    """(crate, name, run dir) for a crate dir or a stage dir."""
    path = path.resolve()
    if (path / "Cargo.toml").is_file():
        return path, path.name, None
    rust = _rust(path)
    if rust is None:
        raise SystemExit(
            f"{path}: neither a crate (Cargo.toml) nor a stage with out/rust"
        )
    return rust, _stage_id(path), path.parent.parent


def pair_target(before: Path, after: Path) -> Target:
    """A bar for two explicit crates or stage dirs, before then after."""
    b, b_name, _ = _crate(before)
    a, a_name, run = _crate(after)
    return Target(run.name if run else a_name, run, a_name, b_name, b, a)


def _pick(case_dir: Path, stage: str | None) -> Path | None:
    """The case's stage dir with id ``stage``, or its last stage with Rust."""
    stages = sorted(p for p in (case_dir / "stages").iterdir() if p.is_dir())
    if stage is not None:
        return next((p for p in stages if _stage_id(p) == stage), None)
    return next((p for p in reversed(stages) if _rust(p)), None)


def targets(
    paths: list[Path], stage: str | None, before_stage: str | None = None
) -> list[Target]:
    """Resolve each path (a stage dir, a case dir, or a bench dir of cases)."""
    out: list[Target] = []
    for path in paths:
        path = path.resolve()
        if path.parent.name == "stages":
            out.append(_target(path, before_stage))
            continue
        cases = (
            [path]
            if (path / "stages").is_dir()
            else sorted(p for p in path.iterdir() if (p / "stages").is_dir())
        )
        if not cases:
            raise SystemExit(f"{path}: not a stage dir, a case dir or a bench dir")
        for case in cases:
            picked = _pick(case, stage)
            if picked is None:
                print(f"skip {case.name}: no {stage or 'Rust'} stage", file=sys.stderr)
            elif _rust(picked) is None:
                print(
                    f"skip {case.name}: {picked.name} produced no Rust (skipped or failed)",
                    file=sys.stderr,
                )
            else:
                out.append(_target(picked, before_stage))
    return out


# ---- counting lines ----------------------------------------------------------


def rs_files(crate: Path) -> list[Path]:
    out = []
    for p in crate.rglob("*.rs"):
        rel = p.relative_to(crate).parts
        if any(part == "target" or part.startswith(".") for part in rel[:-1]):
            continue
        if p.name != "build.rs":
            out.append(p)
    return out


def norm_text(path: Path) -> list[str]:
    """The file's lines as diff_metrics counts them: rustfmt-formatted, trimmed, non-blank."""
    src = path.read_text(errors="replace")
    fmt = subprocess.run(
        ["rustfmt", "--edition", "2021", "--quiet"],
        input=src,
        capture_output=True,
        text=True,
        check=False,
    )
    text = fmt.stdout if fmt.returncode == 0 else src
    return [line.strip() for line in text.splitlines() if line.strip()]


ITEM = re.compile(
    r"^(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?(?:extern\s+\"C\"\s+)?"
    r"(fn|struct|enum|union|type|const|static(?:\s+mut)?|use)\s+(\w+)"
)


def c_api(case_dir: Path, before: Path) -> tuple[PublicApi, set[str]] | None:
    """The case's C API and every word in its headers, or None for a program
    (a crate with a `[[bin]]` target: nothing outside it calls its functions,
    so its headers are only internal interfaces) or a run with no C input."""
    c = case_dir / "inputs" / "c"
    if "[[bin]]" in (before / "Cargo.toml").read_text() or not c.is_dir():
        return None
    api = public_api(c)
    if api is None:
        return None
    words = {
        w
        for h in c.rglob("*.h")
        for w in re.findall(r"\w+", h.read_text(errors="replace"))
    }
    return api, words


def api_lines(lines: list[str], capi: tuple[PublicApi, set[str]] | None) -> list[str]:
    """The lines of one file that preserve the C API: the signature of each
    function the headers declare, each exported global, and the definitions of
    the types the headers define, including typedef aliases, constants and
    enum re-exports whose name is in a header."""
    if capi is None:
        return []
    api, words = capi
    out: list[str] = []
    attrs: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#["):
            attrs.append(line)
            i += 1
            continue
        m = ITEM.match(line)
        kind, name = (m.group(1), m.group(2)) if m else ("", "")
        keep = (
            (
                kind == "fn"
                and name in api.symbols
                and any("no_mangle" in a for a in attrs)
            )
            or (kind.startswith("static") and name in api.symbols)
            or (
                kind in ("struct", "enum", "union")
                and (name in api.types or name in words)
            )
            or (kind in ("type", "const") and name in words)
            or (kind == "use" and (name in api.types or name in words))
        )
        if not keep:
            attrs = []
            i += 1
            continue
        j = i
        if kind == "fn":  # the signature, up to the body's opening brace
            while j < len(lines) and not lines[j].endswith("{"):
                j += 1
        elif kind in ("struct", "enum", "union") and line.endswith("{"):
            depth = 0
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth == 0:
                    break
        else:  # a one-statement item
            while j < len(lines) and not lines[j].endswith(";"):
                j += 1
        out += attrs + lines[i : j + 1]
        attrs = []
        i = j + 1
    return out


def measure(t: Target) -> Row:
    d = measure_diff(t.before, t.after)
    capi = c_api(t.case_dir, t.before) if t.case_dir else None
    lines: list[str] = []
    api: Counter[str] = Counter()
    untouched = api_untouched = 0
    for f in rs_files(t.before):
        text = norm_text(f)
        lines += text
        file_api = api_lines(text, capi)
        api.update(file_api)
        twin = t.after / f.relative_to(t.before)
        if twin.is_file() and twin.read_bytes() == f.read_bytes():
            untouched += len(text)
            api_untouched += len(file_api)
    final = Counter(line for f in rs_files(t.after) for line in norm_text(f))
    kept = len(lines) - d.lines_removed
    # The API lines still in the final crate, capped at what diff_metrics
    # counts as kept. Untouched files' API lines count as API.
    preserved = min(sum((api & final).values()), kept)
    return Row(
        case=t.case,
        lines=len(lines),
        added=d.lines_added,
        removed=d.lines_removed,
        api=preserved,
        untouched=max(0, min(untouched - api_untouched, kept - preserved)),
        files=len(
            {f.relative_to(t.before) for f in rs_files(t.before)}
            | {f.relative_to(t.after) for f in rs_files(t.after)}
        ),
        files_changed=d.files_changed,
    )


# ---- drawing -----------------------------------------------------------------


def draw(rows: list[Row], title: str, out: Path) -> None:
    rows = sorted(rows, key=lambda r: r.lines)
    ys = list(range(len(rows)))
    fig, ax = plt.subplots(figsize=(7.5, 0.24 * len(rows) + 1.3))
    segments = [
        ("kept: preserves the C API", API, [r.api for r in rows]),
        ("kept: files the stage didn't touch", UNTOUCHED, [r.untouched for r in rows]),
        ("kept: other lines in edited files", KEPT, [r.edited for r in rows]),
        ("removed (rewritten or deleted)", REMOVED, [r.removed for r in rows]),
        ("added by the stage", ADDED, [r.added for r in rows]),
    ]
    left = [0] * len(rows)
    for label, color, widths in segments:
        ax.barh(
            ys,
            widths,
            left=left,
            height=0.62,
            color=color,
            label=label,
            edgecolor=SURFACE,
            linewidth=1,
        )
        left = [a + w for a, w in zip(left, widths)]
    for y, r, end in zip(ys, rows, left):
        ax.text(
            end,
            y,
            f"  {r.files_changed}/{r.files} files changed",
            va="center",
            fontsize=6.5,
            color=INK_2,
        )
    ax.set_xlabel("lines in program (rustfmt, non-blank)")
    ax.set_yticks(ys, [r.case for r in rows], fontsize=7)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.margins(x=0.3)
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    # below the axis label, so it never covers a bar or its label; the greys on
    # the first row and the changes under them (columns fill top to bottom)
    handles, labels = ax.get_legend_handles_labels()
    order = [0, 3, 1, 4, 2]
    ax.legend(
        [handles[i] for i in order],
        [labels[i] for i in order],
        loc="upper left",
        bbox_to_anchor=(0, -0.55 / (0.24 * len(rows) + 1.3) - 0.12),
        ncol=3,
        frameon=False,
        fontsize=7,
    )
    ax.set_title(
        f"{title} ({len(rows)} case{'s' if len(rows) != 1 else ''})",
        loc="left",
        fontsize=10,
        color=INK,
        fontweight="bold",
    )
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="stage dirs (…/stages/NN-<id>), case run dirs, or bench dirs",
    )
    ap.add_argument(
        "--stage",
        help="stage id to chart in case/bench dirs (default: the last stage with Rust)",
    )
    ap.add_argument(
        "-o", "--out", type=Path, default=Path("lines_changed.png"), help="PNG to write"
    )
    ap.add_argument(
        "--before",
        metavar="STAGE",
        help="stage id to compare against (default: the stage right before)",
    )
    ap.add_argument(
        "--pair",
        nargs=2,
        action="append",
        type=Path,
        default=[],
        metavar=("BEFORE", "AFTER"),
        help="compare two crate or stage dirs directly; repeat for more bars",
    )
    ap.add_argument("--title", help="chart title (default: lines changed by <stage>)")
    ap.add_argument("--json", type=Path, help="also write the per-case counts as JSON")
    args = ap.parse_args(argv)

    if not args.paths and not args.pair:
        ap.error("give stage/case/bench dirs, or --pair BEFORE AFTER")
    found = targets(args.paths, args.stage, args.before) if args.paths else []
    found += [pair_target(b, a) for b, a in args.pair]
    if not found:
        print("no stage outputs to chart", file=sys.stderr)
        return 1
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(measure, found))
    pairs = sorted({(t.after_name, t.before_name) for t in found})
    title = args.title or "Lines changed by " + ", ".join(
        f"{stage} (vs {before})" for stage, before in pairs
    )
    draw(rows, title, args.out)
    if args.json:
        args.json.write_text(json.dumps([vars(r) for r in rows], indent=1) + "\n")
    print(f"{len(rows)} case(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
