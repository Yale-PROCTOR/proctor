"""Envelope round-trips and contract validation."""

from pathlib import Path

import pytest

from proctor.contracts import (
    Budget,
    ContractError,
    FrameworkSettings,
    InputArtifacts,
    ModelInfo,
    OutputDestinations,
    PromptUse,
    StageInput,
    StageOutput,
    UsageSummary,
)


def sample_input() -> StageInput:
    return StageInput(
        run_id="run-1",
        stage_id="example",
        stage_index=0,
        item="B01/001_helloworld",
        inputs=InputArtifacts(
            c_project=Path("/data/c"),
            rust_project=Path("/data/rust"),
            test_package=Path("/data/tests"),
        ),
        outputs=OutputDestinations(
            rust_project=Path("/out/rust"),
            artifacts_dir=Path("/out/artifacts"),
        ),
        config={"max_iterations": 5},
        framework=FrameworkSettings(
            llm={"provider": "anthropic", "model": "claude-opus-4-8"},
            usage_log=Path("/out/usage.jsonl"),
            budget=Budget(max_usd=5.0),
            timeout_s=3600,
        ),
    )


def sample_output() -> StageOutput:
    return StageOutput(
        status="success",
        stage_id="example",
        stage_version="0.1.0",
        outputs=OutputDestinations(rust_project=Path("/out/rust")),
        config_used={"max_iterations": 5},
        models=(ModelInfo(provider="anthropic", model="claude-opus-4-8"),),
        usage=UsageSummary(calls=3, input_tokens=100, output_tokens=50),
        prompts=(PromptUse(id="wrapper_preserve_update", version=3),),
        metrics={"build_ok": True},
        logs=("stdout.log",),
    )


def test_stage_input_round_trip(tmp_path: Path) -> None:
    original = sample_input()
    original.write(tmp_path / "stage_input.json")
    loaded = StageInput.read(tmp_path / "stage_input.json")
    assert loaded == original


def test_old_stage_input_defaults_no_fail_to_false() -> None:
    payload = sample_input().to_dict()
    del payload["framework"]["no_fail"]
    assert StageInput.from_dict(payload).framework.no_fail is False


@pytest.mark.parametrize("no_fail", [False, True])
def test_stage_input_preserves_no_fail(tmp_path: Path, no_fail: bool) -> None:
    payload = sample_input().to_dict()
    payload["framework"]["no_fail"] = no_fail
    original = StageInput.from_dict(payload)
    path = tmp_path / "stage_input.json"
    original.write(path)
    assert StageInput.read(path).framework.no_fail is no_fail


@pytest.mark.parametrize("value", ["true", 1, 0, None, [], {}])
def test_stage_input_rejects_non_boolean_no_fail(value: object) -> None:
    payload = sample_input().to_dict()
    payload["framework"]["no_fail"] = value
    with pytest.raises(ContractError, match="no_fail.*boolean"):
        StageInput.from_dict(payload)


def test_stage_output_round_trip(tmp_path: Path) -> None:
    original = sample_output()
    original.write(tmp_path / "stage_output.json")
    loaded = StageOutput.read(tmp_path / "stage_output.json")
    assert loaded == original


def test_newer_schema_version_refused() -> None:
    data = sample_input().to_dict()
    data["schema_version"] = 99
    with pytest.raises(ContractError, match="newer"):
        StageInput.from_dict(data)


def test_failure_requires_error() -> None:
    with pytest.raises(ContractError, match="non-empty 'error'"):
        StageOutput(status="failure", stage_id="example")


def test_success_must_not_carry_error() -> None:
    with pytest.raises(ContractError, match="null"):
        StageOutput(status="success", stage_id="example", error="boom")


def test_invalid_status_refused() -> None:
    data = sample_output().to_dict()
    data["status"] = "partial"
    with pytest.raises(ContractError, match="status"):
        StageOutput.from_dict(data)


def test_unknown_fields_ignored() -> None:
    data = sample_input().to_dict()
    data["future_field"] = {"anything": 1}
    loaded = StageInput.from_dict(data)
    assert loaded == sample_input()


def test_missing_inputs_object_refused() -> None:
    data = sample_input().to_dict()
    del data["inputs"]
    with pytest.raises(ContractError, match="inputs"):
        StageInput.from_dict(data)


def test_negative_usage_refused() -> None:
    data = sample_output().to_dict()
    assert isinstance(data["usage"], dict)
    data["usage"]["input_tokens"] = -1
    with pytest.raises(ContractError, match=">= 0"):
        StageOutput.from_dict(data)


def test_skipped_round_trips() -> None:
    skipped = StageOutput(status="skipped", stage_id="example")
    assert StageOutput.from_dict(skipped.to_dict()) == skipped
