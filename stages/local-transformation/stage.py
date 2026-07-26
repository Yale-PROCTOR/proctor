from __future__ import annotations

import os
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Literal, cast

from model import (
    ContextOverflow,
    ItemRecord,
    SkeletonError,
    dependency_context,
    function_graph,
    leaf_schedule,
    load_skeletons,
    render_transformation_targets,
)
from protocol import (
    PromptRenderInput,
    extract_code_block,
    llm_request,
    render_prompt,
    replacement_request,
    validation_request,
)
from tooling import (
    CommandResult,
    CratTools,
    StageFailure,
    install_candidate_transaction,
    write_json,
)

from proctor.contracts import (
    ModelInfo,
    OutputDestinations,
    PromptUse,
    StageInput,
    StageOutput,
    UsageSummary,
)
from proctor.llm.client import LlmClient
from proctor.llm.types import Response
from proctor.usage.pricing import PricingTable
from proctor.usage.tracker import UsageTracker, read_usage

STAGE_ID = "local_transformation"
STAGE_VERSION = "0.1.0"
MAX_REPAIRS = 10
CONTEXT_LIMIT = 100_000


@dataclass
class Metrics:
    function_count: int = 0
    scc_count: int = 0
    llm_generation_calls: int = 0
    repair_calls: int = 0
    structural_failures: int = 0
    compilation_failures: int = 0
    cargo_builds: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "function_count": self.function_count,
            "scc_count": self.scc_count,
            "llm_generation_calls": self.llm_generation_calls,
            "repair_calls": self.repair_calls,
            "structural_failures": self.structural_failures,
            "compilation_failures": self.compilation_failures,
            "cargo_builds": self.cargo_builds,
        }


@dataclass
class RunState:
    metrics: Metrics = field(default_factory=Metrics)
    config_used: dict[str, Any] = field(default_factory=dict)
    usage_path: Path | None = None
    usage_start: int = 0
    prompt_used: bool = False
    logs: tuple[str, ...] = ()


def _effective_config(config: dict[str, Any], stage_dir: Path) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise StageFailure("stage config must be an object")
    unknown = sorted(set(config) - {"crat_dir", "dump_llm_exchanges"})
    if unknown:
        raise StageFailure(f"unknown local-transformation config keys: {unknown}")
    crat_dir = config.get("crat_dir", "../crat")
    if not isinstance(crat_dir, str) or not crat_dir:
        raise StageFailure("config.crat_dir must be a nonempty string")
    dump_llm_exchanges = config.get("dump_llm_exchanges", False)
    if not isinstance(dump_llm_exchanges, bool):
        raise StageFailure("config.dump_llm_exchanges must be a boolean")
    return {
        "crat_dir": str((stage_dir / crat_dir).resolve()),
        "dump_llm_exchanges": dump_llm_exchanges,
    }


def _library_relative_path(project: Path) -> Path:
    manifest = project / "Cargo.toml"
    if not manifest.is_file():
        raise StageFailure(f"input Rust project has no Cargo.toml: {manifest}")
    try:
        cargo = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise StageFailure(f"input Cargo.toml is invalid: {exc}") from exc
    lib = cargo.get("lib")
    if not isinstance(lib, dict) or "path" not in lib:
        raise StageFailure("input Cargo.toml must declare [lib].path")
    raw = lib["path"]
    if not isinstance(raw, str):
        raise StageFailure("Cargo [lib].path must be a string")
    path = PurePath(raw)
    if path.is_absolute():
        raise StageFailure("Cargo [lib].path must be relative")
    components = path.parts
    if ".." in components:
        raise StageFailure("Cargo [lib].path must not contain '..'")
    names = [component for component in components if component not in {"", "."}]
    if len(names) != 1:
        raise StageFailure("Cargo [lib].path must name one root-level source file")
    relative = Path(names[0])
    source = project / relative
    if not source.is_file():
        raise StageFailure(f"Cargo library source is not a regular file: {source}")
    return relative


def _validate_boundaries(
    stage_input: StageInput, stage_dir: Path
) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    config = _effective_config(stage_input.config, stage_dir)
    source = stage_input.inputs.rust_project
    destination = stage_input.outputs.rust_project
    workdir = stage_input.framework.workdir
    if source is None:
        raise StageFailure("inputs.rust_project is required")
    if destination is None:
        raise StageFailure("outputs.rust_project is required")
    if workdir is None:
        raise StageFailure("framework.workdir is required")
    if not source.is_dir():
        raise StageFailure(f"input Rust project is not a directory: {source}")
    if destination.exists() or destination.is_symlink():
        raise StageFailure(
            f"output Rust project destination already exists: {destination}"
        )
    source_resolved = source.resolve()
    writable_paths = {
        "outputs.rust_project": destination.resolve(),
        "framework.workdir": workdir.resolve(),
        "config.crat_dir": Path(config["crat_dir"]).resolve(),
    }
    if stage_input.outputs.artifacts_dir is not None:
        writable_paths["outputs.artifacts_dir"] = (
            stage_input.outputs.artifacts_dir.resolve()
        )
    if stage_input.framework.usage_log is not None:
        writable_paths["framework.usage_log"] = (
            stage_input.framework.usage_log.resolve()
        )
    for name, path in writable_paths.items():
        if (
            path == source_resolved
            or path in source_resolved.parents
            or source_resolved in path.parents
        ):
            raise StageFailure(f"{name} overlaps input Rust project: {path}")
    library_relative = _library_relative_path(source)
    return source, destination, workdir, library_relative, config


def _copy_final(current: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {"target"} if Path(directory) == current else set()

    shutil.copytree(current, destination, ignore=ignore)


def _usage_summary(records: list[dict[str, Any]]) -> UsageSummary | None:
    if not records:
        return None
    input_tokens = sum(int(record.get("input_tokens", 0)) for record in records)
    cached = sum(int(record.get("cached_input_tokens", 0)) for record in records)
    output = sum(int(record.get("output_tokens", 0)) for record in records)
    reasoning_values = [
        record.get("reasoning_tokens")
        for record in records
        if record.get("reasoning_tokens") is not None
    ]
    reasoning = (
        sum(cast(int, value) for value in reasoning_values)
        if reasoning_values
        else None
    )
    token_bearing_unknown = any(
        record.get("cost_usd") is None
        and (
            int(record.get("input_tokens", 0))
            or int(record.get("cached_input_tokens", 0))
            or int(record.get("output_tokens", 0))
        )
        for record in records
    )
    cost = None
    if not token_bearing_unknown:
        cost = sum(float(record.get("cost_usd") or 0.0) for record in records)
    return UsageSummary(
        calls=len(records),
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output,
        reasoning_tokens=reasoning,
        cost_usd=cost,
    )


def _models(records: list[dict[str, Any]]) -> tuple[ModelInfo, ...]:
    seen: set[tuple[str, str]] = set()
    result = []
    for record in records:
        pair = (str(record.get("provider", "")), str(record.get("model", "")))
        if pair not in seen and all(pair):
            seen.add(pair)
            result.append(ModelInfo(provider=pair[0], model=pair[1]))
    return tuple(result)


def _output(
    status: Literal["success", "failure"],
    state: RunState,
    *,
    destination: Path | None = None,
    error: str | None = None,
) -> StageOutput:
    records: list[dict[str, Any]] = []
    if state.usage_path is not None:
        records = read_usage(state.usage_path)[state.usage_start :]
    return StageOutput(
        status=status,
        stage_id=STAGE_ID,
        stage_version=STAGE_VERSION,
        outputs=OutputDestinations(
            rust_project=destination if status == "success" else None
        ),
        config_used=state.config_used,
        models=_models(records),
        usage=_usage_summary(records),
        prompts=(
            (PromptUse(id="local_transformation", version=1),)
            if state.prompt_used
            else ()
        ),
        metrics=state.metrics.to_dict(),
        logs=state.logs,
        error=error,
    )


def _validate_validator_response(value: dict[str, Any]) -> str:
    def require_exact_keys(
        object_name: str, actual: dict[str, Any], expected: set[str]
    ) -> None:
        if set(actual) != expected:
            raise StageFailure(
                f"validator {object_name} must contain exactly {sorted(expected)}"
            )

    def require_text(object_name: str, field: str, item: dict[str, Any]) -> None:
        if not isinstance(item.get(field), str):
            raise StageFailure(f"validator {object_name}.{field} must be a string")

    version = value.get("schema_version")
    if isinstance(version, bool) or version != 1:
        raise StageFailure(f"unsupported validator response schema_version {version!r}")
    status = value.get("status")
    if not isinstance(status, str) or status not in {
        "valid",
        "invalid",
        "setup_error",
    }:
        raise StageFailure(f"unknown validator response status {status!r}")
    if status == "valid":
        require_exact_keys("valid response", value, {"schema_version", "status"})
        return status
    if status == "setup_error":
        error = value.get("error")
        require_exact_keys(
            "setup_error response", value, {"schema_version", "status", "error"}
        )
        if not isinstance(error, dict):
            raise StageFailure("validator setup_error.error must be an object")
        require_exact_keys("setup_error.error", error, {"code", "message"})
        require_text("setup_error.error", "code", error)
        require_text("setup_error.error", "message", error)
        raise StageFailure(f"validator setup_error: {error!r}")
    require_exact_keys(
        "invalid response", value, {"schema_version", "status", "failures"}
    )
    failures = value.get("failures")
    if not isinstance(failures, list) or not failures:
        raise StageFailure(
            "validator invalid response failures must be a nonempty array"
        )
    for failure_index, failure in enumerate(failures):
        object_name = f"failure[{failure_index}]"
        if not isinstance(failure, dict):
            raise StageFailure(f"validator {object_name} must be an object")
        require_exact_keys(
            object_name, failure, {"id", "name", "failed_snippet", "errors"}
        )
        item_id = failure.get("id")
        name = failure.get("name")
        if item_id is None or name is None:
            if item_id is not None or name is not None:
                raise StageFailure(
                    f"validator {object_name}.id and .name must both be null "
                    "or both identify a function"
                )
        elif (
            isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or item_id < 0
            or item_id > 2**64 - 1
            or not isinstance(name, str)
        ):
            raise StageFailure(
                f"validator {object_name}.id and .name must identify a function"
            )
        require_text(object_name, "failed_snippet", failure)
        errors = failure.get("errors")
        if not isinstance(errors, list) or not errors:
            raise StageFailure(
                f"validator {object_name}.errors must be a nonempty array"
            )
        for error_index, error in enumerate(errors):
            error_name = f"{object_name}.errors[{error_index}]"
            if not isinstance(error, dict):
                raise StageFailure(f"validator {error_name} must be an object")
            require_exact_keys(error_name, error, {"code", "message"})
            require_text(error_name, "code", error)
            require_text(error_name, "message", error)
    return status


def _record_untracked_response(
    tracker: UsageTracker,
    usage_path: Path,
    before: int,
    request: Any,
    response: Response,
) -> None:
    if len(read_usage(usage_path)) == before:
        tracker.record(
            metadata=request.metadata,
            provider=response.provider,
            model=response.model,
            usage=response.usage,
            latency_s=response.latency_s,
            finish_reason=response.finish_reason,
        )


def _process_scc(
    members: tuple[int, ...],
    records_by_id: dict[int, ItemRecord],
    *,
    stage_input: StageInput,
    current: Path,
    library_source: Path,
    workdir: Path,
    tools: Any,
    client: Any,
    tracker: UsageTracker,
    usage_path: Path,
    exchange_root: Path | None,
    state: RunState,
) -> None:
    duplicate_names: dict[str, list[ItemRecord]] = {}
    for item_id in members:
        record = records_by_id[item_id]
        duplicate_names.setdefault(record.name or "", []).append(record)
    duplicates = [values for values in duplicate_names.values() if len(values) > 1]
    if duplicates:
        detail = "; ".join(
            ", ".join(f"{record.id}:{record.path}" for record in values)
            for values in duplicates
        )
        raise StageFailure(f"duplicate function names inside SCC: {detail}")

    if not any(
        records_by_id[item_id].needs_transformation is True for item_id in members
    ):
        transformation = "\n\n".join(
            records_by_id[item_id].annotated_skeleton or ""
            for item_id in sorted(members)
        )
        replacement_request_path = workdir / "replacement-request.json"
        candidate = workdir / "candidate.rs"
        write_json(
            replacement_request_path,
            replacement_request(members, records_by_id, transformation),
        )
        tools.replace(current, replacement_request_path, candidate)
        state.metrics.cargo_builds += 1
        build = install_candidate_transaction(
            library_source,
            candidate,
            workdir / "rollback",
            lambda: tools.cargo_build(current),
        )
        if build.returncode != 0:
            state.metrics.compilation_failures += 1
            raise StageFailure(
                "mechanical SCC candidate cargo build failed "
                f"({build.returncode})\nstdout:\n{build.stdout}"
                f"\nstderr:\n{build.stderr}"
            )
        return

    context, _ = dependency_context(members, records_by_id, limit=CONTEXT_LIMIT)
    targets = render_transformation_targets(members, records_by_id)
    latest_failed: str | None = None
    latest_diagnostics: str | None = None

    for generation in range(MAX_REPAIRS + 1):
        if generation:
            state.metrics.repair_calls += 1
        rendered = render_prompt(
            PromptRenderInput(
                dependency_context=context,
                transformation_targets=targets,
                failed_transformation=latest_failed,
                diagnostics=latest_diagnostics,
            )
        )
        request = llm_request(rendered, run_id=stage_input.run_id, members=members)
        exchange_dir: Path | None = None
        if exchange_root is not None:
            scc = "_".join(str(item_id) for item_id in sorted(members))
            exchange_dir = exchange_root / f"scc-{scc}" / f"generation-{generation:02d}"
            exchange_dir.mkdir(parents=True, exist_ok=True)
            (exchange_dir / "prompt.md").write_text(rendered.text, encoding="utf-8")
        state.prompt_used = True
        state.metrics.llm_generation_calls += 1
        before = len(read_usage(usage_path))
        response = client.complete(request)
        _record_untracked_response(tracker, usage_path, before, request, response)
        if exchange_dir is not None:
            (exchange_dir / "response.md").write_text(response.text, encoding="utf-8")
        extraction = extract_code_block(response.text)
        if extraction.candidate is None:
            state.metrics.structural_failures += 1
            latest_failed = extraction.failed_text
            latest_diagnostics = extraction.diagnostics
            continue
        transformation = extraction.candidate
        validation_request_path = workdir / "validation-request.json"
        validation_response_path = workdir / "validation-response.json"
        write_json(
            validation_request_path,
            validation_request(members, records_by_id, transformation),
        )
        raw_response, parsed_response = tools.validate(
            validation_request_path, validation_response_path
        )
        validation_status = _validate_validator_response(parsed_response)
        if validation_status == "invalid":
            state.metrics.structural_failures += 1
            latest_failed = transformation
            latest_diagnostics = raw_response
            continue

        replacement_request_path = workdir / "replacement-request.json"
        candidate = workdir / "candidate.rs"
        write_json(
            replacement_request_path,
            replacement_request(members, records_by_id, transformation),
        )
        tools.replace(current, replacement_request_path, candidate)
        state.metrics.cargo_builds += 1
        build = install_candidate_transaction(
            library_source,
            candidate,
            workdir / "rollback",
            lambda: tools.cargo_build(current),
        )
        if build.returncode == 0:
            return
        state.metrics.compilation_failures += 1
        latest_failed = transformation
        latest_diagnostics = (
            f"cargo build stdout:\n{build.stdout}\ncargo build stderr:\n{build.stderr}"
        )
    raise StageFailure(
        f"SCC {','.join(map(str, members))} exhausted {MAX_REPAIRS} repair calls"
    )


def run_stage(
    stage_input: StageInput,
    *,
    stage_dir: Path | None = None,
    tools: Any | None = None,
    llm_client_factory: Callable[[dict[str, Any], UsageTracker], Any] | None = None,
) -> StageOutput:
    stage_dir = (stage_dir or Path(__file__).parent).resolve()
    state = RunState()
    destination: Path | None = None
    try:
        source, destination, workdir, library_relative, config = _validate_boundaries(
            stage_input, stage_dir
        )
        state.config_used = config
        artifacts = stage_input.outputs.artifacts_dir
        log_path = (
            artifacts / "local-transformation.log"
            if artifacts is not None
            else workdir / "local-transformation.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        state.logs = (
            (str(log_path.relative_to(artifacts)),) if artifacts is not None else ()
        )
        usage_path = stage_input.framework.usage_log or workdir / "usage.jsonl"
        state.usage_path = usage_path
        state.usage_start = len(read_usage(usage_path))

        active_tools = tools or CratTools(log_path)
        crat_dir = Path(config["crat_dir"])
        active_tools.build_tools(crat_dir)
        current = workdir / "current"
        if current.exists():
            shutil.rmtree(current)
        shutil.copytree(source, current)
        active_tools.prepare(current, ("expand", "unexpand"), True)
        library_source = current / library_relative
        if not library_source.is_file():
            raise StageFailure(
                f"prepared Cargo library source is not a regular file: {library_source}"
            )

        skeleton_path = workdir / "skeletons.json"
        active_tools.make_skeleton(current, skeleton_path)
        records = load_skeletons(skeleton_path.read_text(encoding="utf-8"))
        records_by_id = {record.id: record for record in records}

        normalized = workdir / "normalized.rs"
        active_tools.normalize(library_source, normalized)
        os.replace(normalized, library_source)
        state.metrics.cargo_builds += 1
        initial_build: CommandResult = active_tools.cargo_build(current)
        if initial_build.returncode != 0:
            raise StageFailure(
                "normalized initial cargo build failed "
                f"({initial_build.returncode})\nstdout:\n{initial_build.stdout}"
                f"\nstderr:\n{initial_build.stderr}"
            )

        graph = function_graph(records)
        schedule = leaf_schedule(graph)
        state.metrics.function_count = len(graph)
        state.metrics.scc_count = len(schedule)

        if schedule:
            llm_settings = dict(stage_input.framework.llm)
            llm_settings["context_overflow"] = "error"
            tracker = UsageTracker(
                usage_path,
                run_id=stage_input.run_id,
                stage=STAGE_ID,
                item=stage_input.item,
                pricing=PricingTable.from_config(llm_settings),
            )
            factory = llm_client_factory or (
                lambda settings, usage_tracker: LlmClient(
                    settings, tracker=usage_tracker
                )
            )
            client = factory(llm_settings, tracker)
            exchange_root = (
                (artifacts or workdir) / "llm-exchanges"
                if config["dump_llm_exchanges"]
                else None
            )
            for members in schedule:
                _process_scc(
                    members,
                    records_by_id,
                    stage_input=stage_input,
                    current=current,
                    library_source=library_source,
                    workdir=workdir,
                    tools=active_tools,
                    client=client,
                    tracker=tracker,
                    usage_path=usage_path,
                    exchange_root=exchange_root,
                    state=state,
                )
        _copy_final(current, destination)
        return _output("success", state, destination=destination)
    except (StageFailure, SkeletonError, ContextOverflow, OSError, ValueError) as exc:
        return _output(
            "failure",
            state,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - stage contract requires failure output
        return _output(
            "failure",
            state,
            error=f"{type(exc).__name__}: {exc}",
        )
