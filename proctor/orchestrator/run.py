"""The pipeline runner: sequencing, artifact wiring, checkpoint/resume.

One run = one test case through the configured pipeline. The runner
copies the run inputs into the run directory (self-contained, relocatable),
then walks the enabled stages in order, materializing an envelope pair
per stage and threading artifacts from each stage's outputs into the
next stage's inputs.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import tomli_w

from proctor.config.model import PipelineConfig
from proctor.orchestrator.checkpoint import (
    chain_key,
    config_hash,
    read_checkpoint,
    stage_fingerprint,
    write_checkpoint,
)
from proctor.orchestrator.events import EventLog
from proctor.orchestrator.invoke import invoke_stage
from proctor.orchestrator.record import build_run_record, write_run_record
from proctor.orchestrator.validate import ValidatedStage, validate_pipeline
from proctor.contracts.stage_io import (
    ContractError,
    FrameworkSettings,
    InputArtifacts,
    OutputDestinations,
    StageInput,
    StageOutput,
)

#: artifact kind -> subdirectory name under runs/<id>/inputs/
_INPUT_DIRS = {
    "c_project": "c",
    "rust_project": "rust",
    "test_package": "tests",
    "rule_set": "rule_set",
}

if TYPE_CHECKING:
    from proctor.testing.diff_eval import PublicApi


class RunError(Exception):
    """The run could not be started or completed."""


@dataclass(frozen=True)
class StageResult:
    stage_id: str
    status: str  # success | failure | skipped | reused
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    stages: list[StageResult] = field(default_factory=list)
    final: dict[str, Path] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(s.status in ("success", "skipped", "reused") for s in self.stages)


def make_run_id(name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{name}-{stamp}-{uuid.uuid4().hex[:6]}"


def record_inputs(run_dir: Path, supplied: dict[str, Path]) -> dict[str, Path]:
    """Copy run inputs into the run dir so it is self-contained."""
    recorded: dict[str, Path] = {}
    for kind, src in supplied.items():
        dst = run_dir / "inputs" / _INPUT_DIRS[kind]
        if dst.exists():
            recorded[kind] = dst
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            # Skip cargo build output: it's never a real input, it's large, and
            # its 0600 incremental *.lock files (left in a corpus slot by a prior
            # verify) are unreadable to the container's uid and abort the copy.
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("target"))
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            raise RunError(f"input {kind} path {src} does not exist")
        recorded[kind] = dst
    return recorded


def _stage_dests(stage_run_dir: Path, validated: ValidatedStage) -> OutputDestinations:
    produces = validated.manifest.produces
    return OutputDestinations(
        rust_project=(stage_run_dir / "out" / "rust")
        if produces.get("rust_project")
        else None,
        rule_set=(stage_run_dir / "out" / "rule_set")
        if produces.get("rule_set")
        else None,
        artifacts_dir=stage_run_dir / "out" / "artifacts",
    )


def _stage_inputs(state: dict[str, Path], validated: ValidatedStage) -> InputArtifacts:
    def pick(kind: str) -> Path | None:
        if validated.manifest.requirement(kind) == "unused":
            return None
        return state.get(kind)

    return InputArtifacts(
        c_project=pick("c_project"),
        rust_project=pick("rust_project"),
        test_package=pick("test_package"),
        rule_set=pick("rule_set"),
    )


def _produced_state(
    dests: OutputDestinations,
    output: StageOutput,
    validated: ValidatedStage,
) -> tuple[dict[str, Path], str | None]:
    """New artifact paths from a successful stage; error message when a
    declared output is missing."""
    produced: dict[str, Path] = {}
    for kind in validated.manifest.produced_kinds():
        reported = getattr(output.outputs, kind, None)
        dest = getattr(dests, kind)
        path = reported if reported is not None else dest
        if path is None:
            return {}, f"stage declares produces.{kind} but reported no path"
        if kind == "rust_project" and not path.is_dir():
            return {}, f"declared output {kind} missing at {path}"
        if kind == "rule_set" and not path.is_file():
            return {}, f"declared output {kind} missing at {path}"
        produced[kind] = path
    return produced, None


def _maybe_gate_on_tests(
    config: PipelineConfig,
    events: EventLog,
    stage_id: str,
    produced: dict[str, Path],
    state: dict[str, Path],
) -> str | None:
    """Run the test package after a stage when ``[testing]
    after_each_stage`` is on; a failure gates the pipeline. Returns an
    error string on failure, None to proceed."""
    if not config.testing.after_each_stage:
        return None
    project = produced.get("rust_project")
    test_package = state.get("test_package")
    if project is None or test_package is None:
        return None
    from proctor.testing.runner import TestRunnerError, run_tests

    try:
        result = run_tests(project, test_package, profile=config.testing.profile)
    except TestRunnerError as exc:
        events.emit("test_result", stage_id=stage_id, ok=False, error=str(exc))
        return f"post-stage test setup invalid: {exc}"
    events.emit(
        "test_result",
        stage_id=stage_id,
        ok=result.ok,
        build_ok=result.build_ok,
        exit_code=result.exit_code,
        duration_s=round(result.duration_s, 2),
    )
    if not result.ok:
        detail = "build failed" if not result.build_ok else "tests failed"
        tail = (result.stderr or result.stdout)[-500:]
        return f"post-stage gate: {detail} (exit {result.exit_code}): {tail}"
    return None


def _metrics_api(config: PipelineConfig, state: dict[str, Path]) -> PublicApi | None:
    """The C headers' public API, used to scope the per-stage ABI diff."""
    c_project = state.get("c_project")
    if not config.metrics.after_each_stage or c_project is None:
        return None
    from proctor.testing.diff_eval import public_api

    try:
        return public_api(c_project)
    except OSError:
        return None  # unscoped: stricter, never falsely clean


def _finish_metrics(
    config: PipelineConfig, run_dir: Path, api: PublicApi | None
) -> None:
    if config.metrics.after_each_stage:
        from proctor.testing.metrics import record_run

        record_run(run_dir, idiomaticity=config.metrics.idiomaticity, api=api)


def execute_run(
    config: PipelineConfig,
    root: Path,
    run_dir: Path,
    run_id: str,
    supplied_inputs: dict[str, Path],
    *,
    item: str | None = None,
    resume: bool = False,
    force_from: str | None = None,
) -> RunResult:
    result_summary = RunResult(run_id=run_id, run_dir=run_dir)
    events = EventLog(run_dir / "events.jsonl")

    validation, validated_stages = validate_pipeline(config, root)
    if not validation.ok:
        raise RunError("pipeline invalid:\n" + "\n".join(validation.errors))

    state = record_inputs(run_dir, supplied_inputs)
    events.emit("run_started", run_id=run_id, resume=resume)
    metrics_api = _metrics_api(config, state)

    prompts_dir = root / "proctor" / "prompts" / "templates"
    upstream_key: str | None = None
    reuse_active = resume

    for validated in validated_stages:
        stage_id = validated.resolved.id
        entry = validated.resolved.entry
        index = validated.resolved.index
        stage_run_dir = run_dir / "stages" / f"{index:02d}-{stage_id}"

        fingerprint = stage_fingerprint(validated.resolved.stage_dir)
        cfg_hash = config_hash(entry.config, validated.resolved.llm, entry.timeout_s)
        key = chain_key(stage_id, fingerprint, cfg_hash, upstream_key)
        upstream_key = key

        if force_from is not None and stage_id == force_from:
            reuse_active = False

        if reuse_active:
            checkpoint = read_checkpoint(stage_run_dir)
            if checkpoint == (key, "success") or checkpoint == (key, "skipped"):
                status = checkpoint[1]
                if status == "success":
                    dests = _stage_dests(stage_run_dir, validated)
                    try:
                        output = StageOutput.read(stage_run_dir / "stage_output.json")
                    except ContractError as exc:
                        raise RunError(
                            f"cannot resume: {stage_run_dir} has an invalid "
                            f"stage_output.json ({exc})"
                        ) from exc
                    produced, missing = _produced_state(dests, output, validated)
                    if missing:
                        raise RunError(f"cannot resume '{stage_id}': {missing}")
                    state.update(produced)
                events.emit("stage_reused", stage_id=stage_id, checkpoint=key[:12])
                result_summary.stages.append(
                    StageResult(stage_id=stage_id, status="reused")
                )
                continue
            reuse_active = False

        # (Re)execute: clear any stale stage dir from a previous attempt.
        if stage_run_dir.exists():
            shutil.rmtree(stage_run_dir)
        (stage_run_dir / "work").mkdir(parents=True)
        dests = _stage_dests(stage_run_dir, validated)
        assert dests.artifacts_dir is not None
        dests.artifacts_dir.mkdir(parents=True)

        inputs = _stage_inputs(state, validated)
        for kind, level in validated.manifest.requires.items():
            if level == "required" and getattr(inputs, kind) is None:
                raise RunError(
                    f"stage '{stage_id}' requires '{kind}' but it is not "
                    f"available at this point in the run"
                )

        stage_input = StageInput(
            run_id=run_id,
            stage_id=stage_id,
            stage_index=index,
            item=item,
            inputs=inputs,
            outputs=dests,
            config=entry.config,
            framework=FrameworkSettings(
                llm=validated.resolved.llm,
                usage_log=stage_run_dir / "usage.jsonl",
                prompt_library=prompts_dir if prompts_dir.is_dir() else None,
                workdir=stage_run_dir / "work",
                timeout_s=entry.timeout_s,
            ),
        )
        input_file = stage_run_dir / "stage_input.json"
        output_file = stage_run_dir / "stage_output.json"
        stage_input.write(input_file)

        events.emit("stage_started", stage_id=stage_id, index=index)
        invocation = invoke_stage(
            validated.resolved.stage_dir,
            validated.manifest.exec,
            input_file,
            output_file,
            stdout_log=stage_run_dir / "stdout.log",
            stderr_log=stage_run_dir / "stderr.log",
            timeout_s=entry.timeout_s,
        )

        error: str | None = None
        status = "failure"
        if invocation.timed_out:
            error = f"timed out after {entry.timeout_s}s (process group killed)"
        else:
            try:
                output = StageOutput.read(output_file)
            except ContractError as exc:
                error = (
                    f"stage exited with code {invocation.returncode} and no "
                    f"valid stage_output.json: {exc}"
                )
            else:
                if output.status == "failure":
                    error = output.error
                elif invocation.returncode != 0:
                    error = (
                        f"stage reported '{output.status}' but exited with "
                        f"code {invocation.returncode}"
                    )
                elif output.status == "success":
                    produced, missing = _produced_state(dests, output, validated)
                    if missing:
                        error = missing
                    else:
                        gate_error = _maybe_gate_on_tests(
                            config, events, stage_id, produced, state
                        )
                        if gate_error:
                            error = gate_error
                        else:
                            if (
                                config.metrics.after_each_stage
                                and "rust_project" in produced
                            ):
                                from proctor.testing.metrics import record_stage

                                row = record_stage(
                                    stage_run_dir,
                                    stage_id,
                                    state.get("rust_project"),
                                    produced["rust_project"],
                                    api=metrics_api,
                                )
                                events.emit(
                                    "stage_metrics",
                                    stage_id=stage_id,
                                    ok=row is not None and "metrics_error" not in row,
                                )
                            state.update(produced)
                            status = "success"
                else:  # skipped: forward inputs unchanged
                    status = "skipped"

        write_checkpoint(stage_run_dir, key, status)
        result_summary.stages.append(
            StageResult(
                stage_id=stage_id,
                status=status,
                duration_s=invocation.duration_s,
                error=error,
            )
        )
        events.emit(
            "stage_finished",
            stage_id=stage_id,
            status=status,
            duration_s=round(invocation.duration_s, 3),
            error=error,
        )

        if status == "failure" and config.run.on_stage_failure == "stop":
            _finish_metrics(config, run_dir, metrics_api)
            events.emit("run_finished", run_id=run_id, ok=False)
            result_summary.final = dict(state)
            return result_summary

    result_summary.final = dict(state)
    _finish_metrics(config, run_dir, metrics_api)
    events.emit("run_finished", run_id=run_id, ok=result_summary.ok)
    return result_summary


def start_run(
    config: PipelineConfig,
    root: Path,
    *,
    name: str,
    supplied_inputs: dict[str, Path],
    config_files: list[Path],
    overrides: list[str],
    item: str | None = None,
    run_dir: Path | None = None,
) -> RunResult:
    """Create a fresh run directory and execute the pipeline in it.

    ``run_dir`` overrides the default ``<output_dir>/<generated id>``
    location — the batch driver uses this to group case runs.
    """
    if run_dir is None:
        run_id = make_run_id(name)
        run_dir = root / config.run.output_dir / run_id
    else:
        run_id = run_dir.name
    run_dir.mkdir(parents=True)

    (run_dir / "run.toml").write_text(tomli_w.dumps(config.raw), encoding="utf-8")

    _, validated_stages = validate_pipeline(config, root)
    stage_records = [
        {
            "id": v.resolved.id,
            "dir": str(v.resolved.stage_dir),
            "version": v.manifest.version,
            "fingerprint": stage_fingerprint(v.resolved.stage_dir),
        }
        for v in validated_stages
    ]
    record = build_run_record(
        run_id=run_id,
        run_dir=run_dir,
        framework_root=root,
        config_files=config_files,
        overrides=overrides,
        resolved_config=config.raw,
        stages=stage_records,
        inputs={kind: f"inputs/{_INPUT_DIRS[kind]}" for kind in supplied_inputs},
        item=item,
    )
    write_run_record(run_dir, record)

    return execute_run(config, root, run_dir, run_id, supplied_inputs, item=item)


def resume_run(
    run_dir: Path,
    root: Path,
    *,
    force_from: str | None = None,
) -> RunResult:
    """Resume a partially completed run from its own recorded state."""
    import json
    import tomllib

    run_toml = run_dir / "run.toml"
    run_json = run_dir / "run.json"
    if not run_toml.is_file() or not run_json.is_file():
        raise RunError(f"{run_dir} is not a run directory (missing run.toml/run.json)")

    config = PipelineConfig.from_dict(
        tomllib.loads(run_toml.read_text(encoding="utf-8"))
    )
    record = json.loads(run_json.read_text(encoding="utf-8"))
    run_id = record.get("run_id", run_dir.name)
    item = record.get("item")

    inputs_record = record.get("inputs", {})
    supplied: dict[str, Path] = {}
    for kind, rel in inputs_record.items():
        if isinstance(rel, str):
            path = run_dir / rel if not Path(rel).is_absolute() else Path(rel)
            supplied[kind] = path

    return execute_run(
        config,
        root,
        run_dir,
        run_id,
        supplied,
        item=item if isinstance(item, str) else None,
        resume=True,
        force_from=force_from,
    )
