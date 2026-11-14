from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from model import (
    SkeletonError,
    group_functions,
    strongly_connected_components,
    function_graph,
)
from stage import run_stage
from tooling import CommandResult
from tests.test_local_transformation import (
    FakeClient,
    STAGE_DIR,
    VALID,
    apply_rules,
    fn_record,
    loaded,
    patch_response,
    stage_input,
)
from tests.test_local_unstructured_repair import UnstructuredTools


def weight(record):
    return len(record.annotated_source) + max(
        len(record.baseline.skeleton), len(record.applied.skeleton)
    )


def test_group_boundaries_preserve_scc_order_with_nonconsecutive_ids():
    records = loaded(
        [
            fn_record(0, "a", "a", [3]),
            fn_record(1, "b", "b", []),
            fn_record(2, "c", "c", []),
            fn_record(3, "d", "d", [0]),
        ]
    )
    by_id = {record.id: record for record in records}
    components = strongly_connected_components(function_graph(records))
    assert components == ((0, 3), (1,), (2,))
    ordered = tuple(item for members in components for item in members)
    limit = weight(by_id[0]) + weight(by_id[3])
    assert group_functions(ordered, by_id, limit) == ((0, 3), (1, 2))
    assert group_functions(ordered, by_id, limit - 1) == ((0,), (3,), (1,), (2,))


def test_group_size_counts_characters_and_the_larger_skeleton_view():
    records = loaded([fn_record(0, "a", "a", []), fn_record(1, "b", "b", [])])
    first = replace(
        records[0],
        annotated_source="é" * 7,
        baseline=replace(records[0].baseline, skeleton="x" * 13),
        applied=replace(records[0].applied, skeleton="x" * 3),
    )
    second = replace(
        records[1],
        annotated_source="y" * 5,
        baseline=replace(records[1].baseline, skeleton="x" * 2),
        applied=replace(records[1].applied, skeleton="x" * 17),
    )
    by_id = {0: first, 1: second}
    assert group_functions((0, 1), by_id, 42) == ((0, 1),)
    assert group_functions((0, 1), by_id, 41) == ((0,), (1,))
    assert group_functions((0, 1), by_id, 1) == ((0,), (1,))
    assert group_functions((), by_id, 1) == ()


def test_duplicate_final_names_force_a_boundary_even_inside_one_scc():
    records = loaded(
        [
            fn_record(0, "left::same", "same", [1]),
            fn_record(1, "right::same", "same", [0]),
            fn_record(2, "other", "other", []),
        ]
    )
    assert strongly_connected_components(function_graph(records)) == ((0, 1), (2,))
    assert group_functions((0, 1, 2), {r.id: r for r in records}, 15_000) == (
        (0,),
        (1, 2),
    )


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "15000", None])
def test_group_limit_rejects_nonpositive_or_noninteger_values(tmp_path, limit):
    tools = GroupTools(skeletons=[])
    value = stage_input(tmp_path, config={"max_group_chars": limit})
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "config.max_group_chars must be a positive integer" in output.error
    assert not tools.events


class GroupTools(UnstructuredTools):
    def __init__(
        self,
        *,
        probe_failures=(),
        structured_failure=False,
        unstructured_failure=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.probe_failures = set(probe_failures)
        self.structured_failure = structured_failure
        self.unstructured_failure = unstructured_failure
        self.forks = []

    def fork(self, log_path, target_dir):
        self.forks.append(target_dir)
        return super().fork(log_path, target_dir)

    def add_functions(self, analysis, current, request, *outputs, **kwargs):
        value = json.loads(request.read_text())
        members = tuple(item["id"] for item in value["items"])
        probe = all(not item["view"]["needs_transformation"] for item in value["items"])
        self.candidates = [f"{'probe' if probe else 'structured'}:{members}\n"]
        assert (current / "lib.rs").read_text() == self.normalized
        super().add_functions(analysis, current, request, *outputs, **kwargs)

    def validate(self, request, response):
        self.validators = [VALID]
        value = json.loads(request.read_text())
        assert set(re.findall(r"unsafe fn (\w+)\(", value["transformation"])) == {
            item["name"] for item in value["expected_functions"]
        }
        return super().validate(request, response)

    def cargo_build(self, current, *, library_only=False):
        result = super().cargo_build(current, library_only=library_only)
        text = (current / "lib.rs").read_text()
        if text.startswith("probe:"):
            members = set(map(int, re.findall(r"\d+", text)))
            if members & self.probe_failures:
                return CommandResult(
                    101, "probe stdout", f"probe error {sorted(members)}"
                )
        if text.startswith("structured:") and self.structured_failure:
            return CommandResult(101, "structured stdout", "structured error")
        if text.startswith("unstructured:") and self.unstructured_failure:
            return CommandResult(101, "unstructured stdout", "unstructured error")
        if text.startswith("unstructured seed:"):
            return CommandResult(101, "seed stdout", "seed error")
        return result

    def prepare_unstructured(self, frozen, request, output):
        value = json.loads(request.read_text())
        self.seed = (
            re.sub(r"#\[proctor\(\d+\)\]\s*", "", value["transformations"][-1])
            if value["transformations"]
            else None
        )
        super().prepare_unstructured(frozen, request, output)

    def replace_unstructured(self, analysis, current, frozen, request, output):
        value = json.loads(request.read_text())
        prefix = (
            "unstructured"
            if "// signature-only repair" in value["transformation"]
            else "unstructured seed"
        )
        self.unstructured_results = [f"{prefix}:{tuple(value['members'])}\n"]
        assert (current / "lib.rs").read_text() == self.normalized
        super().replace_unstructured(analysis, current, frozen, request, output)

    def assemble_project(self, frozen, accepted, output):
        self.events.append(("assemble_project", frozen, accepted, output))
        assert len({item for members, _ in accepted for item in members}) == sum(
            len(members) for members, _ in accepted
        )
        output.write_text("assembled functions\n")


class GroupClient(FakeClient):
    def __init__(self, records):
        super().__init__([])
        self.names = {record["id"]: record["name"] for record in records}
        self.signatures = {
            record["id"]: record["target_signature"] for record in records
        }

    def complete(self, request):
        text = request.messages[0].content
        unstructured = request.metadata.prompt_id == "unstructured_repair"
        prefix = "// signature-only repair\n" if unstructured else ""
        label = "" if unstructured else "#[proctor(0)] "
        code = prefix + "\n\n".join(
            f"{self.signatures[int(item)]} {{ {label}{len(self.requests)} }}"
            for item in request.metadata.item.split(",")
        )
        marker = "## Repair Base: `transformation.rs`"
        if marker in text:
            base = (
                text.split(marker, 1)[1].split("```rust\n", 1)[1].split("\n```", 1)[0]
            )
            self.responses = [patch_response(base, code)]
        else:
            self.responses = [f"```rust\n{code}\n```"]
        return super().complete(request)


def run_groups(tmp_path, records, *, config=None, **tool_options):
    tools = GroupTools(skeletons=records, **tool_options)
    client = GroupClient(records)
    value = stage_input(tmp_path, config=config)
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: client,
    )
    return value, output, tools, client


def generic_record(item_id, name, dependencies):
    record = fn_record(item_id, name, name, dependencies, needs_transformation=False)
    signature = f"unsafe fn {name}<T>(value: T)"
    record.update(
        unstructured=True,
        annotated_source=f"{signature} {{ let inferred = value; drop(inferred); }}",
        source_signature=signature,
        target_signature=signature,
        baseline={
            "skeleton": f"{signature} {{ todo!() }}",
            "needs_transformation": False,
            "statement_dispositions": [],
            "statement_pair_metadata": [],
        },
    )
    record["applied"] = record["baseline"]
    return record


def test_generic_functions_form_separate_direct_groups_across_recursive_components(
    tmp_path,
):
    records = [
        generic_record(0, "generic_a", [1]),
        fn_record(1, "ordinary_a", "ordinary_a", [0]),
        generic_record(2, "generic_b", [3]),
        fn_record(3, "ordinary_b", "ordinary_b", [2]),
    ]
    value, output, tools, client = run_groups(
        tmp_path, records, config={"workers": 1, "max_unstructured_repairs": 0}
    )
    assert output.status == "success", output.error
    assert output.metrics["scc_count"] == 2
    assert [request.metadata.item for request in client.requests] == ["0,2", "1,3"]
    assert [request.metadata.prompt_id for request in client.requests] == [
        "unstructured_repair",
        "local_transformation",
    ]
    prompt = client.requests[0].messages[0].content
    assert "let inferred = value" in prompt
    assert "unsafe fn ordinary_a()" in prompt
    assert "Target skeleton:" not in prompt and "#[proctor(" not in prompt
    additions = [event[2] for event in tools.events if event[0] == "add_functions"]
    assert [[item["id"] for item in addition["items"]] for addition in additions] == [
        [1, 3]
    ]
    replacements = [
        event[1] for event in tools.events if event[0] == "replace_unstructured"
    ]
    assert [request["members"] for request in replacements] == [[0, 2]]
    assert output.metrics["llm_generation_calls"] == 2
    assert (
        output.metrics["repair_calls"]
        == output.metrics["unstructured_repair_calls"]
        == 0
    )
    assert output.metrics["unstructured_group_count"] == 1
    assert not list(
        (value.framework.workdir / "groups/000000").glob("accepted-*observation*")
    )
    report = (value.outputs.artifacts_dir / "statement-pairs.md").read_text()
    assert "generic_a" not in report and "generic_b" not in report


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_generic_generation_has_an_initial_attempt_then_unstructured_repair_budget(
    tmp_path, limit
):
    _, output, tools, client = run_groups(
        tmp_path,
        [generic_record(0, "generic", [])],
        config={"max_repairs": 0, "max_unstructured_repairs": limit},
        unstructured_failure=True,
    )
    assert output.status == "success", output.error
    assert len(client.requests) == limit + 1
    assert output.metrics["llm_generation_calls"] == limit + 1
    assert (
        output.metrics["repair_calls"]
        == output.metrics["unstructured_repair_calls"]
        == limit
    )
    assert output.metrics["retained_functions"] == 1
    assert not [
        event
        for event in tools.events
        if event[0] in {"validate", "add_functions", "extract_observations"}
    ]


def test_generic_group_size_uses_original_body_and_frozen_signature():
    records = loaded([generic_record(0, "first", []), generic_record(1, "second", [])])
    by_id = {record.id: record for record in records}
    limit = sum(
        len(record.annotated_source) + len(record.target_signature)
        for record in records
    )
    assert group_functions((0, 1), by_id, limit) == ((0, 1),)
    assert group_functions((0, 1), by_id, limit - 1) == ((0,), (1,))


@pytest.mark.parametrize("change", ["flag", "signature", "metadata"])
def test_generic_records_reject_inconsistent_unstructured_metadata(change):
    record = generic_record(0, "generic", [])
    if change == "flag":
        record["unstructured"] = "true"
    elif change == "signature":
        record["target_signature"] += " where T: Copy"
    else:
        record["baseline"]["needs_transformation"] = True
    with pytest.raises(SkeletonError):
        loaded([record])


def test_initial_groups_split_a_recursive_scc_and_merge_adjacent_sccs(tmp_path):
    records = [
        fn_record(0, "a", "a", [1]),
        fn_record(1, "b", "b", [2]),
        fn_record(2, "c", "c", [0]),
        fn_record(3, "d", "d", []),
    ]
    by_id = {record.id: record for record in loaded(records)}
    limit = weight(by_id[0]) + weight(by_id[1])
    _, output, tools, client = run_groups(
        tmp_path, records, config={"max_group_chars": limit}
    )
    assert output.status == "success", output.error
    assert [request.metadata.item for request in client.requests] == ["0,1", "2,3"]
    assert output.metrics["scc_count"] == 2
    assert output.metrics["llm_generation_calls"] == 2
    additions = [event[2] for event in tools.events if event[0] == "add_functions"]
    assert [[item["id"] for item in value["items"]] for value in additions] == [
        [0, 1],
        [2, 3],
    ]
    assert all(value["accepted_correspondence"] == [] for value in additions)


def test_probe_failures_group_by_repair_route_and_keep_successes_once(tmp_path):
    records = [fn_record(0, "initial", "initial", [])]
    for item in range(1, 7):
        record = fn_record(
            item, f"f{item}", f"f{item}", [], needs_transformation=item in {2, 3, 6}
        )
        if item in {2, 3, 6}:
            record = apply_rules(record, rule_labels=[0], transform_labels=[])
        records.append(record)
    value, output, tools, client = run_groups(
        tmp_path, records, probe_failures={2, 3, 4, 5}
    )
    assert output.status == "success", output.error
    assert [request.metadata.item for request in client.requests] == ["0", "2,3", "4,5"]
    assert [request.metadata.prompt_id for request in client.requests] == [
        "local_transformation",
        "local_transformation",
        "unstructured_repair",
    ]
    for request in client.requests[1:]:
        assert "probe error" in request.messages[0].content
        for item in request.metadata.item.split(","):
            assert f"Function {item}:f{item}" in request.messages[0].content
    repair = [event[2] for event in tools.events if event[0] == "add_functions"][-1]
    assert [item["view"] for item in repair["items"]] == [
        records[2]["baseline"],
        records[3]["baseline"],
    ]
    accepted = next(
        event[2] for event in tools.events if event[0] == "assemble_project"
    )
    assert [members for members, _ in accepted] == [(0,), (1,), (6,), (2, 3), (4, 5)]
    assert all(
        not (value.framework.workdir / f"groups/{item:06d}/accepted.rs").exists()
        for item in (2, 3, 4, 5)
    )
    assert len(tools.forks) == 1
    assert output.metrics["compilation_failures"] == 4
    assert output.metrics["repair_calls"] == 2
    assert output.metrics["unstructured_repair_calls"] == 1
    assert output.metrics["unstructured_group_count"] == 1
    assert output.metrics["cargo_builds"] == 12
    assert [
        event[1]["members"]
        for event in tools.events
        if event[0] == "replace_unstructured"
    ] == [[4, 5]]
    report = (value.outputs.artifacts_dir / "statement-pairs.md").read_text()
    assert (
        "f2" in report and "f3" in report and "f4" not in report and "f5" not in report
    )


@pytest.mark.parametrize("structured_limit", [0, 2, 9])
@pytest.mark.parametrize("unstructured_limit", [0, 1, 3])
def test_rule_free_failures_use_only_the_unstructured_allowance(
    tmp_path, structured_limit, unstructured_limit
):
    records = [fn_record(0, "target", "target", [], needs_transformation=False)]
    _, output, tools, client = run_groups(
        tmp_path,
        records,
        probe_failures={0},
        unstructured_failure=True,
        config={
            "max_repairs": structured_limit,
            "max_unstructured_repairs": unstructured_limit,
        },
    )
    assert output.status == "success", output.error
    assert output.metrics["retained_functions"] == 1
    assert output.metrics["compilation_repair_calls"] == 0
    assert len([event for event in tools.events if event[0] == "retain_functions"]) == 1
    assert len(client.requests) == unstructured_limit
    assert output.metrics["repair_calls"] == unstructured_limit
    assert output.metrics["unstructured_repair_calls"] == unstructured_limit
    assert output.metrics["compilation_failures"] == 1 + unstructured_limit
    assert output.metrics["cargo_builds"] == 4 + unstructured_limit
    assert not [event for event in tools.events if event[0] == "validate"]
    assert all(
        request.metadata.prompt_id == "unstructured_repair"
        for request in client.requests
    )


@pytest.mark.parametrize("structured_limit", [0, 1, 2])
def test_failed_rule_probes_receive_exactly_the_structured_repair_budget(
    tmp_path, structured_limit
):
    record = apply_rules(
        fn_record(0, "target", "target", []), rule_labels=[0], transform_labels=[]
    )
    _, output, tools, client = run_groups(
        tmp_path,
        [record],
        probe_failures={0},
        structured_failure=True,
        config={"max_repairs": structured_limit, "max_unstructured_repairs": 1},
    )
    assert output.status == "success", output.error
    assert [request.metadata.prompt_id for request in client.requests] == [
        "local_transformation"
    ] * structured_limit + ["unstructured_repair"]
    assert output.metrics["repair_calls"] == structured_limit + 1
    assert output.metrics["unstructured_repair_calls"] == 1
    assert (
        len([event for event in tools.events if event[0] == "validate"])
        == structured_limit
    )
    assert "probe error" in client.requests[0].messages[0].content


@pytest.mark.parametrize("rule_generated", [False, True])
@pytest.mark.parametrize("limit_kind", ["exact", "oversized", "duplicate_names"])
def test_failed_functions_use_the_same_group_boundaries(
    tmp_path, rule_generated, limit_kind
):
    records = []
    for item, name in enumerate(("a", "b", "c", "d")):
        path = name
        if limit_kind == "duplicate_names" and item in {0, 2}:
            name = "same"
            path = f"module{item}::same"
        dependencies = [2] if item == 0 else [0] if item == 2 else []
        record = fn_record(
            item, path, name, dependencies, needs_transformation=rule_generated
        )
        if rule_generated:
            record = apply_rules(record, rule_labels=[0], transform_labels=[])
        records.append(record)
    by_id = {record.id: record for record in loaded(records)}
    limit = weight(by_id[0]) + weight(by_id[2])
    expected = ["0,2", "1,3"]
    if limit_kind == "oversized":
        limit = 1
        expected = ["0", "2", "1", "3"]
    elif limit_kind == "duplicate_names":
        limit = 15_000
        expected = ["0", "1,2,3"]
    _, output, tools, client = run_groups(
        tmp_path,
        records,
        probe_failures={0, 1, 2, 3},
        config={"max_group_chars": limit},
    )
    assert output.status == "success", output.error
    assert [request.metadata.item for request in client.requests] == expected
    assert output.metrics["scc_count"] == 3
    assert len([event for event in tools.events if event[0] == "add_functions"]) == (
        4 + len(expected) if rule_generated else 4
    )


def test_deferred_repairs_reuse_worker_caches_clients_and_rate_limiter(
    tmp_path, monkeypatch
):
    class CacheTools(GroupTools):
        def fork(self, log_path, target_dir):
            worker = super().fork(log_path, target_dir)
            target_dir.mkdir()
            (target_dir / "cache-marker").write_text("retained")
            return worker

        def replace_unstructured(self, analysis, current, frozen, request, output):
            assert (self.target_dir / "cache-marker").read_text() == "retained"
            super().replace_unstructured(analysis, current, frozen, request, output)

    records = [
        fn_record(item, f"f{item}", f"f{item}", [], needs_transformation=False)
        for item in range(4)
    ]
    limiters = []

    class Client(GroupClient):
        def __init__(self, settings, *, tracker, limiter):
            super().__init__(records)
            limiters.append(limiter)

    monkeypatch.setattr("stage.LlmClient", Client)
    value = stage_input(tmp_path, config={"workers": 2})
    tools = CacheTools(skeletons=records, probe_failures={0, 1, 2, 3})
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "success", output.error
    assert len(limiters) == len(tools.forks) == 2
    assert limiters[0] is limiters[1]
    assert output.usage.calls == output.metrics["unstructured_repair_calls"] == 1
    assert output.metrics["compilation_failures"] == 4
    assert all(
        (target / "cache-marker").read_text() == "retained" for target in tools.forks
    )
