"""Batch driver: the pipeline across a corpus, one run dir per case
(plan M7). `bench` is just N independent `run`s — same envelopes, same
checkpoints, same reports; there is no separate batch mode for stages.

Corpus layout is convention-over-configuration: a case is any directory
containing the ``[bench.layout]`` subpath for every artifact kind in
``[run] provides`` (defaults: c_project=c, rust_project=c2rust,
test_package=tests, rule_set=rules).

Rule-set policy across cases: ``independent`` only for now — the one
genuine cross-case coupling (cross-program rule learning) arrives with
the chained/merge policies later.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from proctor.config.load import ConfigError
from proctor.config.model import PipelineConfig
from proctor.orchestrator.run import RunError, RunResult, start_run
from proctor.testing.vector_harness import StageVectorResult, VectorComparison

DEFAULT_LAYOUT = {
    "c_project": "c",
    "rust_project": "c2rust",
    "test_package": "tests",
    "rule_set": "rules",
}

_POLICIES = ("independent", "chained", "merge-per-round")


@dataclass(frozen=True)
class BenchSettings:
    jobs: int = 4
    layout: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LAYOUT))
    rule_set_policy: str = "independent"
    verify_vectors: bool = False  # run TRACTOR's harness on the outputs
    verify_all_stages: bool = False  # verify each stage (per-stage comparison)

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> BenchSettings:
        bench_raw = raw.get("bench", {})
        if not isinstance(bench_raw, dict):
            raise ConfigError("[bench] must be a table")
        jobs = bench_raw.get("jobs", 4)
        if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
            raise ConfigError("[bench] jobs must be a positive integer")
        layout = dict(DEFAULT_LAYOUT)
        layout_raw = bench_raw.get("layout", {})
        if not isinstance(layout_raw, dict):
            raise ConfigError("[bench.layout] must be a table")
        for kind, sub in layout_raw.items():
            if kind not in DEFAULT_LAYOUT or not isinstance(sub, str):
                raise ConfigError(
                    f"[bench.layout] {kind!r} must be one of "
                    f"{sorted(DEFAULT_LAYOUT)} mapped to a subpath"
                )
            layout[kind] = sub
        policy = bench_raw.get("rule_set_policy", "independent")
        if policy not in _POLICIES:
            raise ConfigError(f"[bench] rule_set_policy must be one of {_POLICIES}")
        if policy != "independent":
            raise ConfigError(
                f"[bench] rule_set_policy {policy!r} is not implemented yet; "
                f"use 'independent'"
            )
        verify_vectors = bench_raw.get("verify_vectors", False)
        verify_all_stages = bench_raw.get("verify_all_stages", False)
        for key, val in (
            ("verify_vectors", verify_vectors),
            ("verify_all_stages", verify_all_stages),
        ):
            if not isinstance(val, bool):
                raise ConfigError(f"[bench] {key} must be a boolean")
        return cls(
            jobs=jobs,
            layout=layout,
            rule_set_policy=policy,
            verify_vectors=verify_vectors,
            verify_all_stages=verify_all_stages,
        )


@dataclass(frozen=True)
class BenchCase:
    name: str
    inputs: dict[str, Path]
    source_dir: Path | None = None  # corpus case dir (test_vectors, runner, ...)


def discover_cases(
    corpus: Path, provides: tuple[str, ...], layout: dict[str, str]
) -> list[BenchCase]:
    """A case = a directory holding the layout subpath for every
    provided artifact kind."""
    if not corpus.is_dir():
        raise RunError(f"corpus directory {corpus} does not exist")
    cases: list[BenchCase] = []
    for candidate in sorted(p for p in corpus.rglob("*") if p.is_dir()):
        inputs: dict[str, Path] = {}
        for kind in provides:
            sub = candidate / layout[kind]
            if not sub.exists():
                break
            inputs[kind] = sub
        else:
            if provides:
                cases.append(
                    BenchCase(
                        name=str(candidate.relative_to(corpus)),
                        inputs=inputs,
                        source_dir=candidate,
                    )
                )
    # drop nested matches: a case must not contain another case
    names = {c.name for c in cases}
    return [
        c
        for c in cases
        if not any(
            other != c.name and c.name.startswith(other + "/") for other in names
        )
    ]


@dataclass
class BenchOutcome:
    case: BenchCase
    run: RunResult | Exception
    vectors: VectorComparison | None = None

    @property
    def run_ok(self) -> bool:
        return isinstance(self.run, RunResult) and self.run.ok

    @property
    def vectors_ok(self) -> bool | None:
        """None when vectors weren't verified; else True iff the last
        verified stage passed all its vectors."""
        if self.vectors is None or not self.vectors.stages:
            return None
        last = self.vectors.stages[-1]
        return last.report is not None and last.report.ok


@dataclass
class BenchResult:
    bench_dir: Path
    outcomes: list[BenchOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and all(o.run_ok for o in self.outcomes)


def run_bench(
    config: PipelineConfig,
    root: Path,
    corpus: Path,
    *,
    name: str,
    jobs: int | None = None,
    match: str | None = None,
) -> BenchResult:
    settings = BenchSettings.from_config(config.raw)
    cases = discover_cases(corpus, config.run.provides, settings.layout)
    if match is not None:
        try:
            pattern = re.compile(match)
        except re.error as exc:
            raise RunError(f"invalid --match regex {match!r}: {exc}") from exc
        cases = [c for c in cases if pattern.search(c.name)]
    if not cases:
        suffix = f" matching {match!r}" if match else ""
        raise RunError(
            f"no cases found under {corpus}{suffix} for provides="
            f"{list(config.run.provides)} with layout {settings.layout}"
        )

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    bench_dir = root / config.run.output_dir / f"bench-{name}-{stamp}"
    bench_dir.mkdir(parents=True)
    result = BenchResult(bench_dir=bench_dir)
    started = time.monotonic()

    # Vector verification runs in place inside a corpus workspace copy
    # (library/cando cases build their runner from the workspace, so the
    # whole thing must be present). Make one writable copy up front.
    ws_root: Path | None = None
    ws_copy: Path | None = None
    if settings.verify_vectors:
        from proctor.testing.vector_harness import copy_workspace, find_workspace_root

        seed = next((c.source_dir for c in cases if c.source_dir is not None), None)
        if seed is not None:
            ws_root = find_workspace_root(seed)
        if ws_root is not None:
            ws_copy = copy_workspace(ws_root, bench_dir / "_corpus_ws")

    def one(case: BenchCase) -> BenchOutcome:
        run_dir = bench_dir / case.name.replace("/", "__")
        try:
            run = start_run(
                config,
                root,
                name=case.name.replace("/", "__"),
                supplied_inputs=case.inputs,
                config_files=[],
                overrides=[],
                item=case.name,
                run_dir=run_dir,
            )
        except Exception as exc:  # a broken case must not sink the batch
            return BenchOutcome(case=case, run=exc)
        vectors = _verify_case(settings, case, run.run_dir, ws_root, ws_copy)
        return BenchOutcome(case=case, run=run, vectors=vectors)

    workers = jobs if jobs is not None else settings.jobs
    with ThreadPoolExecutor(max_workers=workers) as pool:
        result.outcomes = list(pool.map(one, cases))

    summary = {
        "bench": name,
        "corpus": str(corpus),
        "wall_s": round(time.monotonic() - started, 1),
        "total": len(result.outcomes),
        "ok": sum(1 for o in result.outcomes if o.run_ok),
        "vectors_ok": sum(1 for o in result.outcomes if o.vectors_ok is True)
        if settings.verify_vectors
        else None,
        "cases": [_case_summary(o) for o in result.outcomes],
    }
    (bench_dir / "bench.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _verify_case(
    settings: BenchSettings,
    case: BenchCase,
    run_dir: Path,
    ws_root: Path | None,
    ws_copy: Path | None,
) -> VectorComparison | None:
    """Run TRACTOR's vector harness on the case's stage output(s).

    Prefers the in-place path (inside the corpus workspace copy), which
    is required for library cases and works for binary cases too. Falls
    back to the isolated per-case corpus only when no workspace is
    present (e.g. a binary-only subset), where library cases can't run.
    """
    if not settings.verify_vectors or case.source_dir is None:
        return None
    if not (case.source_dir / "test_vectors").is_dir():
        return None
    from proctor.testing.vector_compare import (
        compare_stages,
        compare_stages_in_place,
        verify_final,
        verify_final_in_place,
    )
    from proctor.testing.vector_harness import is_library_case

    workdir = run_dir / "vectors"

    if ws_root is not None and ws_copy is not None:
        case_rel = case.source_dir.resolve().relative_to(ws_root.resolve())
        case_in_copy = ws_copy / case_rel
        if settings.verify_all_stages:
            return compare_stages_in_place(
                run_dir, case_in_copy, workspace_root=ws_copy, workdir=workdir
            )
        final = verify_final_in_place(
            run_dir, case_in_copy, workspace_root=ws_copy, workdir=workdir
        )
        comparison = VectorComparison(case=case.name)
        if final is not None:
            comparison.stages.append(final)
        return comparison

    # No corpus workspace: isolated path (binary cases only).
    if is_library_case(case.source_dir):
        comparison = VectorComparison(case=case.name)
        comparison.stages.append(
            StageVectorResult(
                stage_id="lib",
                report=None,
                error="library case needs the corpus workspace (none found)",
            )
        )
        return comparison
    if settings.verify_all_stages:
        return compare_stages(run_dir, case.source_dir, workdir=workdir)
    final = verify_final(run_dir, case.source_dir, workdir=workdir)
    comparison = VectorComparison(case=case.name)
    if final is not None:
        comparison.stages.append(final)
    return comparison


def _case_summary(o: BenchOutcome) -> dict[str, Any]:
    run = o.run
    summary: dict[str, Any] = {
        "name": o.case.name,
        "ok": o.run_ok,
        "error": str(run) if isinstance(run, Exception) else None,
        "stages": (
            [
                {
                    "id": s.stage_id,
                    "status": s.status,
                    "duration_s": round(s.duration_s, 2),
                    "error": s.error,
                }
                for s in run.stages
            ]
            if isinstance(run, RunResult)
            else []
        ),
    }
    if o.vectors is not None:
        summary["vectors_ok"] = o.vectors_ok
        summary["vectors"] = [
            {
                "stage": sv.stage_id,
                "passed": sv.report.passed if sv.report else None,
                "failed": sv.report.failed if sv.report else None,
                "skipped": sv.report.skipped if sv.report else None,
                "total": sv.report.total if sv.report else None,
                "build_ok": sv.report.build_ok if sv.report else None,
                "error": sv.error,
            }
            for sv in o.vectors.stages
        ]
    return summary
