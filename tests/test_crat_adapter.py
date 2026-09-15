"""Focused tests for CRAT adapter pass configuration."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).parent.parent
ADAPTER_MAIN = REPO / "stages" / "crat-adapter" / "main.py"


def _load_adapter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crat_adapter_main", ADAPTER_MAIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = _load_adapter()


def test_default_pass_plan_is_unchanged() -> None:
    plan = ADAPTER.resolve_pass_plan({})

    assert [plugin for plugin, _ in plan] == [
        "expand",
        "extern",
        "preprocess",
        "outparam",
        "punning",
        "enum",
        "pointer",
        "io",
        "libc",
        "static",
        "simpl",
        "interface",
        "unsafe",
        "unexpand",
        "split",
        "bin",
    ]
    assert "prepare" not in [plugin for plugin, _ in plan]
    assert dict(plan)["extern"] == [
        "--extern-ignore-return-type",
        "--extern-ignore-param-type",
    ]
    assert dict(plan)["outparam"] == ["--outparam-simplify"]
    assert dict(plan)["io"] == ["--io-assume-to-str-ok"]
    assert dict(plan)["unsafe"] == [
        "--unsafe-remove-unused",
        "--unsafe-remove-no-mangle",
        "--unsafe-replace-pub",
        "--unsafe-remove-extern-c",
    ]
    assert dict(plan)["unexpand"] == ["--unexpand-use-print"]


def test_final_pass_still_selects_canonical_prefix() -> None:
    plan = ADAPTER.resolve_pass_plan({"final_pass": "pointer"})

    assert [plugin for plugin, _ in plan] == [
        "expand",
        "extern",
        "preprocess",
        "outparam",
        "punning",
        "enum",
        "pointer",
    ]


def test_prepare_is_an_explicit_opt_in_branch() -> None:
    assert ADAPTER.resolve_pass_plan({"final_pass": "prepare"}) == [
        ("expand", []),
        (
            "extern",
            ["--extern-ignore-return-type", "--extern-ignore-param-type"],
        ),
        ("preprocess", []),
        ("outparam", ["--outparam-simplify"]),
        ("punning", []),
        ("enum", []),
        ("prepare", []),
    ]
    assert ADAPTER.resolve_pass_plan({"passes": ["enum", "prepare", "simpl"]}) == [
        ("enum", []),
        ("prepare", []),
        ("simpl", []),
    ]


def test_prepare_arguments_use_generic_replacement_semantics() -> None:
    assert ADAPTER.resolve_pass_plan(
        {"passes": ["prepare"], "pass_args": {"prepare": ["--generic"]}}
    ) == [("prepare", ["--generic"])]
    with pytest.raises(ADAPTER.StageFailure, match="passes not selected"):
        ADAPTER.resolve_pass_plan({"passes": ["enum"], "pass_args": {"prepare": []}})


def test_custom_sequence_and_replacement_arguments() -> None:
    plan = ADAPTER.resolve_pass_plan(
        {
            "passes": ["unsafe", "expand"],
            "pass_args": {
                "unsafe": [],
                "expand": ["--extra-option"],
            },
        }
    )

    assert plan == [("unsafe", []), ("expand", ["--extra-option"])]


def test_custom_sequence_retains_unoverridden_defaults() -> None:
    plan = ADAPTER.resolve_pass_plan({"passes": ["extern"]})

    assert plan == [
        (
            "extern",
            ["--extern-ignore-return-type", "--extern-ignore-param-type"],
        )
    ]


def test_run_pass_uses_resolved_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "input"
    output_root = tmp_path / "output"
    input_dir.mkdir()
    commands: list[list[str]] = []

    def fake_run_logged(command: list[str], *_args: object, **_kwargs: object) -> None:
        commands.append(command)
        (output_root / input_dir.name).mkdir(parents=True)

    monkeypatch.setattr(ADAPTER, "run_logged", fake_run_logged)

    produced = ADAPTER.run_pass(
        Path("/crat"),
        {},
        "extern",
        ["--custom-option"],
        input_dir,
        output_root,
        tmp_path / "crat.log",
    )

    assert produced == output_root / input_dir.name
    assert commands[0][7:-1] == ["--custom-option"]


def test_run_pass_selects_prepare_without_special_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "input"
    output_root = tmp_path / "output"
    input_dir.mkdir()
    commands: list[list[str]] = []

    def fake_run_logged(command: list[str], *_args: object, **_kwargs: object) -> None:
        commands.append(command)
        (output_root / input_dir.name).mkdir(parents=True)

    monkeypatch.setattr(ADAPTER, "run_logged", fake_run_logged)
    ADAPTER.run_pass(
        Path("/crat"),
        {},
        "prepare",
        [],
        input_dir,
        output_root,
        tmp_path / "crat.log",
    )

    assert "--pass" in commands[0]
    assert commands[0][commands[0].index("--pass") + 1] == "prepare"
    assert commands[0][7:-1] == []


def test_local_pipeline_selects_prepare_in_exact_order() -> None:
    import tomllib

    config = tomllib.loads(
        (REPO / "configs" / "c2rust_crat_local.toml").read_text(encoding="utf-8")
    )
    crat = config["stages"]["crat"]["config"]

    assert crat["passes"] == [
        "expand",
        "extern",
        "preprocess",
        "enum",
        "prepare",
        "simpl",
        "unsafe",
        "unexpand",
        "split",
        "bin",
    ]
    assert "libc" not in crat["passes"]
    assert "pass_args" not in crat


@pytest.mark.parametrize(
    ("check_build_config", "expected_build"),
    [
        ({}, True),
        ({"check_build": False}, False),
    ],
    ids=["default", "disabled"],
)
def test_run_stage_build_check_default_and_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check_build_config: dict[str, bool],
    expected_build: bool,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("c_exposed_fns = []\n", encoding="utf-8")
    (source / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n[lib]\nname = "demo"\n',
        encoding="utf-8",
    )
    crat_dir = tmp_path / "crat"
    crat_dir.mkdir()
    commands: list[tuple[list[str], Path | None]] = []

    def fake_run_pass(
        _crat_bin: Path,
        _env: dict[str, str],
        _plugin: str,
        _flags: list[str],
        input_dir: Path,
        output_root: Path,
        _log_file: Path,
    ) -> Path:
        produced = output_root / input_dir.name
        shutil.copytree(input_dir, produced)
        return produced

    def fake_run_logged(
        command: list[str],
        _log_file: Path,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        commands.append((command, cwd))
        assert env is not None and env["RUSTFLAGS"] == "-Awarnings"
        assert cwd is not None
        (cwd / "target").mkdir()

    monkeypatch.setattr(ADAPTER, "ensure_crat_built", lambda *_args: Path("/crat"))
    monkeypatch.setattr(ADAPTER, "crat_env", lambda *_args: {})
    monkeypatch.setattr(ADAPTER, "run_pass", fake_run_pass)
    monkeypatch.setattr(ADAPTER, "run_logged", fake_run_logged)
    monkeypatch.setattr(ADAPTER, "git_head", lambda *_args: "test-commit")

    destination = tmp_path / "destination"
    output = ADAPTER.run_stage(
        {
            "inputs": {"rust_project": str(source)},
            "outputs": {
                "rust_project": str(destination),
                "artifacts_dir": str(tmp_path / "artifacts"),
            },
            "framework": {"workdir": str(tmp_path / "work")},
            "config": {
                "passes": ["expand"],
                "crat_dir": str(crat_dir),
                **check_build_config,
            },
        }
    )

    expected_commands = (
        [(["cargo", "build"], tmp_path / "work" / "expand" / "source")]
        if expected_build
        else []
    )
    assert commands == expected_commands
    assert output["config_used"]["check_build"] is expected_build
    assert ("check_build_s" in output["metrics"]) is expected_build
    assert not (destination / "target").exists()
    assert (destination / "proctor.toml").is_file()


def test_run_stage_rejects_non_boolean_check_build(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("", encoding="utf-8")

    with pytest.raises(ADAPTER.StageFailure, match="check_build must be a boolean"):
        ADAPTER.run_stage(
            {
                "inputs": {"rust_project": str(source)},
                "outputs": {
                    "rust_project": str(tmp_path / "destination"),
                    "artifacts_dir": str(tmp_path / "artifacts"),
                },
                "framework": {"workdir": str(tmp_path / "work")},
                "config": {"check_build": "yes"},
            }
        )


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {"passes": ["expand"], "final_pass": "bin"},
            "cannot set both passes and final_pass",
        ),
        ({"passes": []}, "passes must be a non-empty list"),
        ({"passes": ["unknown"]}, "passes must be a non-empty list"),
        ({"passes": ["expand", "expand"]}, "must not contain duplicates"),
        ({"pass_args": []}, "pass_args must be a table"),
        ({"pass_args": {"unknown": []}}, "contains unknown CRAT passes"),
        (
            {"passes": ["expand"], "pass_args": {"extern": []}},
            "passes not selected",
        ),
        (
            {"passes": ["expand"], "pass_args": {"expand": "--flag"}},
            "must be a list of strings",
        ),
    ],
)
def test_invalid_pass_config_is_rejected(config: dict, message: str) -> None:
    with pytest.raises(ADAPTER.StageFailure, match=message):
        ADAPTER.resolve_pass_plan(config)
