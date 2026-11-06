"""Typed view over the merged configuration tree.

Known framework keys get typed fields with validation; stage-specific
``config`` tables and ``llm`` settings stay as dicts — the framework
passes them through opaquely (stages validate their own config).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from proctor.config.load import ConfigError

OnStageFailure = Literal["stop", "continue"]
_ON_STAGE_FAILURE = ("stop", "continue")


#: Artifact kinds the runner hands to the first stages of a run.
DEFAULT_PROVIDES = ("c_project", "test_package")
_PROVIDABLE = ("c_project", "rust_project", "test_package", "rule_set")


@dataclass(frozen=True)
class RunSettings:
    output_dir: Path = Path("runs")
    on_stage_failure: OnStageFailure = "stop"
    keep_intermediates: bool = True
    provides: tuple[str, ...] = DEFAULT_PROVIDES

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSettings:
        output_dir = data.get("output_dir", "runs")
        if not isinstance(output_dir, str) or not output_dir:
            raise ConfigError("[run] output_dir must be a non-empty string")
        on_stage_failure = data.get("on_stage_failure", "stop")
        if on_stage_failure not in _ON_STAGE_FAILURE:
            raise ConfigError(
                f"[run] on_stage_failure must be one of {_ON_STAGE_FAILURE}, "
                f"got {on_stage_failure!r}"
            )
        keep_intermediates = data.get("keep_intermediates", True)
        if not isinstance(keep_intermediates, bool):
            raise ConfigError("[run] keep_intermediates must be a boolean")
        provides_raw = data.get("provides", list(DEFAULT_PROVIDES))
        if not isinstance(provides_raw, list) or not all(
            isinstance(kind, str) for kind in provides_raw
        ):
            raise ConfigError("[run] provides must be an array of artifact kinds")
        unknown = [kind for kind in provides_raw if kind not in _PROVIDABLE]
        if unknown:
            raise ConfigError(
                f"[run] provides contains unknown artifact kinds {unknown}; "
                f"known kinds: {_PROVIDABLE}"
            )
        return cls(
            output_dir=Path(output_dir),
            on_stage_failure=on_stage_failure,
            keep_intermediates=keep_intermediates,
            provides=tuple(provides_raw),
        )


@dataclass(frozen=True)
class TestingSettings:
    """``[testing]`` — post-stage test gating (plan §5.5)."""

    after_each_stage: bool = False
    profile: str = "debug"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestingSettings:
        after_each_stage = data.get("after_each_stage", False)
        if not isinstance(after_each_stage, bool):
            raise ConfigError("[testing] after_each_stage must be a boolean")
        profile = data.get("profile", "debug")
        if profile not in ("debug", "release"):
            raise ConfigError("[testing] profile must be 'debug' or 'release'")
        return cls(after_each_stage=after_each_stage, profile=profile)


@dataclass(frozen=True)
class MetricsSettings:
    """``[metrics]`` — measure every stage the same way (proctor-side, not
    stage self-report). After each successful stage, write
    ``stages/NN-<id>/metrics.json`` (unsafe + what changed vs the previous
    stage); at run end, a run-level ``metrics.json`` with clippy on the stages
    ``idiomaticity`` selects. Fail-open: a metrics error never fails a run."""

    after_each_stage: bool = False
    #: clippy costs two builds per crate: "first_last" (default) measures the
    #: first and final crate, "all" every stage, "none" skips it.
    idiomaticity: str = "first_last"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetricsSettings:
        after_each_stage = data.get("after_each_stage", False)
        if not isinstance(after_each_stage, bool):
            raise ConfigError("[metrics] after_each_stage must be a boolean")
        idiomaticity = data.get("idiomaticity", "first_last")
        if idiomaticity not in ("first_last", "all", "none"):
            raise ConfigError(
                "[metrics] idiomaticity must be 'first_last', 'all' or 'none'"
            )
        return cls(after_each_stage=after_each_stage, idiomaticity=idiomaticity)


@dataclass(frozen=True)
class StageEntry:
    """One ``[stages.<id>]`` table."""

    id: str
    uses: Path
    enabled: bool = True
    timeout_s: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, stage_id: str, data: dict[str, Any]) -> StageEntry:
        uses = data.get("uses")
        if not isinstance(uses, str) or not uses:
            raise ConfigError(
                f"[stages.{stage_id}] must set 'uses' to the stage directory"
            )
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"[stages.{stage_id}] enabled must be a boolean")
        timeout_s = data.get("timeout_s")
        if timeout_s is not None and (
            not isinstance(timeout_s, int) or isinstance(timeout_s, bool)
        ):
            raise ConfigError(f"[stages.{stage_id}] timeout_s must be an integer")
        config = data.get("config", {})
        if not isinstance(config, dict):
            raise ConfigError(f"[stages.{stage_id}.config] must be a table")
        llm = data.get("llm", {})
        if not isinstance(llm, dict):
            raise ConfigError(f"[stages.{stage_id}.llm] must be a table")
        return cls(
            id=stage_id,
            uses=Path(uses),
            enabled=enabled,
            timeout_s=timeout_s,
            config=config,
            llm=llm,
        )


@dataclass(frozen=True)
class PipelineConfig:
    """The parsed configuration tree for one pipeline run."""

    order: tuple[str, ...]
    stages: dict[str, StageEntry]
    run: RunSettings = field(default_factory=RunSettings)
    testing: TestingSettings = field(default_factory=TestingSettings)
    metrics: MetricsSettings = field(default_factory=MetricsSettings)
    llm_defaults: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineConfig:
        pipeline = data.get("pipeline")
        if not isinstance(pipeline, dict):
            raise ConfigError("config must contain a [pipeline] table")
        order_raw = pipeline.get("order")
        if (
            not isinstance(order_raw, list)
            or not order_raw
            or not all(isinstance(sid, str) and sid for sid in order_raw)
        ):
            raise ConfigError("[pipeline] order must be a non-empty array of stage ids")
        if len(set(order_raw)) != len(order_raw):
            raise ConfigError("[pipeline] order contains duplicate stage ids")

        stages_raw = data.get("stages", {})
        if not isinstance(stages_raw, dict):
            raise ConfigError("[stages] must be a table")
        stages: dict[str, StageEntry] = {}
        for stage_id, entry in stages_raw.items():
            if not isinstance(entry, dict):
                raise ConfigError(f"[stages.{stage_id}] must be a table")
            stages[stage_id] = StageEntry.from_dict(stage_id, entry)

        missing = [sid for sid in order_raw if sid not in stages]
        if missing:
            raise ConfigError(f"pipeline.order references undefined stages: {missing}")

        llm_defaults = data.get("llm", {})
        if not isinstance(llm_defaults, dict):
            raise ConfigError("[llm] must be a table")

        run_raw = data.get("run", {})
        if not isinstance(run_raw, dict):
            raise ConfigError("[run] must be a table")

        testing_raw = data.get("testing", {})
        if not isinstance(testing_raw, dict):
            raise ConfigError("[testing] must be a table")

        metrics_raw = data.get("metrics", {})
        if not isinstance(metrics_raw, dict):
            raise ConfigError("[metrics] must be a table")

        return cls(
            order=tuple(order_raw),
            stages=stages,
            run=RunSettings.from_dict(run_raw),
            testing=TestingSettings.from_dict(testing_raw),
            metrics=MetricsSettings.from_dict(metrics_raw),
            llm_defaults=llm_defaults,
            raw=data,
        )
