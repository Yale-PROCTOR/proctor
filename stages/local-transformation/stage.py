from __future__ import annotations

import os
import json
import hashlib
import re
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Literal, cast

from model import (
    ContextOverflow,
    CallableCorrespondence,
    ItemRecord,
    PointerVariableMetadata,
    StatementPairMetadata,
    SkeletonError,
    ObservationError,
    ObservationDocument,
    dependency_context,
    function_graph,
    leaf_schedule,
    load_skeletons,
    load_observations,
    load_replacement_metadata,
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
    statement_pairs: dict[tuple[int, int], AcceptedStatementPair] = field(
        default_factory=dict
    )
    accepted_correspondence: list[CallableCorrespondence] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ReplacementStatementPair:
    item_id: int
    path: str
    label: int
    after_statement: str


@dataclass(frozen=True)
class AcceptedStatementPair:
    item_id: int
    path: str
    metadata: StatementPairMetadata
    after_statement: str


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
) -> tuple[Path, Path, Path, Path, Path, Path, dict[str, Any]]:
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
    report_path = (
        stage_input.outputs.artifacts_dir / "statement-pairs.md"
        if stage_input.outputs.artifacts_dir is not None
        else workdir / "statement-pairs.md"
    )
    observations_path = (
        stage_input.outputs.artifacts_dir / "observations.json"
        if stage_input.outputs.artifacts_dir is not None
        else workdir / "observations.json"
    )
    report_resolved = report_path.resolve()
    destination_resolved = destination.resolve()
    if (
        report_resolved == destination_resolved
        or report_resolved in destination_resolved.parents
        or destination_resolved in report_resolved.parents
    ):
        raise StageFailure(
            "statement-pairs report path overlaps output Rust project: "
            f"{report_resolved}"
        )
    observations_resolved = observations_path.resolve()
    if (
        observations_resolved == destination_resolved
        or observations_resolved in destination_resolved.parents
        or destination_resolved in observations_resolved.parents
    ):
        raise StageFailure(
            "observations artifact path overlaps output Rust project: "
            f"{observations_resolved}"
        )
    if observations_resolved == report_resolved:
        raise StageFailure("statement-pairs and observations artifact paths overlap")
    current_resolved = (workdir / "current").resolve()
    current_sensitive_paths = {
        "statement-pairs report": report_resolved,
        "observations artifact": observations_resolved,
    }
    if stage_input.outputs.artifacts_dir is not None:
        current_sensitive_paths["artifacts directory"] = (
            stage_input.outputs.artifacts_dir.resolve()
        )
    for name, path in current_sensitive_paths.items():
        if (
            path == current_resolved
            or path in current_resolved.parents
            or current_resolved in path.parents
        ):
            raise StageFailure(f"{name} overlaps working Rust project: {path}")
    library_relative = _library_relative_path(source)
    return (
        source,
        destination,
        workdir,
        library_relative,
        report_path,
        observations_path,
        config,
    )


def _clear_stale_report(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise StageFailure(
            f"statement-pairs report destination is not a regular file or symlink: "
            f"{path}"
        )


def _copy_final(
    current: Path,
    destination: Path,
    mark_destination_created: Callable[[], None],
) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {"target"} if Path(directory) == current else set()

    destination.mkdir()
    mark_destination_created()
    shutil.copytree(current, destination, ignore=ignore, dirs_exist_ok=True)


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


def _load_replacement_statement_pairs(
    path: Path,
    members: tuple[int, ...],
    records_by_id: dict[int, ItemRecord],
) -> tuple[ReplacementStatementPair, ...]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StageFailure(
            f"replacement statement-pairs sidecar is malformed JSON: {exc}"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "statements"}:
        raise StageFailure(
            "replacement statement-pairs sidecar must contain exactly "
            "['schema_version', 'statements']"
        )
    version = value["schema_version"]
    if isinstance(version, bool) or version != 1:
        raise StageFailure(
            "unsupported replacement statement-pairs sidecar schema_version "
            f"{version!r}"
        )
    statements = value["statements"]
    if not isinstance(statements, list):
        raise StageFailure(
            "replacement statement-pairs sidecar statements must be an array"
        )
    expected: dict[tuple[int, int], str] = {
        (item_id, label): records_by_id[item_id].path
        for item_id in members
        for label in records_by_id[item_id].statements_requiring_transformation
    }
    result: list[ReplacementStatementPair] = []
    previous: tuple[int, int] | None = None
    for index, statement in enumerate(statements):
        where = f"replacement statement-pairs sidecar statements[{index}]"
        if not isinstance(statement, dict) or set(statement) != {
            "item_id",
            "path",
            "label",
            "after_statement",
        }:
            raise StageFailure(
                f"{where} must contain exactly "
                "['after_statement', 'item_id', 'label', 'path']"
            )
        item_id = statement["item_id"]
        if (
            isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or not 0 <= item_id <= 2**64 - 1
        ):
            raise StageFailure(f"{where}.item_id must be in the u64 range")
        label = statement["label"]
        if (
            isinstance(label, bool)
            or not isinstance(label, int)
            or not 0 <= label <= 2**32 - 1
        ):
            raise StageFailure(f"{where}.label must be in the u32 range")
        function_path = statement["path"]
        if (
            not isinstance(function_path, str)
            or not function_path
            or "\r" in function_path
            or "\n" in function_path
        ):
            raise StageFailure(f"{where}.path must be a nonempty single-line string")
        after_statement = statement["after_statement"]
        if (
            not isinstance(after_statement, str)
            or not after_statement
            or "\r" in after_statement
            or after_statement.endswith("\n")
        ):
            raise StageFailure(
                f"{where}.after_statement must be nonempty, contain no carriage "
                "return, and have no trailing newline"
            )
        key = (item_id, label)
        if previous is not None and key <= previous:
            detail = "duplicate" if key == previous else "out of order"
            raise StageFailure(
                f"replacement statement-pairs sidecar key {key} is {detail}"
            )
        previous = key
        if expected.get(key) != function_path:
            raise StageFailure(
                f"replacement statement-pairs sidecar item/path is outside the "
                f"current SCC request: {item_id}:{function_path}"
            )
        result.append(
            ReplacementStatementPair(
                item_id=item_id,
                path=function_path,
                label=label,
                after_statement=after_statement,
            )
        )
    actual_keys = {(entry.item_id, entry.label) for entry in result}
    if actual_keys != set(expected):
        missing = sorted(set(expected) - actual_keys)
        extra = sorted(actual_keys - set(expected))
        raise StageFailure(
            "replacement statement-pairs sidecar labels do not exactly match "
            f"the current SCC request; missing={missing}, extra={extra}"
        )
    return tuple(result)


def _load_and_validate_replacement_metadata(
    path: Path,
    candidate: Path,
    statement_pairs: Path,
    observation_source: Path,
    members: tuple[int, ...],
    records_by_id: dict[int, ItemRecord],
    accepted: tuple[CallableCorrespondence, ...],
):
    try:
        metadata = load_replacement_metadata(path.read_text(encoding="utf-8"))
    except ObservationError as exc:
        raise StageFailure(f"invalid replacement observation metadata: {exc}") from exc
    companions = (
        ("candidate_sha256", candidate, "candidate"),
        ("statement_pairs_sha256", statement_pairs, "statement-pairs sidecar"),
        ("observation_source_sha256", observation_source, "observation source"),
    )
    for field_name, companion, display in companions:
        actual = hashlib.sha256(companion.read_bytes()).hexdigest()
        if getattr(metadata, field_name) != actual:
            raise StageFailure(
                f"replacement metadata {field_name} does not match {display} bytes"
            )
    if metadata.accepted_correspondence != accepted:
        raise StageFailure(
            "replacement metadata accepted_correspondence does not equal the request"
        )
    if len(metadata.new_correspondence) != len(metadata.current_items):
        raise StageFailure("replacement metadata current/new record counts differ")
    for index, (new, current) in enumerate(
        zip(metadata.new_correspondence, metadata.current_items, strict=True)
    ):
        for field_name in (
            "item_id",
            "logical_path",
            "implementation_path",
            "wrapper_path",
        ):
            if getattr(new, field_name) != getattr(current, field_name):
                raise StageFailure(
                    f"replacement metadata current_items[{index}].{field_name} "
                    f"disagrees with new_correspondence[{index}].{field_name}"
                )
    expected_order = tuple(sorted(members))
    if (
        tuple(record.item_id for record in metadata.new_correspondence)
        != expected_order
        or tuple(record.item_id for record in metadata.current_items) != expected_order
    ):
        raise StageFailure("replacement metadata records do not preserve request order")
    for index, (new, current) in enumerate(
        zip(metadata.new_correspondence, metadata.current_items, strict=True)
    ):
        expected = records_by_id[new.item_id]
        if current.logical_path != expected.path:
            raise StageFailure(
                "replacement metadata records do not preserve request order"
            )
        expected_labels = expected.statements_requiring_transformation
        if current.transform_labels != expected_labels:
            raise StageFailure(
                f"replacement metadata current_items[{index}].transform_labels "
                f"does not equal {list(expected_labels)}"
            )
    all_records = (*metadata.accepted_correspondence, *metadata.new_correspondence)
    categories: dict[str, list[tuple[str, int]]] = {
        "logical_path": [
            (record.logical_path, record.item_id) for record in all_records
        ],
        "implementation_path": [
            (record.implementation_path, record.item_id) for record in all_records
        ],
        "wrapper_path": [
            (record.wrapper_path, record.item_id)
            for record in all_records
            if record.wrapper_path is not None
        ],
        "source_copy_path": [
            (record.source_copy_path, record.item_id)
            for record in metadata.current_items
        ],
    }
    item_ids: set[int] = set()
    for record in all_records:
        if record.item_id in item_ids:
            raise StageFailure(
                f"replacement metadata has duplicate item_id {record.item_id}"
            )
        item_ids.add(record.item_id)
    for category, values in categories.items():
        seen: set[str] = set()
        for value, _item_id in values:
            if value in seen:
                raise StageFailure(
                    f"replacement metadata has duplicate {category} {value}"
                )
            seen.add(value)
    path_roles: dict[str, tuple[str, int]] = {}
    for category, values in categories.items():
        for value, item_id in values:
            previous = path_roles.get(value)
            if previous is not None and not (
                previous[0] == "logical_path"
                and category == "implementation_path"
                and previous[1] == item_id
            ):
                raise StageFailure(
                    f"replacement metadata path {value} is used as both "
                    f"{previous[0]} and {category}"
                )
            path_roles.setdefault(value, (category, item_id))
    return metadata


def _accept_statement_pairs(
    pairs: tuple[ReplacementStatementPair, ...],
    records_by_id: dict[int, ItemRecord],
    accumulator: dict[tuple[int, int], AcceptedStatementPair],
) -> None:
    for pair in pairs:
        key = (pair.item_id, pair.label)
        if key in accumulator:
            raise StageFailure(
                f"duplicate accepted statement-pair key {pair.item_id}:{pair.label}"
            )
        record = records_by_id[pair.item_id]
        metadata = next(
            (
                statement
                for statement in record.statement_pair_metadata
                if statement.label == pair.label
            ),
            None,
        )
        if metadata is None:
            raise StageFailure(
                f"missing immutable statement metadata for {pair.item_id}:{pair.label}"
            )
        accumulator[key] = AcceptedStatementPair(
            item_id=pair.item_id,
            path=pair.path,
            metadata=metadata,
            after_statement=pair.after_statement,
        )


_TYPE_WHITESPACE = re.compile(r"[ \t\r\n\f]+")


def _code_value(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise StageFailure("single-line report code value contains a newline")
    escaped = value
    for source, replacement in (
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("|", "&#124;"),
        ("`", "&#96;"),
        ("\\", "&#92;"),
    ):
        escaped = escaped.replace(source, replacement)
    return f"<code>{escaped}</code>"


def _type_code_value(value: str) -> str:
    return _code_value(_TYPE_WHITESPACE.sub(" ", value).strip(" "))


def _rust_fence(snippet: str) -> str:
    longest = max(
        (len(match.group(0)) for match in re.finditer(r"`+", snippet)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}rust\n{snippet}\n{fence}"


def _origin_text(variable: PointerVariableMetadata) -> str:
    if variable.origin.kind == "parameter":
        return f"parameter {variable.origin.value}"
    return f"local statement {variable.origin.value}"


def _pointer_variable_section(metadata: StatementPairMetadata) -> str:
    rows = metadata.pointer_variables
    pieces = ["#### Pointer variables"]
    if not metadata.pointer_variables_complete:
        pieces.append(
            "> **Warning:** Pointer-variable metadata is incomplete because Crat "
            "could not\n> resolve every possible binding occurrence in this source "
            "statement."
        )
    if rows:
        table = [
            "| Variable | Origin | Before type | Selected target type | Before type inferred |",
            "| --- | --- | --- | --- | --- |",
        ]
        table.extend(
            "| "
            + " | ".join(
                (
                    _code_value(variable.name),
                    _code_value(_origin_text(variable)),
                    _type_code_value(variable.before_type),
                    _type_code_value(variable.selected_target_type),
                    "yes" if variable.before_type_is_inferred else "no",
                )
            )
            + " |"
            for variable in rows
        )
        pieces.append("\n".join(table))
    elif metadata.pointer_variables_complete:
        pieces.append(
            "_No existing source raw-pointer parameter or simple local binding "
            "appears in\nthis statement._"
        )
    else:
        pieces.append(
            "_No eligible pointer-variable binding could be resolved for this "
            "statement._"
        )
    return "\n\n".join(pieces)


def _render_statement_pairs(
    pairs: dict[tuple[int, int], AcceptedStatementPair],
) -> str:
    introduction = (
        "# Before/After Statement Pairs\n\n"
        "This report contains build-accepted local-transformation statement pairs."
    )
    if not pairs:
        return introduction + "\n\n_No statements required local transformation._\n"
    sections = [introduction]
    previous_item: int | None = None
    for key in sorted(pairs):
        pair = pairs[key]
        if pair.item_id != previous_item:
            sections.append(f"## Item {pair.item_id}: {_code_value(pair.path)}")
            previous_item = pair.item_id
        sections.append(
            "\n\n".join(
                (
                    f"### Statement {pair.metadata.label}",
                    "#### Before",
                    _rust_fence(pair.metadata.before_statement),
                    "#### After",
                    _rust_fence(pair.after_statement),
                    _pointer_variable_section(pair.metadata),
                )
            )
        )
    return "\n\n".join(sections) + "\n"


def _remove_exact_output(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        raise OSError(f"cannot clean up unexpected output node: {path}")


def _remove_exact_report(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise OSError(f"cannot clean up unexpected report node: {path}")


def _publish_final_outputs(
    current: Path,
    destination: Path,
    report_path: Path,
    report: str,
    observations_path: Path | None = None,
    observations: str = '{\n  "schema_version": 1,\n  "observations": []\n}\n',
) -> None:
    observations_path = observations_path or report_path.with_name("observations.json")
    report_temporary: Path | None = None
    observations_temporary: Path | None = None
    destination_created = False
    report_published = False
    observations_published = False

    def mark_destination_created() -> None:
        nonlocal destination_created
        destination_created = True

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=report_path.parent,
            prefix=f".{report_path.name}.",
            suffix=".tmp",
        )
        report_temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(report)
        observations_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=observations_path.parent,
            prefix=f".{observations_path.name}.",
            suffix=".tmp",
        )
        observations_temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(observations)
        _copy_final(current, destination, mark_destination_created)
        os.replace(report_temporary, report_path)
        report_temporary = None
        report_published = True
        os.replace(observations_temporary, observations_path)
        observations_temporary = None
        observations_published = True
    except Exception as primary:
        cleanup_errors: list[str] = []
        cleanup_targets = [
            (report_temporary, _remove_exact_report),
            (observations_temporary, _remove_exact_report),
        ]
        if observations_published:
            cleanup_targets.append((observations_path, _remove_exact_report))
        if report_published:
            cleanup_targets.append((report_path, _remove_exact_report))
        if destination_created:
            cleanup_targets.append((destination, _remove_exact_output))
        for path, remover in cleanup_targets:
            if path is None:
                continue
            try:
                remover(path)
            except OSError as cleanup:
                cleanup_errors.append(f"{path}: {cleanup}")
        detail = (
            f"; cleanup failures: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        )
        raise StageFailure(
            f"failed to publish final transformation outputs: "
            f"{type(primary).__name__}: {primary}{detail}"
        ) from primary


@contextmanager
def _replacement_attempt_outputs(paths: tuple[Path, ...]):
    primary: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        for path in paths:
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.exists():
                    raise OSError(
                        f"attempt output is not a regular file or symlink: {path}"
                    )
            except OSError as exc:
                cleanup_errors.append(f"{path}: {exc}")
        if cleanup_errors:
            detail = "; ".join(cleanup_errors)
            if primary is None:
                raise StageFailure(
                    f"failed to clean replacement attempt outputs: {detail}"
                )
            raise StageFailure(
                f"{type(primary).__name__}: {primary}; cleanup failures: {detail}"
            ) from primary


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
        statement_pairs_path = workdir / "replacement-statement-pairs.json"
        observation_source_path = workdir / "replacement-observation.rs"
        observation_metadata_path = workdir / "replacement-observation-metadata.json"
        write_json(
            replacement_request_path,
            replacement_request(
                members,
                records_by_id,
                transformation,
                tuple(state.accepted_correspondence),
            ),
        )
        with _replacement_attempt_outputs(
            (
                candidate,
                statement_pairs_path,
                observation_source_path,
                observation_metadata_path,
            )
        ):
            tools.replace(
                current,
                replacement_request_path,
                candidate,
                statement_pairs_path,
                observation_source_path,
                observation_metadata_path,
            )
            statement_pairs = _load_replacement_statement_pairs(
                statement_pairs_path,
                members,
                records_by_id,
            )
            metadata = _load_and_validate_replacement_metadata(
                observation_metadata_path,
                candidate,
                statement_pairs_path,
                observation_source_path,
                members,
                records_by_id,
                tuple(state.accepted_correspondence),
            )
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
            _accept_statement_pairs(
                statement_pairs,
                records_by_id,
                state.statement_pairs,
            )
            state.accepted_correspondence.extend(metadata.new_correspondence)
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
        statement_pairs_path = workdir / "replacement-statement-pairs.json"
        observation_source_path = workdir / "replacement-observation.rs"
        observation_metadata_path = workdir / "replacement-observation-metadata.json"
        extracted_observations_path = workdir / "extracted-observations.json"
        write_json(
            replacement_request_path,
            replacement_request(
                members,
                records_by_id,
                transformation,
                tuple(state.accepted_correspondence),
            ),
        )
        with _replacement_attempt_outputs(
            (
                candidate,
                statement_pairs_path,
                observation_source_path,
                observation_metadata_path,
                extracted_observations_path,
            )
        ):
            tools.replace(
                current,
                replacement_request_path,
                candidate,
                statement_pairs_path,
                observation_source_path,
                observation_metadata_path,
            )
            statement_pairs = _load_replacement_statement_pairs(
                statement_pairs_path,
                members,
                records_by_id,
            )
            metadata = _load_and_validate_replacement_metadata(
                observation_metadata_path,
                candidate,
                statement_pairs_path,
                observation_source_path,
                members,
                records_by_id,
                tuple(state.accepted_correspondence),
            )
            state.metrics.cargo_builds += 1
            build = install_candidate_transaction(
                library_source,
                candidate,
                workdir / "rollback",
                lambda: tools.cargo_build(current),
            )
            if build.returncode == 0:
                extracted = ObservationDocument(observations=())
                if any(item.transform_labels for item in metadata.current_items):
                    tools.extract_observations(
                        observation_source_path,
                        observation_metadata_path,
                        extracted_observations_path,
                    )
                    try:
                        extracted = load_observations(
                            extracted_observations_path.read_text(encoding="utf-8")
                        )
                    except ObservationError as exc:
                        raise StageFailure(
                            f"invalid extracted observation document: {exc}"
                        ) from exc
                _accept_statement_pairs(
                    statement_pairs,
                    records_by_id,
                    state.statement_pairs,
                )
                state.observations.extend(extracted.observations)
                state.accepted_correspondence.extend(metadata.new_correspondence)
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
        (
            source,
            destination,
            workdir,
            library_relative,
            report_path,
            observations_path,
            config,
        ) = _validate_boundaries(stage_input, stage_dir)
        _clear_stale_report(report_path)
        _clear_stale_report(observations_path)
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
        report = _render_statement_pairs(state.statement_pairs)
        observations = (
            json.dumps(
                {"schema_version": 1, "observations": state.observations}, indent=2
            )
            + "\n"
        )
        _publish_final_outputs(
            current,
            destination,
            report_path,
            report,
            observations_path,
            observations,
        )
        return _output("success", state, destination=destination)
    except (
        StageFailure,
        SkeletonError,
        ObservationError,
        ContextOverflow,
        OSError,
        ValueError,
    ) as exc:
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
