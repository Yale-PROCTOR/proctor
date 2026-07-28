"""Command-line interface for the proctor orchestration framework.

Implemented: validate, stages, run, resume. Coming with later
milestones: report (M3), bench (M7), warmup (M8).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from proctor import __version__
from proctor.config.load import ConfigError, load_config
from proctor.config.model import PipelineConfig
from proctor.orchestrator.run import RunError, RunResult, resume_run, start_run
from proctor.orchestrator.validate import validate_pipeline

if TYPE_CHECKING:
    from proctor.orchestrator.bench import BenchOutcome

_NOT_YET: dict[str, str] = {}

#: artifact kind -> run flag
_INPUT_FLAGS = {
    "c_project": "input_c",
    "rust_project": "input_rust",
    "test_package": "tests",
    "rule_set": "rule_set",
}


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        action="append",
        required=True,
        metavar="FILE",
        help="config file; repeat to overlay (later files win)",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="overrides",
        metavar="PATH=VALUE",
        help="override a config value, e.g. --set stages.crat.enabled=false",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="directory that stage 'uses' paths are relative to (default: cwd)",
    )


def _load_pipeline_config(args: argparse.Namespace) -> PipelineConfig:
    merged = load_config([Path(p) for p in args.config], list(args.overrides))
    return PipelineConfig.from_dict(merged)


def _cmd_validate(args: argparse.Namespace) -> int:
    config = _load_pipeline_config(args)
    result, validated = validate_pipeline(config, args.root)
    for warning in result.warnings:
        print(f"warning: {warning}")
    for error in result.errors:
        print(f"error: {error}")
    if result.ok:
        stage_ids = [v.resolved.id for v in validated]
        print(f"ok: {len(stage_ids)} stage(s) validated: {', '.join(stage_ids)}")
        return 0
    return 1


def _cmd_stages(args: argparse.Namespace) -> int:
    config = _load_pipeline_config(args)
    result, validated = validate_pipeline(config, args.root)
    for stage_id in config.order:
        entry = config.stages[stage_id]
        if not entry.enabled:
            print(f"  - {stage_id}  (disabled)")
            continue
        match = next((v for v in validated if v.resolved.id == stage_id), None)
        if match is None:
            print(f"  ! {stage_id}  (invalid — see 'proctor validate')")
            continue
        manifest = match.manifest
        produces = ", ".join(manifest.produced_kinds()) or "nothing"
        print(
            f"  * {stage_id}  v{manifest.version}  "
            f"[{match.resolved.stage_dir}]  produces: {produces}"
        )
    return 0 if result.ok else 1


def _print_run_result(result: RunResult) -> None:
    print(f"run: {result.run_dir}")
    for stage in result.stages:
        line = f"  {stage.status:8s} {stage.stage_id}"
        if stage.duration_s:
            line += f"  ({stage.duration_s:.1f}s)"
        if stage.error:
            line += f"  — {stage.error}"
        print(line)
    if result.ok and "rust_project" in result.final:
        print(f"final rust project: {result.final['rust_project']}")
    print("ok" if result.ok else "FAILED")


def _cmd_run(args: argparse.Namespace) -> int:
    config = _load_pipeline_config(args)

    supplied: dict[str, Path] = {}
    for kind, attr in _INPUT_FLAGS.items():
        value = getattr(args, attr)
        if value is not None:
            supplied[kind] = value.resolve()

    for kind in config.run.provides:
        if kind not in supplied:
            flag = "--" + _INPUT_FLAGS[kind].replace("_", "-")
            print(
                f"error: [run] provides declares '{kind}' but {flag} was not given",
                file=sys.stderr,
            )
            return 1
    for kind in supplied:
        if kind not in config.run.provides:
            print(
                f"warning: '{kind}' supplied but not in [run] provides; "
                f"stages requiring it will still fail validation"
            )

    name = args.name or Path(args.config[0]).stem
    result = start_run(
        config,
        args.root,
        name=name,
        supplied_inputs=supplied,
        config_files=[Path(p) for p in args.config],
        overrides=list(args.overrides),
        item=args.item,
    )
    _print_run_result(result)
    return 0 if result.ok else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    result = resume_run(args.run_dir.resolve(), args.root, force_from=args.from_stage)
    _print_run_result(result)
    return 0 if result.ok else 1


def _cmd_bench(args: argparse.Namespace) -> int:
    from proctor.orchestrator.bench import run_bench
    from proctor.orchestrator.run import RunResult

    config = _load_pipeline_config(args)
    name = args.name or Path(args.config[0]).stem
    result = run_bench(
        config, args.root, args.corpus.resolve(), name=name, jobs=args.jobs
    )
    ok_count = 0
    for o in result.outcomes:
        run = o.run
        vec = _fmt_vectors(o)
        if isinstance(run, RunResult) and run.ok:
            ok_count += 1
            statuses = ",".join(s.status for s in run.stages)
            print(f"  ok      {o.case.name}  [{statuses}]{vec}")
        elif isinstance(run, RunResult):
            failed = next((s for s in run.stages if s.status == "failure"), None)
            detail = f"{failed.stage_id}: {failed.error}" if failed else "?"
            print(f"  FAILED  {o.case.name}  — {detail}{vec}")
        else:
            print(f"  ERROR   {o.case.name}  — {run}")
    print(f"{ok_count}/{len(result.outcomes)} cases ok — {result.bench_dir}")
    return 0 if result.ok else 1


def _fmt_vectors(o: BenchOutcome) -> str:
    """Compact per-case vector summary for the bench line, e.g.
    '  vectors 3/3 (crat)'. Empty when vectors weren't verified."""
    if o.vectors is None or not o.vectors.stages:
        return ""
    last = o.vectors.stages[-1]
    if last.report is None:
        return f"  vectors ERROR ({last.stage_id}: {last.error})"
    r = last.report
    tail = "" if r.build_ok else " build-fail"
    return f"  vectors {r.passed}/{r.total} ({last.stage_id}){tail}"


def _cmd_report(args: argparse.Namespace) -> int:
    from proctor.usage.report import (
        aggregate,
        collect,
        render_csv,
        render_json,
        render_table,
    )

    records = collect([Path(p) for p in args.paths])
    group_by = [f.strip() for f in args.group_by.split(",") if f.strip()]
    rows = aggregate(records, group_by)
    renderers = {"table": render_table, "csv": render_csv, "json": render_json}
    print(renderers[args.format](rows))
    return 0


def _cmd_warmup(args: argparse.Namespace) -> int:
    """Resolve stage venvs, run stage warmup commands, build the index
    crate — so runs (and container image builds) start warm."""
    import shutil as _shutil
    import subprocess

    config = _load_pipeline_config(args)
    result, validated = validate_pipeline(config, args.root)
    for error in result.errors:
        print(f"error: {error}")
    if not result.ok:
        return 1

    failed = False
    for v in validated:
        stage_dir = v.resolved.stage_dir
        if (stage_dir / "pyproject.toml").is_file() and _shutil.which("uv"):
            print(f"warmup {v.resolved.id}: uv sync")
            sync = subprocess.run(
                ["uv", "sync", "--project", str(stage_dir)],
                capture_output=True,
                text=True,
            )
            if sync.returncode != 0:
                print(f"error: uv sync failed for {v.resolved.id}:\n{sync.stderr}")
                failed = True
        if v.manifest.warmup:
            print(f"warmup {v.resolved.id}: {' '.join(v.manifest.warmup)}")
            warm = subprocess.run(
                list(v.manifest.warmup),
                cwd=stage_dir,
                capture_output=True,
                text=True,
            )
            if warm.returncode != 0:
                print(
                    f"error: warmup failed for {v.resolved.id} "
                    f"(exit {warm.returncode}):\n{warm.stderr[-2000:]}"
                )
                failed = True

    if _shutil.which("cargo"):
        from proctor.context.index import IndexError_, ensure_index_binary

        print("warmup framework: building proctor-rust-index")
        try:
            ensure_index_binary()
        except IndexError_ as exc:
            print(f"error: {exc}")
            failed = True
    return 1 if failed else 0


def _cmd_not_yet(verb: str, milestone: str) -> int:
    print(f"proctor {verb} arrives with {milestone}; not implemented yet.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proctor",
        description="Orchestration framework for the PROCTOR pipeline.",
    )
    parser.add_argument("--version", action="version", version=f"proctor {__version__}")
    subparsers = parser.add_subparsers(dest="verb", required=True)

    validate = subparsers.add_parser(
        "validate", help="check the pipeline config and stage manifests"
    )
    _add_config_args(validate)
    validate.set_defaults(func=_cmd_validate)

    stages = subparsers.add_parser("stages", help="list the configured pipeline stages")
    _add_config_args(stages)
    stages.set_defaults(func=_cmd_stages)

    run = subparsers.add_parser("run", help="run the pipeline on one test case")
    _add_config_args(run)
    run.add_argument("--input-c", type=Path, help="C project directory")
    run.add_argument("--input-rust", type=Path, help="Rust project directory")
    run.add_argument("--tests", type=Path, help="test package directory")
    run.add_argument("--rule-set", type=Path, help="rule-set file")
    run.add_argument("--name", help="run name prefix (default: config file stem)")
    run.add_argument("--item", help="test-case label recorded in envelopes")
    run.set_defaults(func=_cmd_run)

    resume = subparsers.add_parser("resume", help="resume a partially completed run")
    resume.add_argument("run_dir", type=Path, help="path to runs/<run_id>")
    resume.add_argument(
        "--from",
        dest="from_stage",
        metavar="STAGE",
        help="force re-execution from this stage even if checkpoints match",
    )
    resume.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="directory that stage 'uses' paths are relative to (default: cwd)",
    )
    resume.set_defaults(func=_cmd_resume)

    bench = subparsers.add_parser(
        "bench", help="run the pipeline across a corpus of test cases"
    )
    _add_config_args(bench)
    bench.add_argument("--corpus", type=Path, required=True)
    bench.add_argument("--jobs", type=int, default=None, help="parallel cases")
    bench.add_argument("--name", help="bench name (default: config file stem)")
    bench.set_defaults(func=_cmd_bench)

    report = subparsers.add_parser(
        "report", help="aggregate LLM usage over run directories"
    )
    report.add_argument("paths", nargs="+", help="run dirs or usage.jsonl files")
    report.add_argument(
        "--group-by",
        default="stage,model",
        help="comma-separated record fields (default: stage,model)",
    )
    report.add_argument("--format", choices=["table", "csv", "json"], default="table")
    report.set_defaults(func=_cmd_report)

    warmup = subparsers.add_parser(
        "warmup", help="pre-build stage venvs, adapters, and the index crate"
    )
    _add_config_args(warmup)
    warmup.set_defaults(func=_cmd_warmup)

    for verb, milestone in _NOT_YET.items():
        stub = subparsers.add_parser(verb, help=f"(arrives with {milestone})")
        stub.set_defaults(func=lambda _args, v=verb, m=milestone: _cmd_not_yet(v, m))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except (ConfigError, RunError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
