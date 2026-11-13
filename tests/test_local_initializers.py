from __future__ import annotations

import json
from pathlib import Path

import pytest

from model import SkeletonError, dependency_context, load_skeletons
from proctor.usage.tracker import read_usage
from stage import run_stage
from tests.test_local_transformation import (
    FakeClient,
    FakeTools,
    STAGE_DIR,
    VALID,
    fn_record,
    patch_response,
    response,
    stage_input,
    type_record,
    value_record,
)
from tooling import CommandResult, CratTools, StageFailure


BEFORE_TYPE = "pub struct Cell { pub value: *const i32 }"
AFTER_TYPE = "pub struct Cell<'a> { pub value: Option<&'a i32> }"
BEFORE = "static mut CACHE: Cell = Cell { value: core::ptr::null() };"
AFTER = "static mut CACHE: Cell = Cell { value: None };"


def initializer_record(*, block=False, needs=True, kind="Static", item_id=0):
    source = BEFORE
    skeleton = "static mut CACHE: Cell = todo!();"
    dispositions = []
    if block:
        source = (
            "static mut CACHE: Cell = {\n"
            "    #[proctor(0)]\n"
            "    let mut cell: Cell = Cell { value: core::ptr::null() };\n"
            "    #[proctor(1)]\n"
            "    cell\n"
            "};"
        )
        skeleton = source.replace("Cell { value: core::ptr::null() }", "todo!()")
        dispositions = [
            {"label": 0, "disposition": "transform", "children": []},
            {"label": 1, "disposition": "preserve", "children": []},
        ]
    if not needs:
        skeleton = source
        for node in dispositions:
            node["disposition"] = "preserve"
    if kind == "Const":
        source = source.replace("static mut", "const")
        skeleton = skeleton.replace("static mut", "const")
    return value_record(
        item_id,
        "CACHE",
        kind,
        f"{'static mut' if kind == 'Static' else 'const'} CACHE: Cell;",
        [10],
        [10],
        initializer={
            "annotated_source": source,
            "skeleton": skeleton,
            "needs_transformation": needs,
            "block": block,
            "statement_dispositions": dispositions,
        },
    )


def changed_type():
    record = type_record(10, "Cell", "Struct", BEFORE_TYPE, [])
    record["target_definition"] = AFTER_TYPE
    return record


def loaded(record):
    return load_skeletons(json.dumps([record, changed_type()]))


class InitializerTools(FakeTools):
    def __init__(self, *args, initializer_results=None, installed=AFTER, **kwargs):
        super().__init__(*args, **kwargs)
        self.initializer_results = list(initializer_results or [])
        self.installed = installed
        self.initializer_requests = []
        self.install_request = None

    def transform_initializer(self, frozen, request, output):
        value = json.loads(request.read_text())
        self.events.append(("transform_initializer", value))
        self.initializer_requests.append(value)
        result = (
            self.initializer_results.pop(0)
            if self.initializer_results
            else {"status": "valid", "source": value["transformation"]}
        )
        raw = result if isinstance(result, str) else json.dumps(result)
        output.write_text(raw)
        return raw, json.loads(raw)

    def install_initializers(self, frozen, request, output):
        value = json.loads(request.read_text())
        self.events.append(("install_initializers", value))
        self.install_request = value
        bundle = json.loads(frozen.read_text())
        bundle["target_source"] = AFTER_TYPE + "\n" + self.installed + "\n"
        output.write_text(json.dumps(bundle))

    def make_initial(self, analysis, output, *, frozen):
        self.events.append(("make_initial", analysis, output))
        output.write_text(json.loads(frozen.read_text())["target_source"])

    def add_functions(self, *args, **kwargs):
        frozen = kwargs["frozen"]
        self.events.append(("worker_frozen", frozen, frozen.read_bytes()))
        super().add_functions(*args, **kwargs)

    def assemble_project(self, frozen, accepted, output, *, stubbed_items=()):
        super().assemble_project(frozen, accepted, output, stubbed_items=stubbed_items)
        if not accepted:
            output.write_text(json.loads(frozen.read_text())["target_source"])


def run(value, tools, client, runner=None):
    return run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: client,
        validation_runner=runner,
    )


@pytest.mark.parametrize("kind", ["Static", "Const"])
@pytest.mark.parametrize("block", [False, True])
def test_global_initializer_records_load_without_losing_source_or_structure(
    kind, block
):
    raw = initializer_record(kind=kind, block=block)
    record = loaded(raw)[0]
    initializer = record.initializer
    assert initializer is not None
    assert initializer.annotated_source == raw["initializer"]["annotated_source"]
    assert initializer.skeleton == raw["initializer"]["skeleton"]
    assert initializer.block is block
    assert initializer.needs_transformation
    assert tuple(node.label for node in initializer.statement_dispositions) == (
        (0, 1) if block else ()
    )
    assert record.declaration == raw["declaration"]


def test_global_initializer_field_is_required_and_null_for_unaffected_globals():
    raw = value_record(0, "LIMIT", "Const", "const LIMIT: usize;", [], [])
    assert load_skeletons(json.dumps([raw]))[0].initializer is None
    del raw["initializer"]
    with pytest.raises(SkeletonError):
        load_skeletons(json.dumps([raw]))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(initializer=[]),
        lambda item: item["initializer"].update(unexpected=True),
        lambda item: item["initializer"].pop("block"),
        lambda item: item["initializer"].update(block=1),
        lambda item: item["initializer"].update(needs_transformation=1),
        lambda item: item["initializer"].update(annotated_source=""),
        lambda item: item["initializer"].update(skeleton=""),
        lambda item: item["initializer"].update(needs_transformation=False),
        lambda item: item["initializer"].update(block=False),
    ],
)
def test_inconsistent_global_initializer_metadata_is_rejected(mutate):
    record = initializer_record(block=True)
    mutate(record)
    with pytest.raises(SkeletonError):
        loaded(record)


def test_initializer_context_contains_both_changed_definitions_through_aliases():
    raw = initializer_record()
    raw["dependencies"] = raw["signature_dependencies"] = [11]
    alias = type_record(11, "Alias", "TyAlias", "type Alias = Cell;", [10])
    alias["target_definition"] = "type Alias<'a> = Cell<'a>;"
    records = load_skeletons(json.dumps([raw, changed_type(), alias]))
    context, _ = dependency_context((0,), {record.id: record for record in records})
    assert BEFORE_TYPE in context
    assert AFTER_TYPE in context
    assert "type Alias = Cell;" in context
    assert "type Alias<'a> = Cell<'a>;" in context


@pytest.mark.parametrize("block", [False, True])
def test_initializer_prompt_includes_unchanged_declaration_and_changed_types(
    tmp_path, block
):
    raw = initializer_record(block=block)
    source = raw["initializer"]["annotated_source"]
    transformed = source.replace("core::ptr::null()", "None")
    client = FakeClient([f"```rust\n{transformed}\n```"])
    tools = InitializerTools(skeletons=[raw, changed_type()])
    value = stage_input(tmp_path)
    output = run(value, tools, client)
    assert output.status == "success", output.error
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.metadata.prompt_id == "global_initializer"
    assert request.metadata.prompt_version == 1
    prompt = request.messages[0].content
    assert source in prompt
    assert raw["initializer"]["skeleton"] in prompt
    assert BEFORE_TYPE in prompt and AFTER_TYPE in prompt
    assert "constant" in prompt.lower()
    assert "unsafe" in prompt.lower()
    assert "static mut CACHE: Cell" in prompt
    assert ("#[proctor(0)]" in source) is block
    assert len(read_usage(value.framework.workdir / "usage.jsonl")) == 1
    assert (value.inputs.rust_project / "lib.rs").read_text() == "old\n"


def test_unaffected_globals_need_no_initializer_tools_or_llm(tmp_path):
    globals_ = [
        value_record(0, "LIMIT", "Const", "const LIMIT: usize;", [], []),
        value_record(1, "RAW", "Static", "static mut RAW: *mut i32;", [], []),
    ]
    tools = InitializerTools(skeletons=globals_)
    client = FakeClient([])
    output = run(stage_input(tmp_path), tools, client)
    assert output.status == "success", output.error
    assert not client.requests
    assert not tools.initializer_requests
    assert tools.install_request is None


def test_fully_preserved_initializer_uses_no_llm(tmp_path):
    raw = initializer_record(block=True, needs=False)
    tools = InitializerTools(skeletons=[raw, changed_type()])
    client = FakeClient([])
    output = run(stage_input(tmp_path), tools, client)
    assert output.status == "success", output.error
    assert not client.requests
    assert tools.install_request is not None
    assert tools.install_request["items"][0]["transformations"]


def test_all_initializers_are_installed_before_any_build_or_function_worker(tmp_path):
    second = initializer_record(item_id=2)
    second["path"] = "OTHER"
    second["declaration"] = "static mut OTHER: Cell;"
    for key in ("annotated_source", "skeleton"):
        second["initializer"][key] = second["initializer"][key].replace(
            "CACHE", "OTHER"
        )
    globals_source = AFTER + "\n" + AFTER.replace("CACHE", "OTHER")
    candidate = AFTER_TYPE + "\n" + globals_source + "\nunsafe fn target() {}"
    tools = InitializerTools(
        skeletons=[
            initializer_record(),
            fn_record(1, "target", "target", [0, 2, 10]),
            second,
            changed_type(),
        ],
        installed=globals_source,
        validators=[VALID],
        candidates=[candidate],
    )
    client = FakeClient(
        [
            f"```rust\n{AFTER}\n```",
            f"```rust\n{AFTER.replace('CACHE', 'OTHER')}\n```",
            response(),
        ]
    )
    value = stage_input(tmp_path)
    output = run(value, tools, client)
    assert output.status == "success", output.error
    operations = [event[0] for event in tools.events]
    assert operations.count("transform_initializer") == 2
    assert operations.count("install_initializers") == 1
    assert operations.index("install_initializers") < operations.index("make_initial")
    assert operations.index("install_initializers") < operations.index("cargo_build")
    assert operations.index("install_initializers") < operations.index("add_functions")
    first_build = next(event for event in tools.events if event[0] == "cargo_build")
    assert globals_source in first_build[2]
    worker = next(event for event in tools.events if event[0] == "add_functions")
    assert globals_source in worker[3]
    frozen = next(event for event in tools.events if event[0] == "worker_frozen")
    assert globals_source in json.loads(frozen[2])["target_source"]
    assert [request.metadata.prompt_id for request in client.requests] == [
        "global_initializer",
        "global_initializer",
        "local_transformation",
    ]
    assert (value.outputs.rust_project / "lib.rs").read_text() == candidate


def test_failed_initializer_history_survives_a_malformed_last_repair(tmp_path):
    first = "static mut CACHE: Cell = missing_value();"
    malformed = "static mut CACHE: Cell = missing_value("
    tools = InitializerTools(
        skeletons=[initializer_record(), changed_type()],
        installed=first,
        initializer_results=[
            {"status": "invalid", "diagnostics": "missing label"},
            {"status": "invalid", "diagnostics": "parse error"},
        ],
    )
    value = stage_input(tmp_path, config={"max_repairs": 1})
    client = FakeClient([f"```rust\n{first}\n```", patch_response(first, malformed)])
    output = run(value, tools, client)
    assert output.status == "success", output.error
    assert tools.install_request == {
        "items": [{"item_id": 0, "transformations": [first, malformed]}]
    }
    assert first in (value.outputs.rust_project / "lib.rs").read_text()
    assert "parse error" in client.requests[-1].messages[0].content or (
        "missing label" in client.requests[-1].messages[0].content
    )


def test_no_parseable_response_leaves_original_initializer_for_final_repair(tmp_path):
    tools = InitializerTools(
        skeletons=[initializer_record(), changed_type()], installed=BEFORE
    )
    client = FakeClient(["no fenced code"])
    value = stage_input(tmp_path, config={"max_repairs": 0})
    output = run(value, tools, client)
    assert output.status == "success", output.error
    assert tools.install_request == {"items": [{"item_id": 0, "transformations": []}]}
    published = (value.outputs.rust_project / "lib.rs").read_text()
    assert BEFORE in published
    assert "todo!()" not in published


def test_uncompilable_initializer_reaches_final_repair_after_functions(tmp_path):
    failed_global = "static mut CACHE: Cell = missing_value();"
    retained = AFTER_TYPE + "\n" + failed_global + "\nunsafe fn target() {}"

    class UncompilableInitializerTools(InitializerTools):
        def cargo_build(self, current, *, library_only=False):
            super().cargo_build(current, library_only=library_only)
            if "missing_value" in (current / "lib.rs").read_text():
                return CommandResult(101, stderr="initializer does not type-check")
            return CommandResult(0)

    tools = UncompilableInitializerTools(
        skeletons=[
            initializer_record(),
            fn_record(1, "target", "target", [0, 10]),
            changed_type(),
        ],
        installed=failed_global,
        candidates=[retained],
        validators=[VALID],
        retained=[{"source": retained, "stubbed_items": []}],
    )
    value = stage_input(
        tmp_path, config={"validation_repair_model": "fixture/compilation-agent"}
    )
    client = FakeClient([f"```rust\n{failed_global}\n```", response()])
    repairs = []

    def repair(command, **kwargs):
        repairs.append(command)
        assert len(client.requests) == 2
        assert not any(event[0] == "postprocess" for event in tools.events)
        project = kwargs["cwd"]
        source = (project / "lib.rs").read_text()
        assert failed_global in source and "unsafe fn target() {}" in source
        (project / "lib.rs").write_text(source.replace(failed_global, AFTER))
        return CommandResult(0)

    output = run(value, tools, client, repair)
    assert output.status == "success", output.error
    assert len(repairs) == 1
    assert output.metrics["compilation_repair_calls"] == 1
    published = (value.outputs.rust_project / "lib.rs").read_text()
    assert AFTER in published and "missing_value" not in published


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"status": "unknown"},
        {"status": "valid"},
        {"status": "valid", "source": ""},
        {"status": "valid", "source": AFTER, "extra": True},
        {"status": "invalid", "diagnostics": ""},
    ],
)
def test_malformed_initializer_tool_responses_abort_as_protocol_failures(
    tmp_path, result
):
    tools = InitializerTools(
        skeletons=[initializer_record(), changed_type()], initializer_results=[result]
    )
    client = FakeClient([f"```rust\n{AFTER}\n```"])
    output = run(stage_input(tmp_path), tools, client)
    assert output.status == "failure"
    assert "initializer" in output.error.lower()
    assert tools.install_request is None


@pytest.mark.parametrize("operation", ["transform", "install"])
def test_initializer_tools_use_parser_only_commands_and_current_frozen_bundle(
    tmp_path, operation
):
    frozen = tmp_path / "frozen.json"
    request = tmp_path / "request.json"
    output = tmp_path / "result.json"
    frozen.write_text("{}")
    request.write_text("{}")
    result = {"status": "valid", "source": AFTER}
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        output.write_text(json.dumps(result))
        return CommandResult(0)

    tools = CratTools(tmp_path / "tools.log", run_command=runner)
    tools.crat_tool = Path("/tools/crat-tool")
    if operation == "transform":
        raw, parsed = tools.transform_initializer(frozen, request, output)
        assert json.loads(raw) == parsed == result
    else:
        tools.install_initializers(frozen, request, output)
    assert commands == [
        [
            "/tools/crat-tool",
            "transform-initializer"
            if operation == "transform"
            else "install-initializers",
            "--frozen",
            str(frozen),
            "--request",
            str(request),
            "--output",
            str(output),
        ]
    ]


@pytest.mark.parametrize("operation", ["transform_initializer", "install_initializers"])
@pytest.mark.parametrize("input_name", ["frozen", "request"])
def test_initializer_outputs_cannot_overwrite_input_files(
    tmp_path, operation, input_name
):
    frozen = tmp_path / "frozen.json"
    request = tmp_path / "request.json"
    frozen.write_text("preserve frozen")
    request.write_text("preserve request")
    tools = CratTools(
        tmp_path / "tools.log",
        run_command=lambda *args, **kwargs: pytest.fail(
            "input overwrite reached a tool"
        ),
    )
    tools.crat_tool = Path("/tools/crat-tool")
    target = frozen if input_name == "frozen" else request
    with pytest.raises(StageFailure, match="overlaps"):
        getattr(tools, operation)(frozen, request, target)
    assert frozen.read_text() == "preserve frozen"
    assert request.read_text() == "preserve request"


@pytest.mark.parametrize("operation", ["transform_initializer", "install_initializers"])
@pytest.mark.parametrize("output_kind", ["missing", "directory"])
def test_initializer_tools_reject_missing_or_nonregular_outputs(
    tmp_path, operation, output_kind
):
    frozen = tmp_path / "frozen.json"
    request = tmp_path / "request.json"
    output = tmp_path / "result.json"
    frozen.write_text("{}")
    request.write_text("{}")
    output.write_text("stale result")

    def runner(command, **kwargs):
        if output_kind == "directory":
            output.mkdir()
        return CommandResult(0)

    tools = CratTools(tmp_path / "tools.log", run_command=runner)
    tools.crat_tool = Path("/tools/crat-tool")
    with pytest.raises(StageFailure):
        getattr(tools, operation)(frozen, request, output)


@pytest.mark.parametrize(
    "name", ["global-initializer-install.json", "initialized-frozen-project.json"]
)
def test_initializer_scratch_symlinks_cannot_modify_input_source(tmp_path, name):
    value = stage_input(tmp_path)
    original = value.inputs.rust_project / "lib.rs"
    (value.framework.workdir / name).symlink_to(original)
    tools = InitializerTools(skeletons=[initializer_record(), changed_type()])
    client = FakeClient([])
    output = run(value, tools, client)
    assert output.status == "failure"
    assert "symlink" in output.error.lower()
    assert original.read_text() == "old\n"
    assert not tools.events
    assert not client.requests
