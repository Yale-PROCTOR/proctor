"""Keep the JSON Schemas and the dataclass serialization in sync."""

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.test_stage_io import sample_input, sample_output

SCHEMAS = Path(__file__).parent.parent / "proctor" / "contracts" / "schemas"


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _errors(validator: Draft202012Validator, payload: dict[str, Any]) -> list[str]:
    return [error.message for error in validator.iter_errors(payload)]


def test_sample_input_conforms() -> None:
    assert _errors(_validator("stage_input.v1.json"), sample_input().to_dict()) == []


@pytest.mark.parametrize("no_fail", [False, True])
def test_input_schema_accepts_no_fail(no_fail: bool) -> None:
    payload = sample_input().to_dict()
    payload["framework"]["no_fail"] = no_fail
    assert _errors(_validator("stage_input.v1.json"), payload) == []


@pytest.mark.parametrize("value", ["true", 1, None])
def test_input_schema_rejects_non_boolean_no_fail(value: object) -> None:
    payload = sample_input().to_dict()
    payload["framework"]["no_fail"] = value
    assert _errors(_validator("stage_input.v1.json"), payload)


def test_sample_output_conforms() -> None:
    assert _errors(_validator("stage_output.v1.json"), sample_output().to_dict()) == []


def test_schema_rejects_failure_without_error() -> None:
    payload = sample_output().to_dict()
    payload["status"] = "failure"
    payload["error"] = None
    assert _errors(_validator("stage_output.v1.json"), payload)


def test_schema_rejects_bad_status() -> None:
    payload = sample_output().to_dict()
    payload["status"] = "partial"
    assert _errors(_validator("stage_output.v1.json"), payload)
