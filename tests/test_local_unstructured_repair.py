from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_local_transformation import (
    FakeClient,
    FakeTools,
    INVALID,
    STAGE_DIR,
    VALID,
    fn_record,
    response,
    run_fake,
    stage_input,
)
from proctor.llm.types import AuthError
from proctor.usage.tracker import read_usage
from protocol import PromptRenderInput, render_prompt
from stage import run_stage
from tooling import CommandResult, CratTools, StageFailure


SEED = "unsafe fn target() { let value = 0; let _ = value; }"
REPAIRED = "unsafe fn target() { for value in 0..0 { let _ = value; } }"


@pytest.mark.parametrize("fragment", ["xj_scanf.jinja", "bytemuck.jinja"])
def test_shared_guidance_updates_both_strategies_without_changing_their_contracts(
    tmp_path, fragment
):
    prompt_dir = tmp_path / "prompts"
    shutil.copytree(STAGE_DIR / "prompts", prompt_dir)
    inputs = [
        PromptRenderInput("", "TARGETS", use_xj_scanf_guidance=True, unstructured=mode)
        for mode in (False, True)
    ]
    original = [render_prompt(value, prompt_dir) for value in inputs]
    inactive = [
        render_prompt(replace(value, use_xj_scanf_guidance=False), prompt_dir)
        for value in inputs
    ]
    shared_path = prompt_dir / "partials" / fragment
    shared_path.write_text(shared_path.read_text() + "\nShared guidance update.\n")
    for value, before in zip(inputs, original, strict=True):
        updated = render_prompt(value, prompt_dir)
        assert updated.text.count("Shared guidance update.") == 1
        assert updated.content_hash != before.content_hash
        assert ("proctor_temp_var_n" in updated.text) == (not value.unstructured)
        assert ("#[proctor(" in updated.text) == (not value.unstructured)
        assert "`u8`, `u16`, `u32`, `u64`, `usize`" in updated.text
        assert "`Vec<u8>`, and `&mut [u8]`" in updated.text
        assert "such as `bytemuck::cast_ref(e)`" in updated.text
        assert updated.version == 1
    if fragment == "xj_scanf.jinja":
        for value, before in zip(inputs, inactive, strict=True):
            updated = render_prompt(
                replace(value, use_xj_scanf_guidance=False), prompt_dir
            )
            assert updated.text == before.text
            assert updated.content_hash == before.content_hash


class UnstructuredTools(FakeTools):
    def __init__(self, *, seed=None, unstructured_results=(), **kwargs):
        super().__init__(**kwargs)
        self.seed = seed
        self.unstructured_results = list(unstructured_results)

    def prepare_unstructured(self, frozen, request, output):
        request_value = json.loads(request.read_text())
        self.events.append(("prepare_unstructured", request_value))
        records = {record["id"]: record for record in self.skeletons}
        targets = [
            {
                "id": item_id,
                "path": records[item_id]["path"],
                "name": records[item_id]["name"],
                "source": re.sub(
                    r"#\[proctor\(\d+\)\]\s*", "", records[item_id]["annotated_source"]
                ),
                "signature": records[item_id]["target_signature"],
            }
            for item_id in request_value["members"]
        ]
        output.write_text(json.dumps({"targets": targets, "candidate": self.seed}))

    def replace_unstructured(self, analysis, current, frozen, request, output):
        request_value = json.loads(request.read_text())
        self.events.append(("replace_unstructured", request_value))
        value = self.unstructured_results.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            records = {record["id"]: record for record in self.skeletons}
            value = {
                "status": "valid",
                "source": value,
                "candidate_sha256": hashlib.sha256(value.encode()).hexdigest(),
                "frozen_project_sha256": hashlib.sha256(
                    frozen.read_bytes()
                ).hexdigest(),
                "new_correspondence": [
                    {
                        "item_id": item_id,
                        "logical_path": records[item_id]["path"],
                        "implementation_path": records[item_id]["path"],
                        "wrapper_path": None,
                    }
                    for item_id in request_value["members"]
                ],
            }
        output.write_text(json.dumps(value))


@pytest.mark.parametrize("trigger", ["limit", "repeat"])
@pytest.mark.parametrize("failure_kind", ["structural", "compilation"])
def test_stripped_seed_can_succeed_without_additional_llm_calls(
    tmp_path, trigger, failure_kind
):
    compilation = failure_kind == "compilation"
    tools = UnstructuredTools(
        seed=SEED,
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[VALID if compilation else INVALID],
        candidates=["failed structured candidate\n"] if compilation else [],
        builds=[CommandResult(0)] + ([CommandResult(101)] if compilation else []),
        unstructured_results=["accepted seed\n"],
    )
    client = FakeClient([response()] * (2 if trigger == "repeat" else 1))
    value, output = run_fake(
        tmp_path, tools, client, config={"max_repairs": 5 if trigger == "repeat" else 0}
    )
    assert output.status == "success", output.error
    assert len(client.requests) == (2 if trigger == "repeat" else 1)
    assert output.metrics["unstructured_repair_calls"] == 0
    assert output.metrics["unstructured_scc_count"] == 1
    assert output.config_used["max_unstructured_repairs"] == 5
    assert [(prompt.id, prompt.version) for prompt in output.prompts] == [
        ("local_transformation", 1)
    ]
    replacements = [
        event for event in tools.events if event[0] == "replace_unstructured"
    ]
    assert replacements[0][1]["transformation"] == SEED
    assert (value.outputs.rust_project / "lib.rs").read_text() == "accepted seed\n"
    assert not [event for event in tools.events if event[0] == "extract_observations"]
    assert not list(
        (value.framework.workdir / "sccs/000000").glob("accepted-*observation*")
    )
    observations = json.loads(
        (value.outputs.artifacts_dir / "observations.json").read_text()
    )
    assert observations["observations"] == observations["printf_observations"] == []
    statistics = json.loads(
        (value.outputs.artifacts_dir / "statistics.json").read_text()
    )
    assert statistics["unstructured_scc_count"] == 1
    assert statistics["statements"]["total"] == 0


@pytest.mark.parametrize("artifacts", [True, False])
def test_unstructured_repair_uses_clean_prompt_fresh_diagnostics_and_usage(
    tmp_path, artifacts
):
    record = fn_record(
        0,
        "target",
        "target",
        [],
        printf_format_specifiers=["%#x"],
        foreign_function_names=["sscanf", "strtod"],
        foreign_static_names=["stdin"],
        proctor_libc_function_paths=["proctor_libc::isalpha"],
    )
    tools = UnstructuredTools(
        seed=SEED,
        skeletons=[record],
        validators=[INVALID],
        builds=[
            CommandResult(0),
            CommandResult(101, "seed stdout", "seed error"),
            CommandResult(0),
        ],
        unstructured_results=["seed failed\n", "accepted free body\n"],
    )
    client = FakeClient([response(), f"```rust\n{REPAIRED}\n```"])
    value, output = run_fake(
        tmp_path,
        tools,
        client,
        artifacts=artifacts,
        config={"max_repairs": 0, "dump_llm_exchanges": True},
    )
    assert output.status == "success", output.error
    prompt = client.requests[1].messages[0].content
    assert "#[proctor(" not in prompt
    assert "Target signature:" in prompt and "Target skeleton:" not in prompt
    assert SEED in prompt
    assert "seed error" in prompt and "seed stdout" in prompt
    assert "missing_label" not in prompt
    assert "proctor_temp_var_" not in prompt
    assert "target skeleton's Rust format string" not in prompt
    for guidance in (
        "proctor_libc::printf::unsigned",
        "proctor_libc::strtod",
        "xj_scanf::legacy::bscanf",
        "bytemuck",
        "proctor_libc::isalpha",
        "stdin().lock()",
    ):
        assert guidance in prompt
    assert output.metrics["unstructured_repair_calls"] == 1
    assert output.metrics["repair_calls"] == 1
    assert output.metrics["llm_generation_calls"] == output.usage.calls == 2
    assert [(entry.id, entry.version) for entry in output.prompts] == [
        ("local_transformation", 1),
        ("unstructured_repair", 1),
    ]
    usage = read_usage(value.framework.workdir / "usage.jsonl")
    assert usage[-1]["prompt_id"] == "unstructured_repair"
    assert usage[-1]["prompt_version"] == 1
    root = value.outputs.artifacts_dir or value.framework.workdir
    exchange = root / "llm-exchanges/scc-0/unstructured-01"
    assert (exchange / "prompt.md").read_text() == prompt
    assert (exchange / "response.md").read_text() == f"```rust\n{REPAIRED}\n```"


def test_unstructured_without_parseable_seed_generates_from_original_and_signature(
    tmp_path,
):
    tools = UnstructuredTools(
        skeletons=[fn_record(0, "target", "target", [])],
        unstructured_results=["accepted\n"],
    )
    client = FakeClient(["no code fence", f"```rust\n{REPAIRED}\n```"])
    _, output = run_fake(tmp_path, tools, client, config={"max_repairs": 0})
    assert output.status == "success", output.error
    prompt = client.requests[1].messages[0].content
    assert "Source:" in prompt and "Target signature:" in prompt
    assert "Previous failed target:" not in prompt
    assert "#[proctor(" not in prompt
    assert output.metrics["unstructured_repair_calls"] == 1


@pytest.mark.parametrize("limit, expected", [(None, 5), (0, 0), (2, 2)])
def test_unstructured_budget_counts_all_additional_calls_and_zero_disables(
    tmp_path, limit, expected
):
    tools = UnstructuredTools(skeletons=[fn_record(0, "target", "target", [])])
    client = FakeClient(
        [
            "structured failure",
            *[f"unfenced reply {index}" for index in range(expected + 1)],
        ]
    )
    config = {"max_repairs": 0}
    if limit is not None:
        config["max_unstructured_repairs"] = limit
    _, output = run_fake(tmp_path, tools, client, config=config)
    assert output.status == "failure"
    assert output.metrics["unstructured_repair_calls"] == expected
    assert output.metrics["repair_calls"] == expected
    assert output.usage.calls == len(client.requests) == expected + 1
    assert output.metrics["unstructured_scc_count"] == 0
    assert ("unstructured repair calls" in output.error) == bool(expected)
    assert len(
        [event for event in tools.events if event[0] == "prepare_unstructured"]
    ) == bool(expected)


def test_last_unstructured_call_can_succeed_and_invalid_targets_are_cleaned(tmp_path):
    tools = UnstructuredTools(
        skeletons=[fn_record(0, "target", "target", [])],
        unstructured_results=[
            {
                "status": "invalid",
                "diagnostics": "remove proctor labels",
                "failed_transformation": SEED,
            },
            "accepted\n",
        ],
    )
    client = FakeClient(["no fence", response(), f"```rust\n{REPAIRED}\n```"])
    _, output = run_fake(
        tmp_path,
        tools,
        client,
        config={"max_repairs": 0, "max_unstructured_repairs": 2},
    )
    assert output.status == "success", output.error
    prompt = client.requests[-1].messages[0].content
    assert SEED in prompt and "remove proctor labels" in prompt
    assert "#[proctor(" not in prompt
    assert output.metrics["unstructured_repair_calls"] == 2


@pytest.mark.parametrize("nonconsecutive", [False, True])
def test_unstructured_repeats_stop_before_replacement_or_build(
    tmp_path, nonconsecutive
):
    candidates = [SEED, REPAIRED] if nonconsecutive else [SEED]
    tools = UnstructuredTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0)] + [CommandResult(101)] * len(candidates),
        unstructured_results=[f"failed {index}\n" for index in range(len(candidates))],
    )
    replies = [f"```rust\n{candidate}\n```" for candidate in candidates]
    client = FakeClient(["no fence", *replies, f"Returned again:\n```\n{SEED}\n```"])
    value, output = run_fake(tmp_path, tools, client, config={"max_repairs": 0})
    assert output.status == "failure"
    assert "repeated unstructured transformation" in output.error
    assert output.metrics["unstructured_repair_calls"] == len(candidates) + 1
    assert output.usage.calls == len(candidates) + 2
    assert len(
        [event for event in tools.events if event[0] == "replace_unstructured"]
    ) == len(candidates)
    assert not value.outputs.rust_project.exists()
    assert (
        value.framework.workdir / "workers/00/project/lib.rs"
    ).read_text() == "normalized\n"


def test_unstructured_seed_is_part_of_fresh_repeat_history_after_it_fails(tmp_path):
    tools = UnstructuredTools(
        seed=SEED,
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID],
        builds=[CommandResult(0), CommandResult(101)],
        unstructured_results=["failed seed\n"],
    )
    client = FakeClient([response(), f"```rust\n{SEED}\n```"])
    _, output = run_fake(tmp_path, tools, client, config={"max_repairs": 0})
    assert output.status == "failure"
    assert "first seen at attempt 0" in output.error
    assert output.metrics["unstructured_repair_calls"] == 1
    assert (
        len([event for event in tools.events if event[0] == "replace_unstructured"])
        == 1
    )


@pytest.mark.parametrize("limit", [-1, True, False, 1.5, "5", None])
def test_unstructured_budget_rejects_negative_or_noninteger_values(tmp_path, limit):
    tools = FakeTools()
    value = stage_input(tmp_path, config={"max_unstructured_repairs": limit})
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert (
        "config.max_unstructured_repairs must be a nonnegative integer" in output.error
    )
    assert not tools.events


def test_terminal_provider_error_during_unstructured_repair_is_recorded_and_fatal(
    tmp_path,
):
    tools = UnstructuredTools(skeletons=[fn_record(0, "target", "target", [])])
    value, output = run_fake(
        tmp_path,
        tools,
        FakeClient(["no fence", AuthError("denied")]),
        config={"max_repairs": 0},
    )
    assert output.status == "failure" and "AuthError: denied" in output.error
    assert output.usage.calls == 2
    assert output.metrics["unstructured_repair_calls"] == 1
    usage = read_usage(value.framework.workdir / "usage.jsonl")
    assert usage[-1]["prompt_id"] == "unstructured_repair"
    assert usage[-1]["error"] == "AuthError: denied"


def test_mixed_strategies_retain_only_structured_scc_observations_and_pairs(tmp_path):
    observation = {"producer": "first", "sentinel": True}
    tools = UnstructuredTools(
        seed="unsafe fn second() {}",
        skeletons=[
            fn_record(0, "first", "first", []),
            fn_record(1, "second", "second", []),
        ],
        validators=[VALID, INVALID],
        candidates=["structured first\n"],
        unstructured_results=["unstructured second\n"],
        observations=[
            {
                "schema_version": 1,
                "observations": [observation],
                "printf_observations": [],
            }
        ],
    )
    value, output = run_fake(
        tmp_path,
        tools,
        FakeClient([response("first"), response("second")]),
        config={"max_repairs": 0},
    )
    assert output.status == "success", output.error
    document = json.loads(
        (value.outputs.artifacts_dir / "observations.json").read_text()
    )
    assert document["observations"] == [observation]
    assert (
        len([event for event in tools.events if event[0] == "extract_observations"])
        == 1
    )
    report = (value.outputs.artifacts_dir / "statement-pairs.md").read_text()
    assert "first" in report and "second" not in report
    statistics = json.loads(
        (value.outputs.artifacts_dir / "statistics.json").read_text()
    )
    assert (
        statistics["statements"]["transform"] == statistics["statements"]["total"] == 1
    )
    assert statistics["unstructured_scc_count"] == 1
    assert statistics["function_scc_count"] == 2


def test_unstructured_build_must_preserve_candidate_source_bytes(tmp_path):
    class MutatingTools(UnstructuredTools):
        def cargo_build(self, current, *, library_only=False):
            result = super().cargo_build(current, library_only=library_only)
            if (current / "lib.rs").read_text() == "unstructured candidate\n":
                (current / "lib.rs").write_text("changed by builder\n")
            return result

    tools = MutatingTools(
        seed=SEED,
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID],
        unstructured_results=["unstructured candidate\n"],
    )
    value, output = run_fake(
        tmp_path, tools, FakeClient([response()]), config={"max_repairs": 0}
    )
    assert output.status == "failure"
    assert (
        "successful cargo build changed unstructured candidate source bytes"
        in output.error
    )
    assert not value.outputs.rust_project.exists()


def test_accepted_unstructured_source_is_rechecked_before_assembly(tmp_path):
    class RetainedMutationTools(UnstructuredTools):
        def extract_observations(self, observation_source, metadata, output):
            super().extract_observations(observation_source, metadata, output)
            retained = output.parent.parent / "000000/accepted.rs"
            retained.write_text("tampered accepted source\n")

    tools = RetainedMutationTools(
        seed="unsafe fn first() {}",
        skeletons=[
            fn_record(0, "first", "first", []),
            fn_record(1, "second", "second", []),
        ],
        validators=[INVALID, VALID],
        candidates=["structured second\n"],
        unstructured_results=["unstructured first\n"],
    )
    value, output = run_fake(
        tmp_path,
        tools,
        FakeClient([response("first"), response("second")]),
        config={"max_repairs": 0},
    )
    assert output.status == "failure"
    assert "accepted unstructured source digest mismatch" in output.error
    assert not [event for event in tools.events if event[0] == "assemble_project"]
    assert not value.outputs.rust_project.exists()


@pytest.mark.parametrize("mutation", ["float_id", "wrong_signature", "extra_field"])
def test_unstructured_preparation_protocol_errors_are_fatal(tmp_path, mutation):
    class InvalidPreparationTools(UnstructuredTools):
        def prepare_unstructured(self, frozen, request, output):
            super().prepare_unstructured(frozen, request, output)
            prepared = json.loads(output.read_text())
            if mutation == "float_id":
                prepared["targets"][0]["id"] = 0.0
            elif mutation == "wrong_signature":
                prepared["targets"][0]["signature"] = "unsafe fn wrong()"
            else:
                prepared["extra"] = True
            output.write_text(json.dumps(prepared))

    tools = InvalidPreparationTools(
        seed=SEED,
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID],
    )
    _, output = run_fake(
        tmp_path, tools, FakeClient([response()]), config={"max_repairs": 0}
    )
    assert output.status == "failure"
    assert "unstructured preparation" in output.error
    assert output.metrics["unstructured_repair_calls"] == 0
    assert not [event for event in tools.events if event[0] == "replace_unstructured"]


@pytest.mark.parametrize(
    "mutation",
    ["candidate_digest", "frozen_digest", "identity", "extra_field", "status"],
)
def test_unstructured_candidate_protocol_errors_are_fatal(tmp_path, mutation):
    class InvalidCandidateTools(UnstructuredTools):
        def replace_unstructured(self, analysis, current, frozen, request, output):
            super().replace_unstructured(analysis, current, frozen, request, output)
            candidate = json.loads(output.read_text())
            if mutation == "candidate_digest":
                candidate["candidate_sha256"] = "0" * 64
            elif mutation == "frozen_digest":
                candidate["frozen_project_sha256"] = "0" * 64
            elif mutation == "identity":
                candidate["new_correspondence"][0]["logical_path"] = "other"
            elif mutation == "extra_field":
                candidate["extra"] = True
            else:
                candidate["status"] = "unknown"
            output.write_text(json.dumps(candidate))

    tools = InvalidCandidateTools(
        seed=SEED,
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID],
        unstructured_results=["candidate\n"],
    )
    value, output = run_fake(
        tmp_path, tools, FakeClient([response()]), config={"max_repairs": 0}
    )
    assert output.status == "failure"
    assert "unstructured" in output.error
    assert output.metrics["unstructured_repair_calls"] == 0
    assert output.metrics["cargo_builds"] == 1
    assert not value.outputs.rust_project.exists()


@pytest.mark.parametrize("operation", ["prepare", "replace"])
def test_unstructured_tool_commands_preserve_inputs_and_require_fresh_outputs(
    tmp_path, operation
):
    frozen = tmp_path / "frozen.json"
    request = tmp_path / "request.json"
    frozen.write_text("frozen")
    request.write_text("request")
    analysis = tmp_path / "analysis"
    current = tmp_path / "current"
    analysis.mkdir()
    current.mkdir()
    output = tmp_path / "response.json"
    calls = []

    def runner(command, *, cwd=None, env=None):
        calls.append(command)
        assert not output.exists()
        return CommandResult(0)

    tools = CratTools(tmp_path / "tool.log", run_command=runner)
    tools.crat_tool = Path("/tools/crat-tool")

    def invoke(destination):
        if operation == "prepare":
            tools.prepare_unstructured(frozen, request, destination)
        else:
            tools.replace_unstructured(analysis, current, frozen, request, destination)

    for protected in (frozen, request):
        with pytest.raises(StageFailure, match="overlaps an input"):
            invoke(protected)
    assert not calls
    assert frozen.read_text() == "frozen" and request.read_text() == "request"
    output.write_text("stale response")
    with pytest.raises(StageFailure, match="output"):
        invoke(output)
    expected = [
        "/tools/crat-tool",
        "prepare-unstructured-repair"
        if operation == "prepare"
        else "replace-unstructured-scc",
        "--frozen",
        str(frozen),
        "--request",
        str(request),
    ]
    if operation == "replace":
        expected += ["--current-project", str(current)]
    expected += ["--output", str(output)]
    if operation == "replace":
        expected.append(str(analysis))
    assert calls == [expected]
    assert not output.exists()
