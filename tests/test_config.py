"""Config merge semantics, --set overrides, typed model, resolution."""

from pathlib import Path

import pytest

from proctor.config.load import (
    ConfigError,
    apply_override,
    deep_merge,
    load_config,
    parse_override,
)
from proctor.config.model import PipelineConfig
from proctor.config.resolve import resolve_stages


def test_tables_deep_merge_scalars_replace() -> None:
    base = {"llm": {"provider": "anthropic", "model": "a"}, "x": 1}
    overlay = {"llm": {"model": "b"}, "x": 2}
    merged = deep_merge(base, overlay)
    assert merged == {"llm": {"provider": "anthropic", "model": "b"}, "x": 2}


def test_arrays_replace_not_append() -> None:
    merged = deep_merge(
        {"pipeline": {"order": ["a", "b"]}}, {"pipeline": {"order": ["b"]}}
    )
    assert merged == {"pipeline": {"order": ["b"]}}


def test_set_override_types() -> None:
    assert parse_override("a.b=8") == (["a", "b"], 8)
    assert parse_override("a.b=true") == (["a", "b"], True)
    assert parse_override('a.b="8"') == (["a", "b"], "8")
    assert parse_override("a.b=hello") == (["a", "b"], "hello")
    assert parse_override('a.b=["x","y"]') == (["a", "b"], ["x", "y"])


def test_set_override_creates_tables() -> None:
    config: dict[str, object] = {}
    apply_override(config, "stages.example.config.max_iterations=8")
    assert config == {"stages": {"example": {"config": {"max_iterations": 8}}}}


def test_set_through_scalar_refused() -> None:
    config: dict[str, object] = {"x": 1}
    with pytest.raises(ConfigError, match="non-table"):
        apply_override(config, "x.y=2")


def test_overlay_files_later_wins(tmp_path: Path) -> None:
    (tmp_path / "base.toml").write_text(
        '[llm]\nprovider = "anthropic"\nmodel = "a"\n', encoding="utf-8"
    )
    (tmp_path / "exp.toml").write_text('[llm]\nmodel = "b"\n', encoding="utf-8")
    merged = load_config(
        [tmp_path / "base.toml", tmp_path / "exp.toml"], ["llm.model='c'"]
    )
    assert merged["llm"] == {"provider": "anthropic", "model": "c"}


def _pipeline_dict() -> dict[str, object]:
    return {
        "pipeline": {"order": ["a", "b"]},
        "llm": {"provider": "anthropic", "model": "base", "max_retries": 5},
        "stages": {
            "a": {"uses": "stages/a"},
            "b": {"uses": "stages/b", "llm": {"model": "override"}},
        },
    }


def test_pipeline_config_parses() -> None:
    config = PipelineConfig.from_dict(_pipeline_dict())
    assert config.order == ("a", "b")
    assert config.stages["b"].llm == {"model": "override"}


def test_order_referencing_undefined_stage_refused() -> None:
    data = _pipeline_dict()
    assert isinstance(data["pipeline"], dict)
    data["pipeline"]["order"] = ["a", "missing"]
    with pytest.raises(ConfigError, match="undefined"):
        PipelineConfig.from_dict(data)


def test_duplicate_order_refused() -> None:
    data = _pipeline_dict()
    assert isinstance(data["pipeline"], dict)
    data["pipeline"]["order"] = ["a", "a"]
    with pytest.raises(ConfigError, match="duplicate"):
        PipelineConfig.from_dict(data)


def test_per_stage_llm_resolution(tmp_path: Path) -> None:
    config = PipelineConfig.from_dict(_pipeline_dict())
    resolved = resolve_stages(config, tmp_path)
    by_id = {stage.id: stage for stage in resolved}
    assert by_id["a"].llm == {
        "provider": "anthropic",
        "model": "base",
        "max_retries": 5,
    }
    assert by_id["b"].llm == {
        "provider": "anthropic",
        "model": "override",
        "max_retries": 5,
    }
    assert by_id["a"].stage_dir == tmp_path / "stages/a"


def test_disabled_stage_dropped(tmp_path: Path) -> None:
    data = _pipeline_dict()
    assert isinstance(data["stages"], dict)
    assert isinstance(data["stages"]["a"], dict)
    data["stages"]["a"]["enabled"] = False
    config = PipelineConfig.from_dict(data)
    resolved = resolve_stages(config, tmp_path)
    assert [stage.id for stage in resolved] == ["b"]
    assert resolved[0].index == 0


def test_run_provides_validated() -> None:
    data = _pipeline_dict()
    data["run"] = {"provides": ["rust_project"]}
    config = PipelineConfig.from_dict(data)
    assert config.run.provides == ("rust_project",)
    data["run"] = {"provides": ["floppy_disk"]}
    with pytest.raises(ConfigError, match="unknown artifact kinds"):
        PipelineConfig.from_dict(data)


def _minimal(extra: dict) -> dict:
    return {"pipeline": {"order": ["a"]}, "stages": {"a": {"uses": "x"}}, **extra}


def test_metrics_settings_default_off() -> None:
    m = PipelineConfig.from_dict(_minimal({})).metrics
    assert not m.after_each_stage and m.idiomaticity == "first_last"


def test_metrics_settings_parse_and_validate() -> None:
    m = PipelineConfig.from_dict(
        _minimal({"metrics": {"after_each_stage": True, "idiomaticity": "all"}})
    ).metrics
    assert m.after_each_stage and m.idiomaticity == "all"
    with pytest.raises(ConfigError):
        PipelineConfig.from_dict(_minimal({"metrics": {"idiomaticity": "sometimes"}}))
    with pytest.raises(ConfigError):
        PipelineConfig.from_dict(_minimal({"metrics": {"after_each_stage": "yes"}}))
