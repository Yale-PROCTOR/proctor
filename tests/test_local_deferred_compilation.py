from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_local_final_validation import suite_result
from tests.test_local_transformation import (
    FakeClient,
    FakeTools,
    INVALID,
    STAGE_DIR,
    VALID,
    fn_record,
    patch_response,
    response,
    stage_input,
)
from stage import run_stage
from tooling import CommandResult, CratTools, StageFailure


MODEL = "fixture/compilation-agent"


class DeferredTools(FakeTools):
    def assemble_project(self, frozen, accepted, output, *, stubbed_items=()):
        super().assemble_project(frozen, accepted, output, stubbed_items=stubbed_items)
        if accepted:
            output.write_text("\n".join(path.read_text() for _, path in accepted))

    def cargo_build(self, current, *, library_only=False):
        super().cargo_build(current, library_only=library_only)
        source = (current / "lib.rs").read_text()
        return (
            CommandResult(101, stderr="unresolved generated value")
            if "missing_value" in source
            else CommandResult(0)
        )


def run(value, tools, client, runner=None):
    return run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: client,
        validation_runner=runner,
    )


def test_exhausted_groups_all_reach_compilation_agent_before_postprocess(tmp_path):
    bodies = [
        "unsafe fn first() { missing_value(); }",
        "unsafe fn second() { missing_value(); }",
    ]
    tools = DeferredTools(
        skeletons=[
            fn_record(0, "first", "first", []),
            fn_record(1, "second", "second", []),
        ],
        validators=[INVALID, INVALID],
        retained=[{"source": body, "stubbed_items": []} for body in bodies],
    )
    value = stage_input(
        tmp_path,
        config={
            "max_group_chars": 1,
            "max_repairs": 0,
            "max_unstructured_repairs": 0,
            "validation_repair_model": MODEL,
        },
    )
    client = FakeClient([response("first"), response("second")])
    calls = []

    def repair(command, **kwargs):
        calls.append(command)
        assert command[0] == "opencode"
        assert len(client.requests) == 2
        assert not any(event[0] == "postprocess" for event in tools.events)
        project = kwargs["cwd"]
        assert all(body in (project / "lib.rs").read_text() for body in bodies)
        # Agent edits are accepted solely by the independent Cargo result.
        (project / "lib.rs").write_text(
            "struct Changed { value: i32 }\nstatic VALUE: i32 = 1;\n"
            "unsafe fn first(_: u32) {}\nunsafe fn second() {}\n"
        )
        (project / "tests").mkdir()
        (project / "tests/agent_change.rs").write_text("// accepted agent edit\n")
        (project / "config.toml").write_text("agent_changed = true\n")
        return CommandResult(0)

    output = run(value, tools, client, repair)
    assert output.status == "success", output.error
    assert len(calls) == 1
    assert output.metrics["retained_functions"] == 2
    assert output.metrics["compilation_repair_calls"] == 1
    published = (value.outputs.rust_project / "lib.rs").read_text()
    assert "struct Changed" in published
    assert "static VALUE: i32 = 1" in published
    assert "unsafe fn first(_: u32)" in published
    assert (value.outputs.rust_project / "config.toml").read_text() == (
        "agent_changed = true\n"
    )
    assert (value.outputs.rust_project / "tests/agent_change.rs").exists()
    assert (value.inputs.rust_project / "lib.rs").read_text() == "old\n"


def test_history_and_explicit_stub_identity_survive_a_malformed_last_response(tmp_path):
    original = "unsafe fn first() { missing_value(); }"
    malformed = "unsafe fn first() { missing_value();"
    retained = original + "\nunsafe fn second() { todo!() }"
    tools = DeferredTools(
        skeletons=[
            fn_record(0, "first", "first", []),
            fn_record(1, "second", "second", []),
        ],
        validators=[INVALID, INVALID],
        retained=[{"source": retained, "stubbed_items": [1]}],
    )
    value = stage_input(
        tmp_path, config={"max_repairs": 1, "max_unstructured_repairs": 0}
    )
    client = FakeClient(
        [f"```rust\n{original}\n```", patch_response(original, malformed)]
    )
    # This fixture isolates the compiler-tool retention contract; Rust library
    # tests exercise the actual parsing and per-function selection.
    tools.cargo_build = FakeTools.cargo_build.__get__(tools)
    output = run(value, tools, client)
    assert output.status == "success", output.error
    request = next(event[1] for event in tools.events if event[0] == "retain_functions")
    assert request["transformations"] == [original, malformed]
    assert ("assembled_stubs", (1,)) in tools.events
    assert (value.outputs.rust_project / "lib.rs").read_text() == retained
    report = json.loads(
        (value.outputs.artifacts_dir / "compilation-repair.json").read_text()
    )
    assert report["retained_functions"] == [
        {"id": 0, "path": "first", "stubbed": False},
        {"id": 1, "path": "second", "stubbed": True},
    ]


def test_broken_initial_template_defers_builds_without_local_compile_repairs(tmp_path):
    tools = DeferredTools(
        normalized="missing_value template\n",
        skeletons=[
            fn_record(0, "first", "first", []),
            fn_record(1, "second", "second", []),
        ],
        validators=[VALID, VALID],
        candidates=["missing_value first\n", "missing_value second\n"],
        retained=[
            {"source": "missing_value first\n", "stubbed_items": []},
            {"source": "missing_value second\n", "stubbed_items": []},
        ],
    )
    value = stage_input(
        tmp_path, config={"max_group_chars": 1, "validation_repair_model": MODEL}
    )
    client = FakeClient([response("first"), response("second")])

    def repair(command, **kwargs):
        assert tools.build_modes == [True, False]
        (kwargs["cwd"] / "lib.rs").write_text("fixed all generated code\n")
        return CommandResult(0)

    output = run(value, tools, client, repair)
    assert output.status == "success", output.error
    assert len(client.requests) == 2
    assert output.metrics["repair_calls"] == 0
    assert tools.build_modes == [True, False, False, False]
    assert (value.framework.workdir / "initial-build-diagnostics.txt").exists()


@pytest.mark.parametrize("limit", [1, 2])
def test_pre_and_postprocess_failures_share_one_compilation_budget(tmp_path, limit):
    class PostprocessFailureTools(DeferredTools):
        def finalize_project(self, *args, **kwargs):
            super().finalize_project(*args, **kwargs)
            args[3].write_text("missing_value before postprocess\n")

        def postprocess(self, project):
            super().postprocess(project)
            (project / "lib.rs").write_text("missing_value after postprocess\n")

    tools = PostprocessFailureTools()
    value = stage_input(
        tmp_path,
        config={"max_compilation_repairs": limit, "validation_repair_model": MODEL},
    )
    calls = []

    def repair(command, **kwargs):
        calls.append(command)
        (kwargs["cwd"] / "lib.rs").write_text("fixed by compilation agent\n")
        return CommandResult(0)

    output = run(value, tools, FakeClient([]), repair)
    assert output.status == ("success" if limit == 2 else "failure"), output.error
    assert len(calls) == limit
    assert output.metrics["compilation_repair_calls"] == limit
    if limit == 1:
        partial = value.outputs.artifacts_dir / "compilation-repair-project/lib.rs"
        assert partial.read_text() == "missing_value after postprocess\n"
        assert not value.outputs.rust_project.exists()


def test_compilation_and_test_repair_have_independent_budgets(tmp_path):
    tools = DeferredTools(normalized="missing_value template\n")
    value = stage_input(
        tmp_path,
        config={
            "max_compilation_repairs": 1,
            "max_validation_repairs": 1,
            "validation_repair_model": MODEL,
        },
    )
    (value.inputs.rust_project / "testinfra.toml").write_text('strategy = "cando2"\n')
    phases = []
    validations = []

    def runner(command, **kwargs):
        if command[0] == "opencode":
            prompt = Path(command[command.index("-f") + 1]).read_text()
            phase = (
                "compile"
                if "This is compilation repair invocation" in prompt
                else "test"
            )
            phases.append(phase)
            (kwargs["cwd"] / "lib.rs").write_text("fixed " + phase + "\n")
            return CommandResult(0)
        validations.append(command)
        return suite_result(failures=int(len(validations) == 1))

    output = run(value, tools, FakeClient([]), runner)
    assert output.status == "success", output.error
    assert phases == ["compile", "test"]
    assert output.metrics["compilation_repair_calls"] == 1
    report = json.loads(
        (value.outputs.artifacts_dir / "test-validation.json").read_text()
    )
    assert report["repair_attempts"] == 1
    assert report["status"] == "validation_succeeded"


@pytest.mark.parametrize(
    "field,value", [("candidate_sha256", "bad"), ("stubbed_items", [5])]
)
def test_retained_protocol_errors_remain_fatal(tmp_path, field, value):
    retained = {"source": "unsafe fn target() {}", "stubbed_items": [], field: value}
    tools = DeferredTools(
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID],
        retained=[retained],
    )
    value = stage_input(
        tmp_path, config={"max_repairs": 0, "max_unstructured_repairs": 0}
    )
    output = run(value, tools, FakeClient([response()]))
    assert output.status == "failure"
    assert not any(event[0] == "assemble_project" for event in tools.events)


@pytest.mark.parametrize("limit", [-1, True, False, 1.5, "3", None])
def test_invalid_compilation_budget_fails_before_tools(tmp_path, limit):
    tools = FakeTools()
    value = stage_input(tmp_path, config={"max_compilation_repairs": limit})
    output = run(value, tools, FakeClient([]))
    assert output.status == "failure"
    assert "max_compilation_repairs" in output.error
    assert not tools.events


@pytest.mark.parametrize(
    "relative",
    [
        "compilation-repair.json",
        "compilation-repair-logs/usage.jsonl",
        "compilation-repair-project/usage.jsonl",
    ],
)
def test_compilation_artifacts_cannot_overwrite_usage_log(tmp_path, relative):
    value = stage_input(tmp_path)
    usage = value.outputs.artifacts_dir / relative
    usage.parent.mkdir(parents=True, exist_ok=True)
    usage.write_text("preserve usage\n")
    value = replace(value, framework=replace(value.framework, usage_log=usage))
    tools = FakeTools()
    output = run(value, tools, FakeClient([]))
    assert output.status == "failure"
    assert not tools.events
    assert usage.read_text() == "preserve usage\n"


def test_partial_project_cannot_overlap_published_destination(tmp_path):
    value = stage_input(tmp_path)
    destination = value.outputs.artifacts_dir / "compilation-repair-project/output"
    value = replace(value, outputs=replace(value.outputs, rust_project=destination))
    tools = FakeTools()
    output = run(value, tools, FakeClient([]))
    assert output.status == "failure"
    assert not tools.events


def test_assembly_request_identifies_explicit_stubs_without_changing_inputs(tmp_path):
    frozen = tmp_path / "frozen.json"
    first = tmp_path / "first.rs"
    second = tmp_path / "second.rs"
    output = tmp_path / "assembled.rs"
    frozen.write_text("{}")
    first.write_text("first body")
    second.write_text("typed second stub")
    requests = []

    def runner(command, **kwargs):
        request = Path(command[command.index("--request") + 1])
        requests.append(json.loads(request.read_text()))
        Path(command[command.index("--output") + 1]).write_text("assembled")
        return CommandResult(0)

    tools = CratTools(tmp_path / "tools.log", run_command=runner)
    tools.crat_tool = tmp_path / "crat-tool"
    tools.assemble_project(
        frozen, (((0,), first), ((1,), second)), output, stubbed_items=(1,)
    )
    assert requests == [
        [
            {"members": [0], "source": "first body", "stubbed_items": []},
            {"members": [1], "source": "typed second stub", "stubbed_items": [1]},
        ]
    ]
    assert first.read_text() == "first body"
    assert second.read_text() == "typed second stub"
    assert frozen.read_text() == "{}"


def test_intermediate_postprocess_compile_failure_is_repaired_before_retry(tmp_path):
    class IntermediateFailureTools(DeferredTools):
        calls = 0

        def postprocess(self, project):
            super().postprocess(project)
            self.calls += 1
            (project / "tests/oracle.rs").unlink()
            if self.calls == 1:
                (project / "lib.rs").write_text(
                    "missing_value introduced by postprocess\n"
                )
                raise StageFailure("postprocess source no longer compiles")

    value = stage_input(tmp_path, config={"validation_repair_model": MODEL})
    (value.inputs.rust_project / "tests").mkdir()
    (value.inputs.rust_project / "tests/oracle.rs").write_text("original harness\n")
    tools = IntermediateFailureTools()
    calls = []

    def repair(command, **kwargs):
        calls.append(command)
        project = kwargs["cwd"]
        (project / "lib.rs").write_text("fixed intermediate source\n")
        (project / "tests/oracle.rs").write_text("agent harness edit\n")
        return CommandResult(0)

    output = run(value, tools, FakeClient([]), repair)
    assert output.status == "success", output.error
    assert tools.calls == 2
    assert len(calls) == 1
    assert (
        value.outputs.rust_project / "tests/oracle.rs"
    ).read_text() == "agent harness edit\n"
