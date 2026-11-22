"""Stage envelope contract: ``stage_input.json`` / ``stage_output.json``.

The orchestrator writes one envelope pair per stage per test case; a stage
reads its ``StageInput``, does its work, and writes a ``StageOutput``.
``docs/stage-contract.md`` is the contributor-facing description of this
contract and the JSON Schemas in ``schemas/`` are its language-agnostic
artifact; ``tests/test_schemas.py`` keeps code and schemas in sync.

Compatibility rules: ``schema_version`` bumps only on breaking change;
additive fields never bump it; readers ignore unknown fields and refuse
versions newer than they understand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1

Status = Literal["success", "failure", "skipped"]
_STATUSES = ("success", "failure", "skipped")


class ContractError(ValueError):
    """A payload does not conform to the stage envelope contract."""


def _check_schema_version(data: dict[str, Any]) -> int:
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ContractError("'schema_version' must be an integer")
    if version > SCHEMA_VERSION:
        raise ContractError(
            f"schema_version {version} is newer than this framework "
            f"understands (max {SCHEMA_VERSION}); upgrade proctor"
        )
    if version < 1:
        raise ContractError(f"invalid schema_version {version}")
    return version


def _req_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{key!r} must be a non-empty string")
    return value


def _opt_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{key!r} must be a string or null")
    return value


def _req_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{key!r} must be an integer")
    return value


def _opt_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{key!r} must be an integer or null")
    return value


def _opt_float(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{key!r} must be a number or null")
    return float(value)


def _obj(data: dict[str, Any], key: str, *, required: bool = False) -> dict[str, Any]:
    value = data.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ContractError(f"{key!r} must be a JSON object")
    return value


def _opt_path(data: dict[str, Any], key: str) -> Path | None:
    value = _opt_str(data, key)
    return None if value is None else Path(value)


def _path_str(path: Path | None) -> str | None:
    return None if path is None else str(path)


@dataclass(frozen=True)
class InputArtifacts:
    """Read-only artifact paths handed to a stage. Absent kinds are None."""

    c_project: Path | None = None
    rust_project: Path | None = None
    test_package: Path | None = None
    rule_set: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InputArtifacts:
        return cls(
            c_project=_opt_path(data, "c_project"),
            rust_project=_opt_path(data, "rust_project"),
            test_package=_opt_path(data, "test_package"),
            rule_set=_opt_path(data, "rule_set"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "c_project": _path_str(self.c_project),
            "rust_project": _path_str(self.rust_project),
            "test_package": _path_str(self.test_package),
            "rule_set": _path_str(self.rule_set),
        }


@dataclass(frozen=True)
class OutputDestinations:
    """Where a stage must create its outputs (input side), or where it
    reports having created them (output side)."""

    rust_project: Path | None = None
    rule_set: Path | None = None
    artifacts_dir: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputDestinations:
        return cls(
            rust_project=_opt_path(data, "rust_project"),
            rule_set=_opt_path(data, "rule_set"),
            artifacts_dir=_opt_path(data, "artifacts_dir"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rust_project": _path_str(self.rust_project),
            "rule_set": _path_str(self.rule_set),
            "artifacts_dir": _path_str(self.artifacts_dir),
        }


@dataclass(frozen=True)
class Budget:
    """Advisory spend ceiling for a stage's LLM usage."""

    max_usd: float | None = None
    max_tokens: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Budget:
        return cls(
            max_usd=_opt_float(data, "max_usd"),
            max_tokens=_opt_int(data, "max_tokens"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"max_usd": self.max_usd, "max_tokens": self.max_tokens}


@dataclass(frozen=True)
class FrameworkSettings:
    """Advisory framework settings. A self-contained stage may ignore these
    entirely, provided it reports the required information in its output."""

    llm: dict[str, Any] = field(default_factory=dict)
    usage_log: Path | None = None
    prompt_library: Path | None = None
    workdir: Path | None = None
    budget: Budget = field(default_factory=Budget)
    timeout_s: int | None = None
    no_fail: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameworkSettings:
        no_fail = data.get("no_fail", False)
        if not isinstance(no_fail, bool):
            raise ContractError("'no_fail' must be a boolean")
        return cls(
            llm=_obj(data, "llm"),
            usage_log=_opt_path(data, "usage_log"),
            prompt_library=_opt_path(data, "prompt_library"),
            workdir=_opt_path(data, "workdir"),
            budget=Budget.from_dict(_obj(data, "budget")),
            timeout_s=_opt_int(data, "timeout_s"),
            no_fail=no_fail,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm": self.llm,
            "usage_log": _path_str(self.usage_log),
            "prompt_library": _path_str(self.prompt_library),
            "workdir": _path_str(self.workdir),
            "budget": self.budget.to_dict(),
            "timeout_s": self.timeout_s,
            "no_fail": self.no_fail,
        }


@dataclass(frozen=True)
class StageInput:
    """The ``stage_input.json`` envelope, written by the orchestrator."""

    run_id: str
    stage_id: str
    stage_index: int
    inputs: InputArtifacts
    outputs: OutputDestinations
    item: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    framework: FrameworkSettings = field(default_factory=FrameworkSettings)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageInput:
        version = _check_schema_version(data)
        return cls(
            run_id=_req_str(data, "run_id"),
            stage_id=_req_str(data, "stage_id"),
            stage_index=_req_int(data, "stage_index"),
            item=_opt_str(data, "item"),
            inputs=InputArtifacts.from_dict(_obj(data, "inputs", required=True)),
            outputs=OutputDestinations.from_dict(_obj(data, "outputs", required=True)),
            config=_obj(data, "config"),
            framework=FrameworkSettings.from_dict(_obj(data, "framework")),
            schema_version=version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "stage_index": self.stage_index,
            "item": self.item,
            "inputs": self.inputs.to_dict(),
            "outputs": self.outputs.to_dict(),
            "config": self.config,
            "framework": self.framework.to_dict(),
        }

    @classmethod
    def read(cls, path: Path) -> StageInput:
        return cls.from_dict(_read_json_object(path))

    def write(self, path: Path) -> None:
        _write_json(path, self.to_dict())


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelInfo:
        return cls(provider=_req_str(data, "provider"), model=_req_str(data, "model"))

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model}


@dataclass(frozen=True)
class UsageSummary:
    """Aggregate LLM usage for one stage invocation."""

    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    cost_usd: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageSummary:
        summary = cls(
            calls=_req_int(data, "calls") if "calls" in data else 0,
            input_tokens=_req_int(data, "input_tokens")
            if "input_tokens" in data
            else 0,
            cached_input_tokens=(
                _req_int(data, "cached_input_tokens")
                if "cached_input_tokens" in data
                else 0
            ),
            output_tokens=(
                _req_int(data, "output_tokens") if "output_tokens" in data else 0
            ),
            reasoning_tokens=_opt_int(data, "reasoning_tokens"),
            cost_usd=_opt_float(data, "cost_usd"),
        )
        for name, value in (
            ("calls", summary.calls),
            ("input_tokens", summary.input_tokens),
            ("cached_input_tokens", summary.cached_input_tokens),
            ("output_tokens", summary.output_tokens),
        ):
            if value < 0:
                raise ContractError(f"usage.{name} must be >= 0")
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class PromptUse:
    id: str
    version: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptUse:
        return cls(id=_req_str(data, "id"), version=_req_int(data, "version"))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version}


@dataclass(frozen=True)
class StageOutput:
    """The ``stage_output.json`` envelope, written by the stage.

    Rules enforced here:

    - ``status`` is one of success | failure | skipped;
    - a ``failure`` must carry a non-empty ``error``;
    - a ``success`` or ``skipped`` must have ``error`` null.
    """

    status: Status
    stage_id: str
    stage_version: str | None = None
    outputs: OutputDestinations = field(default_factory=OutputDestinations)
    config_used: dict[str, Any] = field(default_factory=dict)
    models: tuple[ModelInfo, ...] = ()
    usage: UsageSummary | None = None
    prompts: tuple[PromptUse, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    logs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ContractError(
                f"status must be one of {_STATUSES}, got {self.status!r}"
            )
        if self.status == "failure" and not self.error:
            raise ContractError("a failure output must carry a non-empty 'error'")
        if self.status != "failure" and self.error is not None:
            raise ContractError(f"a {self.status} output must have 'error' null")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageOutput:
        version = _check_schema_version(data)
        status = _req_str(data, "status")
        if status not in _STATUSES:
            raise ContractError(f"status must be one of {_STATUSES}, got {status!r}")

        models_raw = data.get("models", [])
        if not isinstance(models_raw, list):
            raise ContractError("'models' must be an array")
        prompts_raw = data.get("prompts", [])
        if not isinstance(prompts_raw, list):
            raise ContractError("'prompts' must be an array")
        logs_raw = data.get("logs", [])
        if not isinstance(logs_raw, list) or not all(
            isinstance(entry, str) for entry in logs_raw
        ):
            raise ContractError("'logs' must be an array of strings")

        usage_raw = data.get("usage")
        usage: UsageSummary | None = None
        if usage_raw is not None:
            if not isinstance(usage_raw, dict):
                raise ContractError("'usage' must be a JSON object or null")
            usage = UsageSummary.from_dict(usage_raw)

        return cls(
            status=status,  # type: ignore[arg-type]  # narrowed by check above
            stage_id=_req_str(data, "stage_id"),
            stage_version=_opt_str(data, "stage_version"),
            outputs=OutputDestinations.from_dict(_obj(data, "outputs")),
            config_used=_obj(data, "config_used"),
            models=tuple(
                ModelInfo.from_dict(_as_obj(entry, "models[]")) for entry in models_raw
            ),
            usage=usage,
            prompts=tuple(
                PromptUse.from_dict(_as_obj(entry, "prompts[]"))
                for entry in prompts_raw
            ),
            metrics=_obj(data, "metrics"),
            logs=tuple(logs_raw),
            metadata=_obj(data, "metadata"),
            error=_opt_str(data, "error"),
            schema_version=version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "stage_id": self.stage_id,
            "stage_version": self.stage_version,
            "outputs": self.outputs.to_dict(),
            "config_used": self.config_used,
            "models": [model.to_dict() for model in self.models],
            "usage": None if self.usage is None else self.usage.to_dict(),
            "prompts": [prompt.to_dict() for prompt in self.prompts],
            "metrics": self.metrics,
            "logs": list(self.logs),
            "metadata": self.metadata,
            "error": self.error,
        }

    @classmethod
    def read(cls, path: Path) -> StageOutput:
        return cls.from_dict(_read_json_object(path))

    def write(self, path: Path) -> None:
        _write_json(path, self.to_dict())


def _as_obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{where} entries must be JSON objects")
    return value


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractError(f"{path} does not exist") from None
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
