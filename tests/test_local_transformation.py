from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from proctor.contracts import (
    FrameworkSettings,
    InputArtifacts,
    OutputDestinations,
    StageInput,
)
from proctor.llm.client import LlmClient
from proctor.llm.types import (
    AuthError,
    ContextLimitExceeded,
    ProviderError,
    Response,
    Usage,
)

from model import (
    ContextOverflow,
    ObservationError,
    PointerVariableMetadata,
    PointerVariableOrigin,
    SkeletonError,
    StatementPairMetadata,
    dependency_context,
    function_graph,
    leaf_schedule,
    load_observations,
    load_rules,
    load_replacement_metadata,
    load_skeletons,
    render_dependency_entry,
    render_transformation_targets,
    strongly_connected_components,
    RuleDocument,
    RuleError,
    rules_to_json,
)
from extract_rules import main as extract_rules_main
import extract_rules as extract_rules_module
from rule_synthesis import (
    PairRejection,
    canonicalize_rule,
    synthesize_pair,
    synthesize_rules,
)
from protocol import (
    NO_FENCE_DIAGNOSTIC,
    PromptRenderInput,
    extract_code_block,
    extract_observations_command,
    llm_request,
    make_skeleton_command,
    normalize_safety_command,
    render_prompt,
    replace_command,
    replacement_request,
    validate_command,
    validation_request,
)
import stage as stage_module
from stage import (
    AcceptedStatementPair,
    _code_value,
    _load_replacement_statement_pairs,
    _load_and_validate_replacement_metadata,
    _publish_final_outputs,
    _render_statement_pairs,
    _rust_fence,
    _type_code_value,
    run_stage,
)
from tooling import (
    CommandResult,
    CratTools,
    StageFailure,
    install_candidate_transaction,
)
from proctor.usage.tracker import read_usage

STAGE_DIR = Path(__file__).parents[1] / "stages" / "local-transformation"
PROMPT_GOLDEN = (
    Path(__file__).parent / "fixtures" / "local_transformation_prompt_golden.md"
)


def fn_record(
    item_id: int,
    path: str,
    name: str,
    dependencies: list[int],
    signature_dependencies: list[int] | None = None,
    *,
    needs_transformation: bool = True,
    transformation_labels: list[int] | None = None,
    statement_pair_metadata: list[dict[str, object]] | None = None,
    foreign_function_names: list[str] | None = None,
) -> dict[str, object]:
    labels = (
        ([0] if needs_transformation else [])
        if transformation_labels is None
        else transformation_labels
    )
    body = "todo!()" if needs_transformation else "()"
    return {
        "id": item_id,
        "path": path,
        "kind": "Fn",
        "name": name,
        "annotated_source": (f"unsafe fn {name}() {{\n    #[proctor(0)]\n    ()\n}}"),
        "annotated_skeleton": (
            f"unsafe fn {name}() {{\n    #[proctor(0)]\n    {body}\n}}"
        ),
        "source_signature": f"unsafe fn {name}()",
        "target_signature": f"unsafe fn {name}()",
        "needs_transformation": needs_transformation,
        "statements_requiring_transformation": labels,
        "statement_pair_metadata": (
            [
                {
                    "label": label,
                    "before_statement": f"#[proctor({label})]\n()",
                    "pointer_variables_complete": True,
                    "pointer_variables": [],
                }
                for label in labels
            ]
            if statement_pair_metadata is None
            else statement_pair_metadata
        ),
        "foreign_function_names": (
            [] if foreign_function_names is None else foreign_function_names
        ),
        "signature_dependencies": (
            [] if signature_dependencies is None else signature_dependencies
        ),
        "dependencies": dependencies,
    }


def type_record(
    item_id: int, path: str, kind: str, definition: str, dependencies: list[int]
) -> dict[str, object]:
    return {
        "id": item_id,
        "path": path,
        "kind": kind,
        "definition": definition,
        "dependencies": dependencies,
    }


def value_record(
    item_id: int,
    path: str,
    kind: str,
    declaration: str,
    dependencies: list[int],
    signature_dependencies: list[int],
) -> dict[str, object]:
    return {
        "id": item_id,
        "path": path,
        "kind": kind,
        "declaration": declaration,
        "signature_dependencies": signature_dependencies,
        "dependencies": dependencies,
    }


GRAPH_RECORDS = [
    fn_record(0, "leaf", "leaf", []),
    fn_record(1, "left", "left", [0, 100]),
    fn_record(2, "right", "right", [0]),
    fn_record(3, "cycle::a", "a", [4]),
    fn_record(4, "cycle::b", "b", [1, 3]),
    fn_record(5, "self_rec", "self_rec", [2, 5]),
    fn_record(6, "root", "root", [3, 5]),
    fn_record(7, "isolated", "isolated", []),
    fn_record(8, "same_a::same", "same", [9]),
    fn_record(9, "same_b::same", "same", [8]),
    type_record(100, "Context", "Struct", "struct Context;", []),
]

CONTEXT_RECORDS = [
    {
        **fn_record(0, "target", "target", [1, 2, 20], [20]),
        "annotated_source": (
            "unsafe fn target(mut p: *const S) -> i32 {\n"
            "    #[proctor(0)]\n    callee(p.cast()) + GLOBAL\n}"
        ),
        "annotated_skeleton": (
            "unsafe fn target(mut p: &S) -> i32 {\n    #[proctor(0)]\n    todo!()\n}"
        ),
        "source_signature": "unsafe fn target(mut p: *const S) -> i32",
        "target_signature": "unsafe fn target(mut p: &S) -> i32",
    },
    {
        **fn_record(1, "callee", "callee", [21, 22], [21]),
        "source_signature": "unsafe fn callee(mut p: *const T) -> i32",
        "target_signature": "unsafe fn callee(mut p: &T) -> i32",
    },
    value_record(2, "GLOBAL", "Static", "static GLOBAL: i32;", [23, 24], [23]),
    fn_record(3, "peer", "peer", [0, 2], []),
    type_record(20, "S", "Struct", "struct S { value: *const i32 }", [25]),
    type_record(21, "T", "TyAlias", "type T = U;", [26]),
    fn_record(22, "body_only", "body_only", []),
    value_record(23, "N", "Const", "const N: usize;", [27, 28], [27]),
    type_record(24, "BodyOnly", "Struct", "struct BodyOnly;", []),
    type_record(25, "V", "Union", "union V { value: i32 }", [26, 28]),
    type_record(26, "U", "Struct", "struct U { e: E }", [29]),
    type_record(27, "W", "Enum", "enum W { A }", []),
    type_record(28, "X", "Struct", "struct X;", []),
    type_record(29, "E", "Enum", "enum E { A }", []),
]

SCALAR_SOURCE = """pub unsafe fn scalar(value: i32) -> i32 {
    value + 1
}"""

FOREIGN_FUNCTION_SOURCE = """#![feature(extern_types)]

unsafe extern "C" {
    fn strlen(text: *const core::ffi::c_char) -> usize;
    fn free(pointer: *mut core::ffi::c_void);
    fn transitive_foreign(value: i32) -> i32;
    fn unused_foreign(value: i32) -> i32;
    static FOREIGN_COUNTER: i32;
    type ForeignOpaque;
}

use strlen as c_strlen;

pub unsafe extern "C" fn local_abi(value: i32) -> i32 {
    transitive_foreign(value)
}

pub mod parser {
    pub unsafe fn scan(
        pointer: *mut core::ffi::c_void,
        text: *const core::ffi::c_char,
    ) -> usize {
        crate::free(pointer);
        let first = crate::c_strlen(text);
        let second = crate::strlen(text);
        let _ = crate::FOREIGN_COUNTER;
        let _: Option<*mut crate::ForeignOpaque> = None;
        let _ = crate::local_abi(first as i32);
        first + second + core::mem::size_of::<usize>()
    }

    pub unsafe fn release(pointer: *mut core::ffi::c_void) {
        crate::free(pointer);
        crate::free(pointer);
    }

    pub unsafe fn scalar(value: i32) -> i32 {
        crate::local_abi(value)
    }
}"""

ITEM_KIND_SOURCE = """pub struct Point {
    pub value: i32,
}

pub static LIMIT: i32 = 10;
pub const STEP: i32 = 1;

pub unsafe fn helper(point: Point) -> i32 {
    point.value
}

pub unsafe fn target(point: Point) -> i32 {
    helper(point) + LIMIT + STEP
}"""

DUPLICATE_NAME_SOURCE = """pub mod outer {
    pub mod left {
        pub unsafe fn parse(value: i32) -> i32 {
            value + 1
        }
    }

    pub mod right {
        pub unsafe fn parse(value: i32) -> i32 {
            value - 1
        }
    }
}

pub unsafe fn target(value: i32) -> i32 {
    outer::left::parse(value) + outer::right::parse(value)
}"""


def scalar_records():
    assert SCALAR_SOURCE.endswith("}")
    record = fn_record(0, "scalar", "scalar", [], needs_transformation=False)
    record["annotated_source"] = (
        "pub unsafe fn scalar(mut value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    (value + 1)\n"
        "}"
    )
    record["annotated_skeleton"] = record["annotated_source"]
    record["source_signature"] = "pub unsafe fn scalar(mut value: i32) -> i32"
    record["target_signature"] = record["source_signature"]
    return loaded([record])


def foreign_function_records():
    assert "use strlen as c_strlen;" in FOREIGN_FUNCTION_SOURCE
    local_abi = fn_record(
        0,
        "local_abi",
        "local_abi",
        [],
        foreign_function_names=["transitive_foreign"],
    )
    local_abi["annotated_source"] = (
        "pub unsafe fn local_abi(mut value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    transitive_foreign(value)\n"
        "}"
    )
    local_abi["annotated_skeleton"] = (
        "pub unsafe fn local_abi(mut value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    todo!()\n"
        "}"
    )
    local_abi["source_signature"] = "pub unsafe fn local_abi(mut value: i32) -> i32"
    local_abi["target_signature"] = local_abi["source_signature"]
    scan = fn_record(
        1,
        "parser::scan",
        "scan",
        [0],
        foreign_function_names=["free", "strlen"],
    )
    scan["annotated_source"] = (
        "pub unsafe fn scan(mut pointer: *mut core::ffi::c_void,\n"
        "    mut text: *const core::ffi::c_char) -> usize {\n"
        "    #[proctor(0)]\n"
        "    crate::free(pointer);\n"
        "    #[proctor(1)]\n"
        "    let mut first = crate::c_strlen(text);\n"
        "    #[proctor(2)]\n"
        "    let mut second = crate::strlen(text);\n"
        "    #[proctor(3)]\n"
        "    let _ = crate::FOREIGN_COUNTER;\n"
        "    #[proctor(4)]\n"
        "    let _: Option<*mut crate::ForeignOpaque> = None;\n"
        "    #[proctor(5)]\n"
        "    let _ = crate::local_abi(first as i32);\n"
        "    #[proctor(6)]\n"
        "    (first + second + core::mem::size_of::<usize>())\n"
        "}"
    )
    scan["annotated_skeleton"] = (
        "pub unsafe fn scan(mut pointer: *mut core::ffi::c_void, mut text: &[i8])\n"
        "    -> usize {\n"
        "    #[proctor(0)]\n"
        "    todo!();\n"
        "    #[proctor(1)]\n"
        "    let mut first: usize = todo!();\n"
        "    #[proctor(2)]\n"
        "    let mut second: usize = todo!();\n"
        "    #[proctor(3)]\n"
        "    let _ = crate::FOREIGN_COUNTER;\n"
        "    #[proctor(4)]\n"
        "    let _: Option<*mut crate::ForeignOpaque> = todo!();\n"
        "    #[proctor(5)]\n"
        "    let _ = crate::local_abi(first as i32);\n"
        "    #[proctor(6)]\n"
        "    (first + second + core::mem::size_of::<usize>())\n"
        "}"
    )
    scan["source_signature"] = (
        "pub unsafe fn scan(mut pointer: *mut core::ffi::c_void,\n"
        "mut text: *const core::ffi::c_char) -> usize"
    )
    scan["target_signature"] = (
        "pub unsafe fn scan(mut pointer: *mut core::ffi::c_void, mut text: &[i8])\n"
        "-> usize"
    )
    scan["statements_requiring_transformation"] = [0, 1, 2, 4]
    scan["statement_pair_metadata"] = [
        {
            "label": label,
            "before_statement": f"#[proctor({label})]\n()",
            "pointer_variables_complete": True,
            "pointer_variables": [],
        }
        for label in [0, 1, 2, 4]
    ]
    release = fn_record(
        2,
        "parser::release",
        "release",
        [],
        transformation_labels=[0, 1],
        foreign_function_names=["free"],
    )
    release["annotated_source"] = (
        "pub unsafe fn release(mut pointer: *mut core::ffi::c_void) {\n"
        "    #[proctor(0)]\n"
        "    crate::free(pointer);\n"
        "    #[proctor(1)]\n"
        "    crate::free(pointer);\n"
        "}"
    )
    release["annotated_skeleton"] = (
        "pub unsafe fn release(mut pointer: *mut core::ffi::c_void) {\n"
        "    #[proctor(0)]\n"
        "    todo!();\n"
        "    #[proctor(1)]\n"
        "    todo!();\n"
        "}"
    )
    release["source_signature"] = (
        "pub unsafe fn release(mut pointer: *mut core::ffi::c_void)"
    )
    release["target_signature"] = release["source_signature"]
    scalar = fn_record(
        3,
        "parser::scalar",
        "scalar",
        [0],
        needs_transformation=False,
    )
    scalar["annotated_source"] = (
        "pub unsafe fn scalar(mut value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    crate::local_abi(value)\n"
        "}"
    )
    scalar["annotated_skeleton"] = scalar["annotated_source"]
    scalar["source_signature"] = "pub unsafe fn scalar(mut value: i32) -> i32"
    scalar["target_signature"] = scalar["source_signature"]
    return loaded([local_abi, scan, release, scalar])


def item_kind_records():
    assert "pub struct Point" in ITEM_KIND_SOURCE
    helper = fn_record(3, "helper", "helper", [0], [0], needs_transformation=False)
    helper["annotated_source"] = (
        "pub unsafe fn helper(mut point: Point) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    point.value\n"
        "}"
    )
    helper["annotated_skeleton"] = helper["annotated_source"]
    helper["source_signature"] = "pub unsafe fn helper(mut point: Point) -> i32"
    helper["target_signature"] = helper["source_signature"]
    target = fn_record(
        4,
        "target",
        "target",
        [0, 1, 2, 3],
        [0],
        needs_transformation=False,
    )
    target["annotated_source"] = (
        "pub unsafe fn target(mut point: Point) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    (helper(point) + LIMIT + STEP)\n"
        "}"
    )
    target["annotated_skeleton"] = target["annotated_source"]
    target["source_signature"] = "pub unsafe fn target(mut point: Point) -> i32"
    target["target_signature"] = target["source_signature"]
    return loaded(
        [
            type_record(
                0,
                "Point",
                "Struct",
                "pub struct Point {\n    pub value: i32,\n}",
                [],
            ),
            value_record(1, "LIMIT", "Static", "pub static LIMIT: i32;", [], []),
            value_record(2, "STEP", "Const", "pub const STEP: i32;", [], []),
            helper,
            target,
        ]
    )


def duplicate_name_records():
    assert "outer::left::parse" in DUPLICATE_NAME_SOURCE
    left = fn_record(
        0,
        "outer::left::parse",
        "parse",
        [],
        needs_transformation=False,
    )
    left["annotated_source"] = (
        "pub unsafe fn parse(mut value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    (value + 1)\n"
        "}"
    )
    left["annotated_skeleton"] = left["annotated_source"]
    left["source_signature"] = "pub unsafe fn parse(mut value: i32) -> i32"
    left["target_signature"] = left["source_signature"]
    right = copy.deepcopy(left)
    right["id"] = 1
    right["path"] = "outer::right::parse"
    right["annotated_source"] = str(right["annotated_source"]).replace(
        "(value + 1)", "(value - 1)"
    )
    right["annotated_skeleton"] = right["annotated_source"]
    target = fn_record(2, "target", "target", [0, 1], needs_transformation=False)
    target["annotated_source"] = (
        "pub unsafe fn target(mut value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    (outer::left::parse(value) + outer::right::parse(value))\n"
        "}"
    )
    target["annotated_skeleton"] = target["annotated_source"]
    target["source_signature"] = "pub unsafe fn target(mut value: i32) -> i32"
    target["target_signature"] = target["source_signature"]
    return loaded(
        [
            left,
            right,
            target,
        ]
    )


def loaded(raw: list[dict[str, object]]):
    return load_skeletons(json.dumps(raw, ensure_ascii=False))


def record_map(raw=CONTEXT_RECORDS):
    return {record.id: record for record in loaded(raw)}


def test_valid_records_load_without_reordering_or_text_changes():
    records = loaded(CONTEXT_RECORDS)
    assert [record.id for record in records] == [
        record["id"] for record in CONTEXT_RECORDS
    ]
    assert records[0].annotated_source == CONTEXT_RECORDS[0]["annotated_source"]
    assert records[0].dependencies == (1, 2, 20)


def test_top_level_and_kind_shapes_fail_clearly():
    for text in ("[", "{}", '[{"id":0,"path":"f","kind":"Module"}]'):
        with pytest.raises(SkeletonError):
            load_skeletons(text)
    with pytest.raises(SkeletonError, match="missing required fields"):
        load_skeletons('[{"id":0,"path":"f","kind":"Fn","name":"f"}]')


def test_loader_requires_and_preserves_function_disposition():
    record = fn_record(
        0,
        "f",
        "f",
        [],
        needs_transformation=True,
        transformation_labels=[1, 3],
    )
    parsed = loaded([record])[0]
    assert parsed.needs_transformation is True
    assert parsed.statements_requiring_transformation == (1, 3)
    for field in ("needs_transformation", "statements_requiring_transformation"):
        malformed = copy.deepcopy(record)
        del malformed[field]
        with pytest.raises(SkeletonError, match="missing required fields"):
            loaded([malformed])
    for labels in ([3, 1], [1, 1], [-1], [2**32], [True]):
        malformed = copy.deepcopy(record)
        malformed["statements_requiring_transformation"] = labels
        with pytest.raises(SkeletonError):
            loaded([malformed])
    for value in (0, 1, "true", None):
        malformed = copy.deepcopy(record)
        malformed["needs_transformation"] = value
        with pytest.raises(SkeletonError):
            loaded([malformed])
    for value in (None, 1, "1,3", {"labels": [1, 3]}):
        malformed = copy.deepcopy(record)
        malformed["statements_requiring_transformation"] = value
        with pytest.raises(SkeletonError):
            loaded([malformed])
    malformed = copy.deepcopy(record)
    malformed["needs_transformation"] = False
    with pytest.raises(SkeletonError, match="inconsistent"):
        loaded([malformed])


def test_function_records_and_python_helpers_add_statement_pair_metadata():
    plain = fn_record(0, "scalar", "scalar", [])
    assert list(plain) == [
        "id",
        "path",
        "kind",
        "name",
        "annotated_source",
        "annotated_skeleton",
        "source_signature",
        "target_signature",
        "needs_transformation",
        "statements_requiring_transformation",
        "statement_pair_metadata",
        "foreign_function_names",
        "signature_dependencies",
        "dependencies",
    ]
    assert plain["statement_pair_metadata"]
    assert plain["foreign_function_names"] == []
    assert scalar_records()[0].foreign_function_names == ()
    assert foreign_function_records()[1].foreign_function_names == ("free", "strlen")

    point = type_record(1, "Point", "Struct", "struct Point;", [])
    assert "foreign_function_names" not in point
    assert item_kind_records()[0].foreign_function_names == ()


def test_loader_requires_sorted_unique_nonempty_foreign_names():
    plain = fn_record(0, "scalar", "scalar", [])
    foreign = fn_record(
        0,
        "parser::scan",
        "scan",
        [],
        foreign_function_names=["free", "strlen"],
    )
    assert loaded([plain])[0].foreign_function_names == ()
    assert loaded([foreign])[0].foreign_function_names == ("free", "strlen")

    malformed_values = (
        None,
        "free",
        ["free", 1],
        [""],
        ["free", "free"],
        ["strlen", "free"],
    )
    for value in malformed_values:
        malformed = copy.deepcopy(foreign)
        if value is None:
            del malformed["foreign_function_names"]
        else:
            malformed["foreign_function_names"] = value
        with pytest.raises(SkeletonError):
            loaded([malformed])

    non_function = type_record(1, "Point", "Struct", "struct Point;", [])
    assert "foreign_function_names" not in non_function
    assert loaded([non_function])[0].foreign_function_names == ()


def test_ids_and_same_namespace_paths_are_valid_and_unique():
    base = [fn_record(0, "a::f", "f", []), fn_record(1, "b::g", "g", [])]
    for key, value in (("id", True), ("id", -1), ("path", "")):
        mutated = copy.deepcopy(base)
        mutated[0][key] = value
        with pytest.raises(SkeletonError):
            loaded(mutated)
    for key, value in (("id", 0), ("path", "a::f")):
        mutated = copy.deepcopy(base)
        mutated[1][key] = value
        with pytest.raises(SkeletonError):
            loaded(mutated)


def test_dependency_lists_must_be_sorted_unique_and_resolved():
    base = [
        fn_record(0, "f", "f", [1, 2], [1]),
        type_record(1, "A", "Struct", "struct A;", []),
        type_record(2, "B", "Struct", "struct B;", []),
    ]
    for dependencies in ([2, 1], [1, 1], [1, True], [1, 99]):
        mutated = copy.deepcopy(base)
        mutated[0]["dependencies"] = dependencies
        with pytest.raises(SkeletonError):
            loaded(mutated)


def test_signature_dependencies_must_be_dependency_subset():
    for value in (
        fn_record(0, "f", "f", [], [1]),
        value_record(0, "S", "Static", "static S: A;", [], [1]),
        value_record(0, "C", "Const", "const C: A;", [], [1]),
    ):
        with pytest.raises(SkeletonError, match="subset"):
            loaded([value, type_record(1, "A", "Struct", "struct A;", [])])


def test_kind_specific_fields_are_not_interchangeable():
    bad = [
        {
            "id": 0,
            "path": "S",
            "kind": "Struct",
            "declaration": "struct S;",
            "dependencies": [],
        },
        {
            "id": 0,
            "path": "N",
            "kind": "Const",
            "definition": "const N: usize = 1;",
            "signature_dependencies": [],
            "dependencies": [],
        },
        {**fn_record(0, "f", "f", []), "annotated_source": 4},
    ]
    for value in bad:
        with pytest.raises(SkeletonError):
            loaded([value])


def test_same_display_path_across_rust_namespaces_is_accepted():
    records = [
        type_record(0, "X", "TyAlias", "type X = i32;", []),
        value_record(1, "X", "Const", "const X: i32;", [], []),
        fn_record(2, "f", "f", [0, 1], [0]),
    ]
    assert [record.path for record in loaded(records)[:2]] == ["X", "X"]
    records[1] = fn_record(1, "X", "X", [])
    assert len(loaded(records)) == 3


def test_ids_accept_exact_u64_range_and_reject_overflow():
    assert loaded([fn_record(2**64 - 1, "max", "max", [])])[0].id == 2**64 - 1
    with pytest.raises(SkeletonError, match="u64"):
        loaded([fn_record(2**64, "overflow", "overflow", [])])


def test_graph_uses_only_function_valued_dependencies():
    assert function_graph(loaded(GRAPH_RECORDS))[1] == {0}
    assert 100 not in function_graph(loaded(GRAPH_RECORDS))


def test_direct_self_edges_are_retained():
    graph = function_graph(loaded([fn_record(5, "self_rec", "self_rec", [5])]))
    assert graph == {5: {5}}


def test_nonrecursive_singleton_has_no_synthetic_self_edge():
    assert function_graph(loaded([fn_record(5, "plain", "plain", [])])) == {5: set()}


def test_tarjan_partition_matches_nested_cycles_and_chains():
    assert strongly_connected_components(function_graph(loaded(GRAPH_RECORDS))) == (
        (0,),
        (1,),
        (2,),
        (3, 4),
        (5,),
        (6,),
        (7,),
        (8, 9),
    )


def test_leaf_schedule_is_exact_for_graph_fixture():
    assert leaf_schedule(function_graph(loaded(GRAPH_RECORDS))) == (
        (0,),
        (1,),
        (2,),
        (3, 4),
        (5,),
        (6,),
        (7,),
        (8, 9),
    )


def test_schedule_is_independent_of_record_and_dependency_order():
    records = tuple(reversed(loaded(GRAPH_RECORDS)))
    assert leaf_schedule(function_graph(records)) == leaf_schedule(
        function_graph(loaded(GRAPH_RECORDS))
    )


def test_duplicate_names_abort_only_inside_one_scc():
    records = loaded(
        [
            fn_record(8, "same_a::same", "same", [9]),
            fn_record(9, "same_b::same", "same", [8]),
        ]
    )
    scc = leaf_schedule(function_graph(records))[0]
    assert len({next(r.name for r in records if r.id == item) for item in scc}) == 1


def test_duplicate_names_in_distinct_sccs_are_allowed():
    records = loaded(
        [fn_record(1, "a::same", "same", []), fn_record(2, "b::same", "same", [1])]
    )
    assert leaf_schedule(function_graph(records)) == ((1,), (2,))


def test_nonrecursive_singleton_omits_own_signature():
    _, ids = dependency_context((0,), record_map())
    assert ids[:3] == (1, 2, 20)
    assert 0 not in ids


def test_direct_recursive_singleton_includes_own_signatures_once():
    records = record_map(
        [
            fn_record(0, "f", "f", [0, 1]),
            type_record(1, "T", "Struct", "struct T;", []),
        ]
    )
    _, ids = dependency_context((0,), records)
    assert ids == (0, 1)


def test_multi_function_scc_includes_every_member_signature_once():
    records = record_map(
        [
            fn_record(0, "m::a", "a", [1, 2]),
            fn_record(1, "m::b", "b", [0, 3]),
            type_record(2, "A", "Struct", "struct A;", []),
            value_record(3, "N", "Const", "const N: usize;", [], []),
        ]
    )
    _, ids = dependency_context((0, 1), records)
    assert ids == (0, 1, 2, 3)


def test_direct_function_static_and_type_entries_render_by_kind():
    records = record_map()
    text, _ = dependency_context((0,), records)
    assert records[1].source_signature in text
    assert records[2].declaration in text
    assert records[20].definition in text
    assert records[1].annotated_source not in text


def test_closure_follows_signature_edges_but_not_body_only_edges():
    _, ids = dependency_context((0,), record_map())
    assert ids == (1, 2, 20, 21, 23, 25, 26, 27, 28, 29)
    assert 22 not in ids and 24 not in ids


def test_closure_deduplicates_at_shortest_union_depth():
    text, _ = dependency_context((0,), record_map())
    assert text.count("### Struct `U`") == 1
    assert text.count("### Struct `X`") == 1


def test_entries_are_finally_sorted_by_item_id_not_discovery_parent():
    records = record_map(
        [
            fn_record(0, "f", "f", [10, 30]),
            type_record(10, "A", "Struct", "struct A;", [40]),
            type_record(20, "B", "Struct", "struct B;", []),
            type_record(30, "C", "Struct", "struct C;", [20]),
            type_record(40, "D", "Struct", "struct D;", []),
        ]
    )
    _, ids = dependency_context((0,), records)
    assert ids == (10, 20, 30, 40)


def test_empty_context_is_exact_empty_string():
    text, ids = dependency_context((0,), record_map([fn_record(0, "f", "f", [])]))
    assert text == "" and ids == ()


def test_rendering_matches_exact_golden():
    expected = (
        "### Function `callee`\n\nSource signature:\n```rust\n"
        "unsafe fn callee(mut p: *const T) -> i32\n```\nTarget signature:\n"
        "```rust\nunsafe fn callee(mut p: &T) -> i32\n```"
    )
    assert render_dependency_entry(record_map()[1]) == expected


def test_exact_limit_is_accepted():
    records = record_map()
    mandatory = "\n\n".join(
        render_dependency_entry(records[item]) for item in (1, 2, 20)
    )
    text, _ = dependency_context((0,), records, limit=len(mandatory))
    assert text == mandatory


def test_whole_depth_is_rejected_without_partial_entries():
    records = record_map()
    mandatory = "\n\n".join(
        render_dependency_entry(records[item]) for item in (1, 2, 20)
    )
    depth = "\n\n".join(
        render_dependency_entry(records[item]) for item in (1, 2, 20, 21, 23, 25)
    )
    text, ids = dependency_context((0,), records, limit=len(depth) - 1)
    assert text == mandatory and ids == (1, 2, 20)


def test_mandatory_overflow_aborts_before_llm():
    records = record_map()
    mandatory = "\n\n".join(
        render_dependency_entry(records[item]) for item in (1, 2, 20)
    )
    with pytest.raises(ContextOverflow):
        dependency_context((0,), records, limit=len(mandatory) - 1)


def test_transformation_targets_render_in_member_id_order():
    text = render_transformation_targets((3, 0), record_map())
    assert text.index("### Function `target`") < text.index("### Function `peer`")


def test_targets_use_final_names_and_omit_empty_foreign_line():
    records = {record.id: record for record in foreign_function_records()}
    text = render_transformation_targets((3, 2, 1), records)
    entries = text.split("\n\n### Function ")
    entries = [entries[0], *(f"### Function {entry}" for entry in entries[1:])]

    assert entries[0].startswith(
        "### Function `scan`\n\n"
        "Foreign function references: `free`, `strlen`\n\n"
        "Source:\n"
    )
    assert entries[1].startswith(
        "### Function `release`\n\nForeign function references: `free`\n\nSource:\n"
    )
    assert entries[2].startswith("### Function `scalar`\n\nSource:\n")
    assert "Foreign function references:" not in entries[2]
    headings = [entry.splitlines()[0] for entry in entries]
    assert headings == [
        "### Function `scan`",
        "### Function `release`",
        "### Function `scalar`",
    ]
    assert all("parser::" not in heading for heading in headings)
    assert text == "\n\n".join(entries)


def test_noncolliding_dependencies_use_kind_and_final_name():
    records = {record.id: record for record in item_kind_records()}
    text, selected = dependency_context((4,), records)
    assert selected == (0, 1, 2, 3)
    headings = [line for line in text.splitlines() if line.startswith("### ")]
    assert headings == [
        "### Struct `Point`",
        "### Static `LIMIT`",
        "### Const `STEP`",
        "### Function `helper`",
    ]
    assert all(f" {item_id}:" not in text for item_id in selected)
    assert all("::" not in heading for heading in headings)


def test_duplicate_dependency_names_remain_name_only():
    records = {record.id: record for record in duplicate_name_records()}
    text, selected = dependency_context((2,), records)
    assert selected == (0, 1)
    assert text.count("### Function `parse`") == 2
    headings = [line for line in text.splitlines() if line.startswith("### ")]
    assert headings == ["### Function `parse`", "### Function `parse`"]
    assert all(
        component not in heading
        for heading in headings
        for component in ("outer::", "left::", "right::")
    )


def test_budget_counts_final_name_only_entries_not_section_heading():
    records = {record.id: record for record in duplicate_name_records()}
    entries = "\n\n".join(
        render_dependency_entry(records[item_id]) for item_id in (0, 1)
    )
    text, selected = dependency_context((2,), records, limit=len(entries))
    assert text == entries
    assert selected == (0, 1)
    with pytest.raises(ContextOverflow):
        dependency_context((2,), records, limit=len(entries) - 1)

    rendered = render_prompt(PromptRenderInput(text, "TARGET"))
    assert f"## Dependency Context\n\n{entries}" in rendered.text


def test_existing_context_budget_and_prompt_goldens_use_final_presentation():
    kind_records = {record.id: record for record in item_kind_records()}
    kind_context, _ = dependency_context((4,), kind_records)
    collision_records = {record.id: record for record in duplicate_name_records()}
    collision_context, _ = dependency_context((2,), collision_records)
    foreign_records = {record.id: record for record in foreign_function_records()}
    targets = render_transformation_targets((1,), foreign_records)

    authored = "\n\n".join((kind_context, collision_context, targets))
    headings = [line for line in authored.splitlines() if line.startswith("### ")]
    assert headings
    assert all(not any(char.isdigit() for char in heading) for heading in headings)
    assert all("::" not in heading for heading in headings)
    rendered = render_prompt(PromptRenderInput(kind_context, targets))
    assert "## Dependency Context" in rendered.text
    assert "## Transformation Targets" in rendered.text
    assert "\nDependency Context:" not in rendered.text
    assert "\nTransformation Targets:" not in rendered.text


def test_initial_prompt_uses_versioned_template_and_empty_repair():
    rendered = render_prompt(PromptRenderInput("DEPENDENCY\n", "TARGETS\n"))
    request = llm_request(rendered, run_id="local-transformation-run", members=(3, 0))
    assert rendered.id == "local_transformation" and rendered.version == 1
    assert "The previous transformation failed." not in rendered.text
    assert request.metadata.item == "0,3"


def test_repair_prompt_contains_only_latest_failure():
    rendered = render_prompt(
        PromptRenderInput("D", "T", "second bad code", "second diagnostics")
    )
    assert "second bad code" in rendered.text and "first bad code" not in rendered.text


def test_version_1_prompt_has_exact_hierarchy_and_advisory():
    kind_records = {record.id: record for record in item_kind_records()}
    dependency_entries, _ = dependency_context((4,), kind_records)
    foreign_records = {record.id: record for record in foreign_function_records()}
    target_entries = render_transformation_targets((1,), foreign_records)
    rendered = render_prompt(PromptRenderInput(dependency_entries, target_entries))
    expected = (
        PROMPT_GOLDEN.read_text()
        .replace("{{ dependency_context }}", dependency_entries)
        .replace("{{ transformation_targets }}", target_entries)
    )
    assert rendered.text == expected
    assert rendered.content_hash == hashlib.sha256(expected.encode()).hexdigest()
    assert (
        rendered.content_hash
        == "c121eec81f2aaa7eb3955448c5eb6075780a97f5e2784ffae9a3df8b781ee174"
    )
    preservation_instruction = (
        "Complete every generated `todo!()` hole. Preserve every complete labeled "
        "statement already present in the Target Skeleton exactly as provided."
    )
    advisory = (
        "10. For each listed foreign-function reference, prefer a behavior-equivalent\n"
        "    safe Rust function or method when one is available; otherwise preserve the\n"
        "    foreign call."
    )
    assert rendered.text.count(preservation_instruction) == 1
    assert rendered.text.count(advisory) == 1
    assert "11. Do not introduce an explicit `unsafe` block" in rendered.text
    assert "12. Return exactly one Rust code block" in rendered.text
    assert (
        f"## Dependency Context\n\n{dependency_entries}\n\n"
        f"## Transformation Targets\n\n{target_entries}"
    ) in rendered.text
    assert all(
        line.startswith(("## ", "### "))
        for line in rendered.text.splitlines()
        if line.startswith("##")
    )


def test_empty_dependency_section_is_omitted_and_repair_text_is_unchanged():
    plain = {record.id: record for record in scalar_records()}
    context, selected = dependency_context((0,), plain)
    assert context == "" and selected == ()
    targets = render_transformation_targets((0,), plain)
    initial = render_prompt(PromptRenderInput(context, targets))
    assert "## Transformation Targets" in initial.text
    assert "## Dependency Context" not in initial.text
    assert "\nDependency Context:" not in initial.text
    assert "Foreign function references:" not in initial.text

    failed = "unsafe fn scalar() { broken }"
    diagnostics = (
        '{"schema_version":1,"status":"invalid","failures":[{"id":0,'
        '"name":"scalar","failed_snippet":"unsafe fn scalar() { broken }",'
        '"errors":[{"code":"missing_label","message":"Function `scalar` '
        '(item 0): label 0 is missing."}]}]}'
    )
    repaired = render_prompt(PromptRenderInput(context, targets, failed, diagnostics))
    assert initial.text.rstrip() in repaired.text
    assert repaired.text.count("## Transformation Targets") == 1
    assert failed in repaired.text
    assert diagnostics in repaired.text


def test_single_fence_ignores_surrounding_prose_and_preserves_interior():
    assert extract_code_block("x\n```rust\nfirst\n\n```\ny").candidate == "first\n"
    assert (
        extract_code_block("```rust\r\nfirst\r\nsecond\r\n```").candidate
        == "first\r\nsecond"
    )


def test_longest_block_wins_and_first_breaks_tie():
    assert (
        extract_code_block(
            "```rust\nshort\n```\n```rust\nthis block is longer\n```"
        ).candidate
        == "this block is longer"
    )
    assert (
        extract_code_block("```rust\nfirst\n```\n```text\nlater\n```").candidate
        == "first"
    )


@pytest.mark.parametrize(
    "text",
    [
        "I cannot provide that transformation.",
        "~~~~rust\nnot a triple-backtick block\n~~~~",
        "````rust\nnot exact\n````",
        "prefix ```rust\ninline\n```",
        "  ```rust\nindented\n```",
        "```rust extra\nbad\n```",
        "```rust\nbad\n``` ",
        "```rust\nunclosed",
    ],
)
def test_missing_fence_is_repairable_with_raw_failed_text(text):
    result = extract_code_block(text)
    assert result.candidate is None
    assert result.failed_text == text
    assert result.diagnostics == NO_FENCE_DIAGNOSTIC


def test_validation_request_is_exact_and_member_ordered():
    request = validation_request((3, 0), record_map(), "code")
    assert [value["id"] for value in request["expected_functions"]] == [0, 3]
    assert list(request["expected_functions"][0]) == [
        "id",
        "name",
        "skeleton",
        "needs_transformation",
        "statements_requiring_transformation",
    ]
    assert request["transformation"] == "code"


def test_replacement_request_is_exact_and_member_ordered():
    request = replacement_request((3, 0), record_map(), "code")
    assert [value["id"] for value in request["items"]] == [0, 3]
    assert list(request["items"][0]) == [
        "id",
        "path",
        "name",
        "skeleton",
        "needs_transformation",
        "statements_requiring_transformation",
    ]
    assert set(request) == {
        "schema_version",
        "items",
        "transformation",
        "accepted_correspondence",
    }


def test_foreign_metadata_does_not_change_graph_or_tool_requests():
    records = foreign_function_records()
    records_by_id = {record.id: record for record in records}
    assert function_graph(records) == {0: set(), 1: {0}, 2: set(), 3: {0}}

    transformation = (
        "pub unsafe fn release(mut pointer: *mut core::ffi::c_void) {\n"
        "    #[proctor(0)]\n"
        "    crate::free(pointer);\n"
        "    #[proctor(1)]\n"
        "    crate::free(pointer);\n"
        "}"
    )
    validation = validation_request((2,), records_by_id, transformation)
    replacement = replacement_request((2,), records_by_id, transformation)
    assert validation["transformation"] == transformation
    assert replacement["transformation"] == transformation
    assert "foreign_function_names" not in validation["expected_functions"][0]
    assert "foreign_function_names" not in replacement["items"][0]
    assert list(validation["expected_functions"][0]) == [
        "id",
        "name",
        "skeleton",
        "needs_transformation",
        "statements_requiring_transformation",
    ]
    assert list(replacement["items"][0]) == [
        "id",
        "path",
        "name",
        "skeleton",
        "needs_transformation",
        "statements_requiring_transformation",
    ]


def test_command_builders_use_exact_four_output_and_extract_argv():
    tool = Path("/tools/crat-tool")
    assert make_skeleton_command(
        tool, Path("/work/current"), Path("/work/skeletons.json")
    ) == [
        "/tools/crat-tool",
        "make-skeleton",
        "--output",
        "/work/skeletons.json",
        "/work/current",
    ]
    assert normalize_safety_command(
        tool, Path("/work/current/lib.rs"), Path("/work/normalized.rs")
    ) == [
        "/tools/crat-tool",
        "normalize-safety",
        "--output",
        "/work/normalized.rs",
        "/work/current/lib.rs",
    ]
    assert validate_command(
        tool,
        Path("/work/validation-request.json"),
        Path("/work/validation-response.json"),
    ) == [
        "/tools/crat-tool",
        "validate",
        "--input",
        "/work/validation-request.json",
        "--output",
        "/work/validation-response.json",
    ]
    assert replace_command(
        tool,
        Path("/work/current"),
        Path("/work/replacement-request.json"),
        Path("/work/candidate.rs"),
        Path("/work/replacement-statement-pairs.json"),
        Path("/work/replacement-observation.rs"),
        Path("/work/replacement-observation-metadata.json"),
    ) == [
        "/tools/crat-tool",
        "replace",
        "--request",
        "/work/replacement-request.json",
        "--output",
        "/work/candidate.rs",
        "--statement-pairs-output",
        "/work/replacement-statement-pairs.json",
        "--observation-source-output",
        "/work/replacement-observation.rs",
        "--observation-metadata-output",
        "/work/replacement-observation-metadata.json",
        "/work/current",
    ]
    assert extract_observations_command(
        tool,
        Path("/work/replacement-observation.rs"),
        Path("/work/replacement-observation-metadata.json"),
        Path("/work/extracted-observations.json"),
    ) == [
        "/tools/crat-tool",
        "extract-observations",
        "--metadata",
        "/work/replacement-observation-metadata.json",
        "--output",
        "/work/extracted-observations.json",
        "/work/replacement-observation.rs",
    ]


VALID = {"schema_version": 1, "status": "valid"}
INVALID = {
    "schema_version": 1,
    "status": "invalid",
    "failures": [
        {
            "id": 0,
            "name": "target",
            "failed_snippet": "unsafe fn target() {}",
            "errors": [
                {
                    "code": "missing_label",
                    "message": "Function `target` (item 0): label 0 is missing.",
                }
            ],
        }
    ],
}


def _valid_observation_document():
    raw_pointer = {
        "kind": "raw_pointer",
        "mutability": "const",
        "pointee": {"kind": "primitive", "name": "i32"},
    }
    reference = {
        "kind": "reference",
        "mutability": "shared",
        "pointee": {"kind": "primitive", "name": "i32"},
    }
    binding = {"kind": "path", "value": {"kind": "binding", "id": "<id0>"}}
    return {
        "schema_version": 1,
        "observations": [
            {
                "source_expression": binding,
                "target_expression": copy.deepcopy(binding),
                "pointer_anchors": [
                    {
                        "id": "<id0>",
                        "source_type": raw_pointer,
                        "target_type": reference,
                    }
                ],
                "source_type": copy.deepcopy(raw_pointer),
                "source_adjusted_type": copy.deepcopy(raw_pointer),
                "target_type": copy.deepcopy(reference),
                "target_adjusted_type": copy.deepcopy(reference),
            }
        ],
    }


def test_strict_observation_loader_uses_exact_valid_base_document():
    value = _valid_observation_document()
    loaded_value = load_observations(json.dumps(value))
    assert loaded_value.observations == tuple(value["observations"])

    mutations = []
    unknown = copy.deepcopy(value)
    unknown["extra"] = True
    mutations.append(unknown)
    unknown_observation = copy.deepcopy(value)
    unknown_observation["observations"][0]["extra"] = True
    mutations.append(unknown_observation)
    unknown_expression = copy.deepcopy(value)
    unknown_expression["observations"][0]["source_expression"]["extra"] = True
    mutations.append(unknown_expression)
    unknown_identity = copy.deepcopy(value)
    unknown_identity["observations"][0]["source_expression"]["value"]["extra"] = True
    mutations.append(unknown_identity)
    unknown_anchor = copy.deepcopy(value)
    unknown_anchor["observations"][0]["pointer_anchors"][0]["extra"] = True
    mutations.append(unknown_anchor)
    unknown_nested_type = copy.deepcopy(value)
    unknown_nested_type["observations"][0]["source_type"]["pointee"]["extra"] = True
    mutations.append(unknown_nested_type)
    boolean_version = copy.deepcopy(value)
    boolean_version["schema_version"] = True
    mutations.append(boolean_version)
    target_only = copy.deepcopy(value)
    target_only["observations"][0]["target_expression"]["value"]["id"] = "<id1>"
    with pytest.raises(ObservationError, match="target-only anonymized ID <id1>"):
        load_observations(json.dumps(target_only))
    noncontiguous = copy.deepcopy(value)
    noncontiguous["observations"][0]["source_expression"]["value"]["id"] = "<id2>"
    noncontiguous["observations"][0]["target_expression"]["value"]["id"] = "<id2>"
    noncontiguous["observations"][0]["pointer_anchors"][0]["id"] = "<id2>"
    mutations.append(noncontiguous)
    unknown_type = copy.deepcopy(value)
    unknown_type["observations"][0]["source_type"]["kind"] = "pointer"
    mutations.append(unknown_type)
    empty_anchors = copy.deepcopy(value)
    empty_anchors["observations"][0]["pointer_anchors"] = []
    mutations.append(empty_anchors)
    nonraw_anchor = copy.deepcopy(value)
    nonraw_anchor["observations"][0]["pointer_anchors"][0]["source_type"] = {
        "kind": "reference",
        "mutability": "shared",
        "pointee": {"kind": "primitive", "name": "i32"},
    }
    mutations.append(nonraw_anchor)
    invalid_mutability = copy.deepcopy(value)
    invalid_mutability["observations"][0]["pointer_anchors"][0]["source_type"][
        "mutability"
    ] = "shared"
    mutations.append(invalid_mutability)
    invalid_id = copy.deepcopy(value)
    invalid_id["observations"][0]["source_expression"]["value"]["id"] = "id0"
    mutations.append(invalid_id)
    duplicate_anchor = copy.deepcopy(value)
    duplicate_anchor["observations"][0]["pointer_anchors"].append(
        copy.deepcopy(duplicate_anchor["observations"][0]["pointer_anchors"][0])
    )
    mutations.append(duplicate_anchor)
    for mutation in mutations:
        with pytest.raises(ObservationError):
            load_observations(json.dumps(mutation))
    with pytest.raises(ObservationError):
        load_observations(json.dumps(value) + " trailing")


def test_replacement_metadata_paths_use_canonical_rust_identifier_segments():
    base = {
        "schema_version": 1,
        "candidate_sha256": "0" * 64,
        "statement_pairs_sha256": "0" * 64,
        "observation_source_sha256": "0" * 64,
        "accepted_correspondence": [],
        "new_correspondence": [
            {
                "item_id": 1,
                "logical_path": "módulo::r#type",
                "implementation_path": "módulo::r#type",
                "wrapper_path": None,
            }
        ],
        "current_items": [
            {
                "item_id": 1,
                "logical_path": "módulo::r#type",
                "source_copy_path": "módulo::__copy",
                "implementation_path": "módulo::r#type",
                "wrapper_path": None,
                "transform_labels": [0],
            }
        ],
    }
    load_replacement_metadata(json.dumps(base))
    for invalid in (
        "fn",
        "_",
        "r#_",
        "r#self",
        "crate::f",
        "::f",
        "f::",
        "a::::b",
    ):
        malformed = copy.deepcopy(base)
        malformed["new_correspondence"][0]["logical_path"] = invalid
        with pytest.raises(ObservationError, match="canonical"):
            load_replacement_metadata(json.dumps(malformed))


def test_observation_loader_enforces_namespace_order_float_widths_and_target_only_policy():
    value = _valid_observation_document()
    binding0 = copy.deepcopy(value["observations"][0]["source_expression"])
    binding1 = copy.deepcopy(binding0)
    binding1["value"]["id"] = "<id1>"

    reordered = copy.deepcopy(value)
    reordered_expression = {
        "kind": "tuple",
        "elements": [binding1, copy.deepcopy(binding0)],
    }
    reordered["observations"][0]["source_expression"] = reordered_expression
    reordered["observations"][0]["target_expression"] = copy.deepcopy(
        reordered_expression
    )
    with pytest.raises(ObservationError, match="first-occurrence order"):
        load_observations(json.dumps(reordered))

    target_nonbinding = copy.deepcopy(value)
    target_nonbinding["observations"][0]["target_expression"] = {
        "kind": "tuple",
        "elements": [
            copy.deepcopy(binding0),
            {
                "kind": "path",
                "value": {
                    "kind": "constructor",
                    "adt": {"kind": "local", "id": "<struct0>"},
                    "variant": None,
                },
            },
        ],
    }
    load_observations(json.dumps(target_nonbinding))

    target_function = copy.deepcopy(value)
    target_function["observations"][0]["target_expression"] = {
        "kind": "tuple",
        "elements": [
            copy.deepcopy(binding0),
            {"kind": "path", "value": {"kind": "function", "id": "<fn0>"}},
        ],
    }
    with pytest.raises(ObservationError, match="target-only anonymized ID <fn0>"):
        load_observations(json.dumps(target_function))

    for float_type, width in (("f16", 4), ("f32", 8), ("f64", 16), ("f128", 32)):
        floating = copy.deepcopy(value)
        expression = {
            "kind": "tuple",
            "elements": [
                copy.deepcopy(binding0),
                {
                    "kind": "literal",
                    "value": {
                        "kind": "float",
                        "bits": "0" * width,
                        "type": float_type,
                    },
                },
            ],
        }
        floating["observations"][0]["source_expression"] = expression
        floating["observations"][0]["target_expression"] = copy.deepcopy(expression)
        load_observations(json.dumps(floating))
        for invalid_bits in ("0" * (width - 1), "0" * (width + 1), "A" * width):
            malformed = copy.deepcopy(floating)
            malformed["observations"][0]["source_expression"]["elements"][1]["value"][
                "bits"
            ] = invalid_bits
            with pytest.raises(ObservationError, match="exactly"):
                load_observations(json.dumps(malformed))

    for invalid_length in (True, -1, 2**64):
        malformed = copy.deepcopy(value)
        malformed["observations"][0]["source_type"] = {
            "kind": "array",
            "element": {"kind": "primitive", "name": "u8"},
            "length": invalid_length,
        }
        with pytest.raises(ObservationError):
            load_observations(json.dumps(malformed))


def test_metadata_digests_and_cross_file_contract_are_strict(tmp_path):
    candidate = tmp_path / "candidate.rs"
    statement_pairs = tmp_path / "pairs.json"
    observation_source = tmp_path / "observation.rs"
    metadata_path = tmp_path / "metadata.json"
    candidate.write_text("pub unsafe fn read(mut pointer: &i32) -> i32 { *pointer }\n")
    statement_pairs.write_text(
        '{"schema_version":1,"statements":[{"item_id":7,"path":"read",'
        '"label":0,"after_statement":"*pointer"}]}\n'
    )
    observation_source.write_text(
        "unsafe fn __proctor_source_read(mut pointer: *const i32) -> i32 { "
        "#[proctor(0)] *pointer }\n"
        "unsafe fn read(mut pointer: &i32) -> i32 { #[proctor(0)] *pointer }\n"
        "pub unsafe fn __proctor_wrapper_read(mut pointer: *const i32) -> i32 {\n"
        "    let __proctor_result = crate::read(&*(pointer as *const i32));\n"
        "    __proctor_result\n"
        "}\n"
    )
    correspondence = {
        "item_id": 7,
        "logical_path": "read",
        "implementation_path": "read",
        "wrapper_path": "__proctor_wrapper_read",
    }
    valid = {
        "schema_version": 1,
        "candidate_sha256": "6c3ea56d9debffcf25243e9a41d58805af269772d266088c83d19053f7ccebf1",
        "statement_pairs_sha256": "2b8e6af47f728734179fa6d023e74d812a888e65d4f111e8ff4a6c01f75c823b",
        "observation_source_sha256": "5d00cc190ae11801bb4ae2af09f7eacb12c2fee35f8645b1aef611a12cf09fd0",
        "accepted_correspondence": [],
        "new_correspondence": [correspondence],
        "current_items": [
            {
                **correspondence,
                "source_copy_path": "__proctor_source_read",
                "transform_labels": [0],
            }
        ],
    }
    metadata_path.write_text(json.dumps(valid))
    assert (
        hashlib.sha256(candidate.read_bytes()).hexdigest() == valid["candidate_sha256"]
    )
    assert (
        hashlib.sha256(statement_pairs.read_bytes()).hexdigest()
        == valid["statement_pairs_sha256"]
    )
    assert (
        hashlib.sha256(observation_source.read_bytes()).hexdigest()
        == valid["observation_source_sha256"]
    )
    record = loaded(
        [
            fn_record(
                7,
                "read",
                "read",
                [],
                transformation_labels=[0],
                statement_pair_metadata=[_pointer_metadata(0)],
            )
        ]
    )[0]
    parsed = _load_and_validate_replacement_metadata(
        metadata_path,
        candidate,
        statement_pairs,
        observation_source,
        (7,),
        {7: record},
        (),
    )
    assert parsed.current_items[0].source_copy_path == "__proctor_source_read"

    for field, message in (
        ("candidate_sha256", "does not match candidate bytes"),
        ("statement_pairs_sha256", "does not match statement-pairs sidecar bytes"),
        ("observation_source_sha256", "does not match observation source bytes"),
    ):
        mutated = copy.deepcopy(valid)
        mutated[field] = "0" * 64
        metadata_path.write_text(json.dumps(mutated))
        with pytest.raises(StageFailure, match=message):
            _load_and_validate_replacement_metadata(
                metadata_path,
                candidate,
                statement_pairs,
                observation_source,
                (7,),
                {7: record},
                (),
            )

    mutated = copy.deepcopy(valid)
    mutated["accepted_correspondence"] = [correspondence]
    metadata_path.write_text(json.dumps(mutated))
    with pytest.raises(StageFailure, match="does not equal the request"):
        _load_and_validate_replacement_metadata(
            metadata_path,
            candidate,
            statement_pairs,
            observation_source,
            (7,),
            {7: record},
            (),
        )

    for field in ("item_id", "logical_path", "implementation_path", "wrapper_path"):
        mutated = copy.deepcopy(valid)
        replacement = 8 if field == "item_id" else f"different_{field}"
        mutated["current_items"][0][field] = replacement
        metadata_path.write_text(json.dumps(mutated))
        with pytest.raises(
            StageFailure,
            match=rf"current_items\[0\]\.{field} disagrees with new_correspondence\[0\]\.{field}",
        ):
            _load_and_validate_replacement_metadata(
                metadata_path,
                candidate,
                statement_pairs,
                observation_source,
                (7,),
                {7: record},
                (),
            )

    mutated = copy.deepcopy(valid)
    mutated["current_items"][0]["transform_labels"] = [1]
    metadata_path.write_text(json.dumps(mutated))
    with pytest.raises(
        StageFailure,
        match=r"current_items\[0\]\.transform_labels does not equal \[0\]",
    ):
        _load_and_validate_replacement_metadata(
            metadata_path,
            candidate,
            statement_pairs,
            observation_source,
            (7,),
            {7: record},
            (),
        )

    absent = copy.deepcopy(valid)
    absent["new_correspondence"][0]["wrapper_path"] = None
    absent["current_items"][0]["wrapper_path"] = None
    metadata_path.write_text(json.dumps(absent))
    _load_and_validate_replacement_metadata(
        metadata_path,
        candidate,
        statement_pairs,
        observation_source,
        (7,),
        {7: record},
        (),
    )

    second_record = loaded(
        [
            fn_record(
                8,
                "other",
                "other",
                [],
                transformation_labels=[0],
                statement_pair_metadata=[_pointer_metadata(0)],
            )
        ]
    )[0]
    two = copy.deepcopy(valid)
    two["new_correspondence"].append(
        {
            "item_id": 8,
            "logical_path": "other",
            "implementation_path": "other",
            "wrapper_path": "__proctor_wrapper_other",
        }
    )
    two["current_items"].append(
        {
            **two["new_correspondence"][1],
            "source_copy_path": "__proctor_source_other",
            "transform_labels": [0],
        }
    )
    records = {7: record, 8: second_record}
    metadata_path.write_text(json.dumps(two))
    _load_and_validate_replacement_metadata(
        metadata_path,
        candidate,
        statement_pairs,
        observation_source,
        (7, 8),
        records,
        (),
    )

    reordered = copy.deepcopy(two)
    reordered["new_correspondence"].reverse()
    reordered["current_items"].reverse()
    metadata_path.write_text(json.dumps(reordered))
    with pytest.raises(StageFailure, match="records do not preserve request order"):
        _load_and_validate_replacement_metadata(
            metadata_path,
            candidate,
            statement_pairs,
            observation_source,
            (7, 8),
            records,
            (),
        )

    for field, expected in (
        ("item_id", "duplicate item_id 7"),
        ("logical_path", "duplicate logical_path read"),
        ("implementation_path", "duplicate implementation_path read"),
        ("wrapper_path", "duplicate wrapper_path __proctor_wrapper_read"),
    ):
        duplicated = copy.deepcopy(valid)
        accepted_record = {
            "item_id": 6,
            "logical_path": "accepted",
            "implementation_path": "accepted",
            "wrapper_path": "__proctor_wrapper_accepted",
        }
        accepted_record[field] = duplicated["new_correspondence"][0][field]
        duplicated["accepted_correspondence"] = [accepted_record]
        metadata_path.write_text(json.dumps(duplicated))
        accepted_value = load_replacement_metadata(
            json.dumps(duplicated)
        ).accepted_correspondence
        with pytest.raises(StageFailure, match=expected):
            _load_and_validate_replacement_metadata(
                metadata_path,
                candidate,
                statement_pairs,
                observation_source,
                (7,),
                {7: record},
                accepted_value,
            )

    duplicated = copy.deepcopy(two)
    duplicated["current_items"][1]["source_copy_path"] = "__proctor_source_read"
    metadata_path.write_text(json.dumps(duplicated))
    with pytest.raises(
        StageFailure, match="duplicate source_copy_path __proctor_source_read"
    ):
        _load_and_validate_replacement_metadata(
            metadata_path,
            candidate,
            statement_pairs,
            observation_source,
            (7, 8),
            records,
            (),
        )

    collision = copy.deepcopy(two)
    collision["current_items"][1]["source_copy_path"] = "read"
    metadata_path.write_text(json.dumps(collision))
    with pytest.raises(
        StageFailure,
        match="path read is used as both logical_path and source_copy_path",
    ):
        _load_and_validate_replacement_metadata(
            metadata_path,
            candidate,
            statement_pairs,
            observation_source,
            (7, 8),
            records,
            (),
        )


class FakeTools:
    def __init__(
        self,
        skeletons=None,
        normalized="normalized\n",
        builds=None,
        validators=None,
        candidates=None,
        sidecars=None,
        observations=None,
    ):
        self.skeletons = [] if skeletons is None else skeletons
        self.normalized = normalized
        self.builds = list(builds or [CommandResult(0)])
        self.validators = list(validators or [])
        self.candidates = list(candidates or [])
        self.sidecars = None if sidecars is None else list(sidecars)
        self.observations = None if observations is None else list(observations)
        self.events = []

    def build_tools(self, crat_dir):
        self.events.append(("build_tools", crat_dir))
        return crat_dir / "target/release/crat", crat_dir / "target/release/crat-tool"

    def prepare(self, current, passes, use_print):
        self.events.append(("prepare", current, passes, use_print))

    def make_skeleton(self, current, output):
        self.events.append(("make_skeleton", current, output))
        output.write_text(json.dumps(self.skeletons), encoding="utf-8")

    def normalize(self, library, output):
        self.events.append(("normalize", library, output))
        output.write_text(self.normalized, encoding="utf-8")

    def cargo_build(self, current):
        self.events.append(("cargo_build", current, (current / "lib.rs").read_text()))
        return self.builds.pop(0)

    def validate(self, request, response):
        self.events.append(("validate", json.loads(request.read_text())))
        value = self.validators.pop(0)
        raw = value if isinstance(value, str) else json.dumps(value)
        parsed = json.loads(raw)
        response.write_text(raw, encoding="utf-8")
        return raw, parsed

    def replace(
        self,
        current,
        request,
        candidate,
        statement_pairs_output,
        observation_source_output,
        observation_metadata_output,
    ):
        request_value = json.loads(request.read_text())
        self.events.append(
            (
                "replace",
                current,
                request_value,
                (current / "lib.rs").read_text(),
                candidate,
                statement_pairs_output,
                observation_source_output,
                observation_metadata_output,
            )
        )
        candidate.write_text(self.candidates.pop(0), encoding="utf-8")
        if self.sidecars is None:
            statements = [
                {
                    "item_id": item["id"],
                    "path": item["path"],
                    "label": label,
                    "after_statement": f"#[proctor({label})]\n()",
                }
                for item in request_value["items"]
                for label in item["statements_requiring_transformation"]
            ]
            sidecar = {"schema_version": 1, "statements": statements}
        else:
            sidecar = self.sidecars.pop(0)
        statement_pairs_output.write_text(
            sidecar if isinstance(sidecar, str) else json.dumps(sidecar),
            encoding="utf-8",
        )
        observation_source_output.write_text(
            "// observation source\n", encoding="utf-8"
        )
        new_correspondence = []
        current_items = []
        for item in request_value["items"]:
            prefix, _, name = item["path"].rpartition("::")
            source_copy = f"__proctor_source_{name.removeprefix('r#')}"
            source_copy_path = f"{prefix}::{source_copy}" if prefix else source_copy
            record = {
                "item_id": item["id"],
                "logical_path": item["path"],
                "implementation_path": item["path"],
                "wrapper_path": None,
            }
            new_correspondence.append(record)
            current_items.append(
                {
                    **record,
                    "source_copy_path": source_copy_path,
                    "transform_labels": item["statements_requiring_transformation"],
                }
            )
        observation_metadata_output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate_sha256": hashlib.sha256(
                        candidate.read_bytes()
                    ).hexdigest(),
                    "statement_pairs_sha256": hashlib.sha256(
                        statement_pairs_output.read_bytes()
                    ).hexdigest(),
                    "observation_source_sha256": hashlib.sha256(
                        observation_source_output.read_bytes()
                    ).hexdigest(),
                    "accepted_correspondence": request_value["accepted_correspondence"],
                    "new_correspondence": new_correspondence,
                    "current_items": current_items,
                }
            ),
            encoding="utf-8",
        )

    def extract_observations(self, observation_source, metadata, output):
        self.events.append(
            ("extract_observations", observation_source, metadata, output)
        )
        value = (
            {"schema_version": 1, "observations": []}
            if self.observations is None
            else self.observations.pop(0)
        )
        if isinstance(value, Exception):
            raise value
        output.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(
            text=value,
            finish_reason="stop",
            provider="replay",
            model="fixture-model",
            latency_s=0.25,
            usage=Usage(100, 20, 30, 4),
        )


def response(name="target"):
    return f"```rust\nunsafe fn {name}() {{\n    #[proctor(0)]\n    ()\n}}\n```"


def stage_input(tmp_path, *, artifacts=True, config=None, llm=None):
    source = tmp_path / "input"
    source.mkdir()
    (source / "Cargo.toml").write_text('[lib]\npath = "lib.rs"\n', encoding="utf-8")
    (source / "lib.rs").write_text("old\n", encoding="utf-8")
    (source / "proctor.toml").write_text("wrappers = []\n", encoding="utf-8")
    (source / "target").mkdir()
    (source / "target/cache").write_text("warm\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    artifact_dir = tmp_path / "artifacts" if artifacts else None
    if artifact_dir:
        artifact_dir.mkdir()
    return StageInput(
        run_id="local-transformation-run",
        stage_id="local_transformation",
        stage_index=0,
        inputs=InputArtifacts(rust_project=source),
        outputs=OutputDestinations(
            rust_project=tmp_path / "output", artifacts_dir=artifact_dir
        ),
        config=config or {},
        framework=FrameworkSettings(
            llm=llm
            or {
                "provider": "replay",
                "model": "fixture-model",
                "pricing": {
                    "replay/fixture-model": {
                        "input": 0,
                        "cached_input": 0,
                        "output": 0,
                    }
                },
            },
            workdir=work,
        ),
    )


def run_fake(tmp_path, tools, client, **kwargs):
    value = stage_input(tmp_path, **kwargs)
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: client,
    )
    return value, output


TOOL_OPERATIONS = (
    "deps_crate cargo build",
    "release cargo build --bin crat",
    "release cargo build --bin crat-tool",
    "ordinary Crat prepare",
    "crat-tool make-skeleton",
    "crat-tool normalize-safety",
    "crat-tool validate",
    "crat-tool replace",
)


def _classify_tool_command(command, cwd):
    if command[:2] == ["git", "rev-parse"]:
        return "git revision"
    if command == ["cargo", "build"] and cwd.name == "deps_crate":
        return "deps_crate cargo build"
    if command == ["cargo", "build", "--release", "--bin", "crat"]:
        return "release cargo build --bin crat"
    if command == ["cargo", "build", "--release", "--bin", "crat-tool"]:
        return "release cargo build --bin crat-tool"
    if len(command) > 1 and command[1] == "make-skeleton":
        return "crat-tool make-skeleton"
    if len(command) > 1 and command[1] == "normalize-safety":
        return "crat-tool normalize-safety"
    if len(command) > 1 and command[1] == "validate":
        return "crat-tool validate"
    if len(command) > 1 and command[1] == "replace":
        return "crat-tool replace"
    if "--inplace" in command:
        return "ordinary Crat prepare"
    return "project cargo build"


@pytest.mark.parametrize("failed_operation", TOOL_OPERATIONS)
def test_nonzero_build_preparation_or_crat_tool_exit_is_fatal(
    tmp_path, failed_operation
):
    crat_dir = tmp_path / "crat"
    (crat_dir / "deps_crate").mkdir(parents=True)
    events = []

    def runner(command, *, cwd=None, env=None):
        operation = _classify_tool_command(command, cwd)
        events.append(operation)
        if operation == "git revision":
            return CommandResult(0, "fixture-revision\n")
        if operation == failed_operation:
            return CommandResult(7, "partial stdout\n", "tool failed\n")
        if operation == "release cargo build --bin crat":
            binary = crat_dir / "target/release/crat"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("crat\n")
        elif operation == "release cargo build --bin crat-tool":
            binary = crat_dir / "target/release/crat-tool"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("crat-tool\n")
        elif operation.startswith("crat-tool "):
            output = Path(command[command.index("--output") + 1])
            contents = {
                "crat-tool make-skeleton": json.dumps(
                    [fn_record(0, "target", "target", [])]
                ),
                "crat-tool normalize-safety": "unsafe fn target() {}\n",
                "crat-tool validate": json.dumps(VALID),
                "crat-tool replace": "unsafe fn target() {}\n",
            }[operation]
            output.write_text(contents)
            if operation == "crat-tool replace":
                statement_pairs_output = Path(
                    command[command.index("--statement-pairs-output") + 1]
                )
                statement_pairs_output.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "statements": [
                                {
                                    "item_id": 0,
                                    "path": "target",
                                    "label": 0,
                                    "after_statement": "#[proctor(0)]\n()",
                                }
                            ],
                        }
                    )
                )
        return CommandResult(0)

    tools = CratTools(
        tmp_path / "tool.log",
        run_command=runner,
        environment_factory=lambda path: {},
    )
    value = stage_input(tmp_path, config={"crat_dir": str(crat_dir)})
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: FakeClient([response()]),
    )
    assert output.status == "failure"
    assert failed_operation in output.error
    assert "exit code 7" in output.error
    assert "partial stdout\n" in output.error
    assert "tool failed\n" in output.error
    assert events[-1] == failed_operation


@pytest.mark.parametrize(
    ("operation", "method_name", "arguments", "output_name"),
    [
        (
            "crat-tool make-skeleton",
            "make_skeleton",
            ("current",),
            "skeletons.json",
        ),
        (
            "crat-tool normalize-safety",
            "normalize",
            ("current/lib.rs",),
            "normalized.rs",
        ),
        (
            "crat-tool validate",
            "validate",
            ("validation-request.json",),
            "validation-response.json",
        ),
        (
            "crat-tool replace",
            "replace",
            ("current", "replacement-request.json"),
            "candidate.rs",
        ),
    ],
)
@pytest.mark.parametrize("created_kind", ["missing", "directory"])
def test_created_outputs_cannot_be_missing_nonregular_or_stale(
    tmp_path, operation, method_name, arguments, output_name, created_kind
):
    work = tmp_path / "work"
    work.mkdir()
    output = work / output_name
    output.write_text("stale\n")

    def runner(command, *, cwd=None, env=None):
        assert not output.exists()
        if created_kind == "directory":
            output.mkdir()
        if operation == "crat-tool replace":
            (work / "replacement-statement-pairs.json").write_text(
                '{"schema_version":1,"statements":[]}'
            )
        return CommandResult(0)

    tools = CratTools(
        tmp_path / "log",
        run_command=runner,
        environment_factory=lambda path: {},
    )
    tools.crat_tool = Path("/tools/crat-tool")
    method = getattr(tools, method_name)
    paths = [work / argument for argument in arguments]
    with pytest.raises(StageFailure, match=operation):
        if operation == "crat-tool replace":
            method(*paths, output, work / "replacement-statement-pairs.json")
        else:
            method(*paths, output)
    assert not output.is_file()


@pytest.mark.parametrize(
    ("config_present", "delete_library"),
    [(True, False), (False, False), (True, True)],
)
def test_preparation_and_initialization_event_order_is_exact(
    tmp_path, config_present, delete_library
):
    value = stage_input(tmp_path)
    source = value.inputs.rust_project
    source.joinpath("Cargo.toml").write_text(
        "[package]\n"
        'name = "p"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n\n'
        "[lib]\n"
        'name = "p"\n'
        'path = "lib.rs"\n\n'
        "[[bin]]\n"
        'name = "p-bin"\n'
        'path = "driver.rs"\n'
    )
    source.joinpath("lib.rs").write_text("pub struct S;\n")
    source.joinpath("driver.rs").write_text("fn main() {}\n")
    source.joinpath("proctor.toml").write_text(
        'wrappers = [{ wrapped = "a", wrapper = "b" }]\n'
    )
    if config_present:
        source.joinpath("config.toml").write_text("")

    class PreparationTools(FakeTools):
        def prepare(self, current, passes, use_print):
            super().prepare(current, passes, use_print)
            if delete_library:
                current.joinpath("lib.rs").unlink()

    tools = PreparationTools(normalized="pub struct S;\n")
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: FakeClient([]),
    )
    if delete_library:
        assert output.status == "failure"
        assert [event[0] for event in tools.events] == ["build_tools", "prepare"]
        assert "prepared Cargo library source is not a regular file" in output.error
        return

    assert output.status == "success"
    assert tools.events == [
        ("build_tools", Path((STAGE_DIR / "../crat").resolve())),
        ("prepare", value.framework.workdir / "current", ("expand", "unexpand"), True),
        (
            "make_skeleton",
            value.framework.workdir / "current",
            value.framework.workdir / "skeletons.json",
        ),
        (
            "normalize",
            value.framework.workdir / "current/lib.rs",
            value.framework.workdir / "normalized.rs",
        ),
        ("cargo_build", value.framework.workdir / "current", "pub struct S;\n"),
    ]
    assert output.metrics == {
        "function_count": 0,
        "scc_count": 0,
        "llm_generation_calls": 0,
        "repair_calls": 0,
        "structural_failures": 0,
        "compilation_failures": 0,
        "cargo_builds": 1,
    }
    assert output.usage is None
    assert output.models == ()
    assert output.prompts == ()
    assert value.outputs.rust_project.joinpath("driver.rs").read_text() == (
        "fn main() {}\n"
    )
    assert value.outputs.rust_project.joinpath("proctor.toml").read_text() == (
        'wrappers = [{ wrapped = "a", wrapper = "b" }]\n'
    )
    assert not (value.outputs.rust_project / "target").exists()

    command_current = tmp_path / "command-current"
    command_current.mkdir()
    if config_present:
        command_current.joinpath("config.toml").write_text("")
    commands = []

    def runner(command, *, cwd=None, env=None):
        commands.append(command)
        return CommandResult(0)

    command_tools = CratTools(
        tmp_path / "prepare.log",
        run_command=runner,
        environment_factory=lambda path: {},
    )
    command_tools.crat = Path("/tools/crat")
    command_tools.environment = {}
    command_tools.prepare(command_current, ("expand", "unexpand"), True)
    expected = ["/tools/crat", "--inplace"]
    if config_present:
        expected.extend(["--config", str(command_current / "config.toml")])
    expected.extend(
        [
            "--pass",
            "expand,unexpand",
            "--unexpand-use-print",
            str(command_current),
        ]
    )
    assert commands == [expected]


def test_normalized_initial_build_failure_aborts_without_llm(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(101, "out", "err")],
    )
    client = FakeClient([])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure" and not client.requests


def test_valid_initial_generation_validates_replaces_and_builds_once(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["candidate-one\n"],
    )
    value, output = run_fake(tmp_path, tools, FakeClient([response()]))
    assert output.status == "success"
    assert (value.outputs.rust_project / "lib.rs").read_text() == "candidate-one\n"
    assert output.metrics["cargo_builds"] == 2


def test_all_preserved_singleton_skips_llm_and_validator(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [], needs_transformation=False)],
        builds=[CommandResult(0), CommandResult(0)],
        candidates=["mechanical\n"],
    )
    client = FakeClient([])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    assert client.requests == []
    assert not [event for event in tools.events if event[0] == "validate"]
    replacement = next(event for event in tools.events if event[0] == "replace")
    assert replacement[2]["transformation"].endswith("#[proctor(0)]\n    ()\n}")
    assert (value.outputs.rust_project / "lib.rs").read_text() == "mechanical\n"
    assert output.metrics["llm_generation_calls"] == 0
    assert output.metrics["cargo_builds"] == 2


def test_entirely_mechanical_run_has_zero_llm_calls(tmp_path):
    tools = FakeTools(
        skeletons=[
            fn_record(0, "first", "first", [], needs_transformation=False),
            fn_record(1, "second", "second", [0], needs_transformation=False),
        ],
        builds=[CommandResult(0), CommandResult(0), CommandResult(0)],
        candidates=["first-candidate\n", "second-candidate\n"],
    )
    client = FakeClient([])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    assert client.requests == []
    assert len([event for event in tools.events if event[0] == "replace"]) == 2
    assert output.metrics == {
        "function_count": 2,
        "scc_count": 2,
        "llm_generation_calls": 0,
        "repair_calls": 0,
        "structural_failures": 0,
        "compilation_failures": 0,
        "cargo_builds": 3,
    }


def test_mixed_scc_still_uses_one_llm_request(tmp_path):
    tools = FakeTools(
        skeletons=[
            fn_record(
                0,
                "preserved",
                "preserved",
                [1],
                needs_transformation=False,
            ),
            fn_record(1, "changed", "changed", [0]),
        ],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=[
            "unsafe fn preserved() { () }\nunsafe fn changed() { transformed(); }\n"
        ],
    )
    client = FakeClient(
        [
            "```rust\n"
            "unsafe fn preserved() { #[proctor(0)] 999 }\n"
            "unsafe fn changed() { #[proctor(0)] () }\n"
            "```"
        ]
    )
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    assert len(client.requests) == 1
    replacement = next(event for event in tools.events if event[0] == "replace")
    items = replacement[2]["items"]
    assert [item["needs_transformation"] for item in items] == [False, True]
    emitted = (value.outputs.rust_project / "lib.rs").read_text()
    assert "unsafe fn preserved() { () }" in emitted
    assert "999" not in emitted
    assert "transformed()" in emitted
    assert output.metrics["llm_generation_calls"] == 1


def test_mechanical_and_llm_sccs_share_deterministic_schedule(tmp_path):
    tools = FakeTools(
        skeletons=[
            fn_record(
                0,
                "scalar_leaf",
                "scalar_leaf",
                [],
                needs_transformation=False,
            ),
            fn_record(1, "pointer_leaf", "pointer_leaf", []),
            fn_record(2, "root", "root", [0, 1]),
        ],
        builds=[
            CommandResult(0),
            CommandResult(0),
            CommandResult(0),
            CommandResult(0),
        ],
        validators=[VALID, VALID],
        candidates=["scalar\n", "pointer\n", "root\n"],
    )
    client = FakeClient([response("pointer_leaf"), response("root")])
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    replacements = [
        event[2]["items"][0]["name"] for event in tools.events if event[0] == "replace"
    ]
    assert replacements == ["scalar_leaf", "pointer_leaf", "root"]
    assert len(client.requests) == 2
    assert "pointer_leaf" in client.requests[0].messages[0].content
    assert "root" in client.requests[1].messages[0].content
    assert output.metrics["cargo_builds"] == 4


def test_mechanical_signature_change_runs_replacer_and_build(tmp_path):
    record = fn_record(
        0,
        "unused_pointer",
        "unused_pointer",
        [],
        needs_transformation=False,
    )
    record["annotated_skeleton"] = (
        "unsafe fn unused_pointer(pointer: &mut i32, value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    value * 2\n"
        "}"
    )
    record["target_signature"] = (
        "unsafe fn unused_pointer(pointer: &mut i32, value: i32) -> i32"
    )
    tools = FakeTools(
        skeletons=[record],
        builds=[CommandResult(0), CommandResult(0)],
        candidates=["implementation\ncompatibility-wrapper\n"],
    )
    client = FakeClient([])
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    replacement = next(event for event in tools.events if event[0] == "replace")
    assert replacement[2]["transformation"] == record["annotated_skeleton"]
    assert "&mut i32" in replacement[2]["items"][0]["skeleton"]
    assert client.requests == []
    assert output.metrics["cargo_builds"] == 2


def test_mechanical_build_failure_is_fatal_without_repair(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [], needs_transformation=False)],
        builds=[CommandResult(0), CommandResult(101, "out", "bad")],
        candidates=["broken\n"],
    )
    client = FakeClient([])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure"
    assert "mechanical SCC candidate cargo build failed" in output.error
    assert client.requests == []
    assert output.metrics["repair_calls"] == 0
    assert output.metrics["compilation_failures"] == 1
    assert (value.framework.workdir / "current/lib.rs").read_text() == "normalized\n"
    assert not value.outputs.rust_project.exists()


def test_mechanical_replacer_failure_is_fatal_without_repair(tmp_path):
    class BrokenMechanicalReplacer(FakeTools):
        def replace(
            self, current, request, candidate, statement_pairs_output, *outputs
        ):
            raise StageFailure("mechanical replacement rejected")

    tools = BrokenMechanicalReplacer(
        skeletons=[fn_record(0, "target", "target", [], needs_transformation=False)],
        builds=[CommandResult(0)],
    )
    client = FakeClient([])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure"
    assert "mechanical replacement rejected" in output.error
    assert client.requests == []
    assert output.metrics["repair_calls"] == 0
    assert output.metrics["cargo_builds"] == 1
    assert (value.framework.workdir / "current/lib.rs").read_text() == "normalized\n"
    assert not value.outputs.rust_project.exists()


def test_validator_invalid_consumes_one_repair_then_succeeds(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[INVALID, VALID],
        candidates=["candidate\n"],
    )
    client = FakeClient([response(), response()])
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    assert output.metrics["repair_calls"] == 1
    assert json.dumps(INVALID) in client.requests[1].messages[0].content


def test_missing_fence_consumes_repair_without_validator_call(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["candidate\n"],
    )
    _, output = run_fake(tmp_path, tools, FakeClient(["no code here", response()]))
    assert output.status == "success"
    assert len([event for event in tools.events if event[0] == "validate"]) == 1


def test_failed_candidate_build_restores_then_repairs(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[
            CommandResult(0),
            CommandResult(101, "checking\n", "bad\n"),
            CommandResult(0),
        ],
        validators=[VALID, VALID],
        candidates=["bad\n", "good\n"],
    )
    value, output = run_fake(tmp_path, tools, FakeClient([response(), response()]))
    assert output.status == "success"
    replace_events = [event for event in tools.events if event[0] == "replace"]
    assert replace_events[1][3] == "normalized\n"
    assert (value.outputs.rust_project / "lib.rs").read_text() == "good\n"


def test_ten_failed_repairs_allow_exactly_eleven_generations(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID] * 11,
    )
    _, output = run_fake(tmp_path, tools, FakeClient([response()] * 12))
    assert output.status == "failure"
    assert output.metrics["llm_generation_calls"] == 11
    assert output.metrics["repair_calls"] == 10


@pytest.mark.parametrize(
    ("outcome", "diagnostic"),
    [
        (
            {
                "schema_version": 1,
                "status": "setup_error",
                "error": {"code": "duplicate_expected_name", "message": "duplicate"},
            },
            "validator setup_error",
        ),
        ("process_failure", "validator crashed"),
        ({"schema_version": 1, "status": "mystery"}, "unknown validator"),
        ({"schema_version": 2, "status": "valid"}, "unsupported validator"),
        ("malformed_json", "malformed JSON"),
        (
            {"schema_version": 1, "status": "invalid"},
            "invalid response must contain exactly",
        ),
    ],
)
def test_validator_setup_or_protocol_failure_aborts_without_repair(
    tmp_path, outcome, diagnostic
):
    class ProtocolTools(FakeTools):
        def validate(self, request, response_path):
            self.events.append(("validate", json.loads(request.read_text())))
            if outcome == "process_failure":
                raise StageFailure(
                    "crat-tool validate failed with exit code 2\n"
                    "stdout:\n\nstderr:\nvalidator crashed\n"
                )
            if outcome == "malformed_json":
                raise StageFailure(
                    "validator response is malformed JSON: "
                    "Expecting property name enclosed in double quotes"
                )
            raw = json.dumps(outcome)
            response_path.write_text(raw)
            return raw, outcome

    tools = ProtocolTools(skeletons=[fn_record(0, "target", "target", [])])
    client = FakeClient([response()])
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure"
    assert diagnostic in output.error
    assert output.metrics["llm_generation_calls"] == 1
    assert output.metrics["repair_calls"] == 0
    assert len(client.requests) == 1
    assert len([event for event in tools.events if event[0] == "cargo_build"]) == 1
    assert not [event for event in tools.events if event[0] == "replace"]


def test_replacement_failure_is_not_sent_to_llm(tmp_path):
    class Broken(FakeTools):
        def replace(
            self, current, request, candidate, statement_pairs_output, *outputs
        ):
            raise StageFailure("TargetResolution: missing target")

    tools = Broken(skeletons=[fn_record(0, "target", "target", [])], validators=[VALID])
    client = FakeClient([response()])
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure" and len(client.requests) == 1


def test_context_overflow_is_forced_to_error_and_aborts(tmp_path):
    seen = {}
    tools = FakeTools(skeletons=[fn_record(0, "target", "target", [])])
    llm = {
        "provider": "replay",
        "model": "fixture-model",
        "context_overflow": "truncate_middle",
        "max_retries": 5,
    }
    value = stage_input(
        tmp_path,
        llm=llm,
    )

    class Provider:
        name = "replay"

        def complete(self, request):
            raise ContextLimitExceeded(
                "too long", needed_tokens=12000, limit_tokens=10000
            )

    def factory(settings, tracker):
        seen.update(settings)
        return LlmClient(settings, tracker=tracker, provider=Provider())

    output = run_stage(
        value, stage_dir=STAGE_DIR, tools=tools, llm_client_factory=factory
    )
    assert output.status == "failure"
    assert seen["context_overflow"] == "error"
    assert value.framework.llm == llm
    assert output.models[0].provider == "replay"
    assert output.models[0].model == "fixture-model"
    assert output.prompts[0].id == "local_transformation"
    assert output.prompts[0].version == 1
    assert output.usage.calls == 1
    assert output.usage.input_tokens == 0
    assert output.usage.cached_input_tokens == 0
    assert output.usage.output_tokens == 0
    assert output.usage.reasoning_tokens is None
    assert "ContextLimitExceeded: too long" in output.error
    assert output.metrics["repair_calls"] == 0
    assert output.metrics["structural_failures"] == 0
    assert output.metrics["compilation_failures"] == 0
    assert not [event for event in tools.events if event[0] in {"validate", "replace"}]
    usage = read_usage(
        value.framework.usage_log or value.framework.workdir / "usage.jsonl"
    )
    assert usage[0]["provider"] == "replay"
    assert usage[0]["model"] == "fixture-model"
    assert usage[0]["error"] == "ContextLimitExceeded: too long"


def test_later_scc_uses_promoted_source_but_immutable_skeleton_prompt(tmp_path):
    tools = FakeTools(
        skeletons=[
            fn_record(0, "callee", "callee", []),
            fn_record(1, "caller", "caller", [0]),
        ],
        builds=[CommandResult(0), CommandResult(0), CommandResult(0)],
        validators=[VALID, VALID],
        candidates=["after-callee\n", "after-caller\n"],
    )
    _, output = run_fake(
        tmp_path, tools, FakeClient([response("callee"), response("caller")])
    )
    assert output.status == "success"
    assert [event[3] for event in tools.events if event[0] == "replace"] == [
        "normalized\n",
        "after-callee\n",
    ]


def test_successful_candidate_keeps_source_and_deletes_rollback(tmp_path):
    library = tmp_path / "lib.rs"
    candidate = tmp_path / "candidate.rs"
    library.write_text("old\n")
    candidate.write_text("new\n")
    result = install_candidate_transaction(
        library, candidate, tmp_path / "rollback", lambda: CommandResult(0)
    )
    assert result.returncode == 0 and library.read_text() == "new\n"
    assert not candidate.exists() and not list((tmp_path / "rollback").iterdir())


def test_failed_build_restores_source_but_retains_target_updates(tmp_path):
    library = tmp_path / "lib.rs"
    candidate = tmp_path / "candidate.rs"
    target = tmp_path / "target"
    target.mkdir()
    cache = target / "cache"
    library.write_text("old\n")
    candidate.write_text("bad\n")

    def build():
        cache.write_text("after\n")
        return CommandResult(101)

    install_candidate_transaction(library, candidate, tmp_path / "rollback", build)
    assert library.read_text() == "old\n" and cache.read_text() == "after\n"


def test_exception_after_installation_restores_in_finally(tmp_path):
    library = tmp_path / "lib.rs"
    candidate = tmp_path / "candidate.rs"
    library.write_text("old\n")
    candidate.write_text("bad\n")
    with pytest.raises(RuntimeError):
        install_candidate_transaction(
            library,
            candidate,
            tmp_path / "rollback",
            lambda: (_ for _ in ()).throw(RuntimeError("transport")),
        )
    assert library.read_text() == "old\n"


def test_rollback_failure_is_fatal_and_not_repairable(tmp_path):
    library = tmp_path / "lib.rs"
    candidate = tmp_path / "candidate.rs"
    library.write_text("old\n")
    candidate.write_text("bad\n")
    calls = 0

    def replacing(source, destination):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("restore denied")
        os.replace(source, destination)

    with pytest.raises(StageFailure, match="restore denied"):
        install_candidate_transaction(
            library,
            candidate,
            tmp_path / "rollback",
            lambda: CommandResult(101),
            atomic_replace=replacing,
        )


def test_input_is_copied_once_and_never_mutated(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["final\n"],
    )
    value, output = run_fake(tmp_path, tools, FakeClient([response()]))
    assert output.status == "success"
    assert (value.inputs.rust_project / "lib.rs").read_text() == "old\n"
    assert (value.inputs.rust_project / "target/cache").read_text() == "warm\n"


def test_final_output_excludes_only_root_target(tmp_path):
    tools = FakeTools()
    value = stage_input(tmp_path)
    assets = value.inputs.rust_project / "assets"
    assets.mkdir()
    (assets / "target").write_text("ordinary\n")
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda s, t: FakeClient([]),
    )
    assert output.status == "success"
    assert not (value.outputs.rust_project / "target").exists()
    assert (value.outputs.rust_project / "assets/target").read_text() == "ordinary\n"


def test_failure_creates_no_claimed_output(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID] * 11,
    )
    value, output = run_fake(tmp_path, tools, FakeClient([response()] * 11))
    assert output.status == "failure"
    assert not value.outputs.rust_project.exists()
    assert output.outputs.rust_project is None


@pytest.mark.parametrize("destination_kind", ["directory", "dangling_symlink"])
def test_existing_output_is_refused_before_work_or_tool_calls(
    tmp_path, destination_kind
):
    value = stage_input(tmp_path)
    if destination_kind == "directory":
        value.outputs.rust_project.mkdir()
        (value.outputs.rust_project / "existing.txt").write_text("owned by caller\n")
    else:
        value.outputs.rust_project.symlink_to(tmp_path / "missing-target")
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "destination already exists" in output.error
    assert not tools.events
    if destination_kind == "directory":
        assert value.outputs.rust_project.joinpath("existing.txt").read_text() == (
            "owned by caller\n"
        )
    else:
        assert value.outputs.rust_project.is_symlink()


def test_stage_manifest_declares_exact_artifacts_and_warmup():
    import tomllib

    manifest = tomllib.loads((STAGE_DIR / "stage.toml").read_text())
    assert manifest["id"] == "local_transformation"
    assert manifest["version"] == "0.1.0"
    assert manifest["exec"] == ["python3", "main.py"]
    assert manifest["warmup"] == ["python3", "main.py", "--build-only"]
    assert manifest["requires"] == {"rust_project": "required"}
    assert manifest["produces"] == {"rust_project": True}
    assert set(manifest["config"]) == {"crat_dir", "dump_llm_exchanges"}
    assert manifest["config"]["crat_dir"]["default"] == "../crat"
    assert manifest["config"]["dump_llm_exchanges"]["default"] is False
    assert not (
        {"c_project", "test_package", "rule_set"}
        & (set(manifest["requires"]) | set(manifest["produces"]))
    )
    project = tomllib.loads((STAGE_DIR / "pyproject.toml").read_text())
    assert "proctor" in project["project"]["dependencies"]
    assert project["tool"]["uv"]["sources"]["proctor"]["path"] == "../.."
    assert (STAGE_DIR / "uv.lock").is_file()


@pytest.mark.parametrize(
    "case",
    ["artifacts", "no_artifacts", "default_usage_log", "missing_pricing"],
)
def test_successful_output_reports_llm_reproducibility(tmp_path, case):
    from dataclasses import replace

    artifacts = case != "no_artifacts"
    llm = {
        "provider": "replay",
        "model": "fixture-model",
        "pricing": {
            "replay/fixture-model": {
                "input": 0,
                "cached_input": 0,
                "output": 0,
            }
        },
    }
    if case == "missing_pricing":
        llm.pop("pricing")
    value = stage_input(tmp_path, artifacts=artifacts, llm=llm)
    explicit_usage = tmp_path / "usage" / "calls.jsonl"
    if case != "default_usage_log":
        value = replace(
            value,
            framework=replace(value.framework, usage_log=explicit_usage),
        )
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["final\n"],
    )
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: FakeClient([response()]),
    )
    assert output.status == "success"
    assert output.outputs.rust_project == value.outputs.rust_project
    assert output.outputs.rule_set is None
    assert [(model.provider, model.model) for model in output.models] == [
        ("replay", "fixture-model")
    ]
    assert output.usage.calls == 1
    assert output.usage.input_tokens == 100
    assert output.usage.cached_input_tokens == 20
    assert output.usage.output_tokens == 30
    assert output.usage.reasoning_tokens == 4
    assert output.usage.cost_usd == (None if case == "missing_pricing" else 0.0)
    assert [(prompt.id, prompt.version) for prompt in output.prompts] == [
        ("local_transformation", 1)
    ]
    assert output.config_used == {
        "crat_dir": str((STAGE_DIR / "../crat").resolve()),
        "dump_llm_exchanges": False,
    }
    assert output.metrics == {
        "function_count": 1,
        "scc_count": 1,
        "llm_generation_calls": 1,
        "repair_calls": 0,
        "structural_failures": 0,
        "compilation_failures": 0,
        "cargo_builds": 2,
    }
    assert output.error is None
    if artifacts:
        assert output.logs == ("local-transformation.log",)
        assert value.outputs.artifacts_dir.joinpath(output.logs[0]).is_file()
        assert all(not Path(log).is_absolute() for log in output.logs)
        assert not value.outputs.artifacts_dir.joinpath("llm-exchanges").exists()
    else:
        assert output.logs == ()
        assert value.framework.workdir.joinpath("local-transformation.log").is_file()
        assert not value.framework.workdir.joinpath("llm-exchanges").exists()
    usage_path = (
        value.framework.workdir / "usage.jsonl"
        if case == "default_usage_log"
        else explicit_usage
    )
    assert usage_path.is_file()


@pytest.mark.parametrize("artifacts", [True, False])
def test_dump_llm_exchanges_preserves_each_generation(tmp_path, artifacts):
    first_response = "first response\n" + response()
    second_response = "second response\n" + response()
    client = FakeClient([first_response, second_response])
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[INVALID, VALID],
        candidates=["final\n"],
    )
    value, output = run_fake(
        tmp_path,
        tools,
        client,
        artifacts=artifacts,
        config={"dump_llm_exchanges": True},
    )
    assert output.status == "success"
    root = (
        value.outputs.artifacts_dir
        if value.outputs.artifacts_dir is not None
        else value.framework.workdir
    )
    exchange_root = root / "llm-exchanges" / "scc-0"
    for generation, raw_response in enumerate([first_response, second_response]):
        generation_dir = exchange_root / f"generation-{generation:02d}"
        assert generation_dir.joinpath("prompt.md").read_text(encoding="utf-8") == (
            client.requests[generation].messages[0].content
        )
        assert (
            generation_dir.joinpath("response.md").read_text(encoding="utf-8")
            == raw_response
        )
    assert (
        "The previous transformation failed."
        not in client.requests[0].messages[0].content
    )
    assert (
        "The previous transformation failed." in client.requests[1].messages[0].content
    )


def test_failure_after_llm_still_reports_accumulated_usage(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[
            {
                "schema_version": 1,
                "status": "setup_error",
                "error": {"code": "duplicate_expected_name", "message": "duplicate"},
            }
        ],
    )
    _, output = run_fake(tmp_path, tools, FakeClient([response()]))
    assert output.status == "failure" and output.usage.calls == 1


def test_provider_retry_success_counts_attempts_but_one_generation(tmp_path):
    class Provider:
        name = "replay"

        def __init__(self):
            self.values = [
                ProviderError("temporary failure", status=503),
                Response(
                    response(),
                    "stop",
                    "replay",
                    "fixture-model",
                    0.25,
                    Usage(100, 20, 30, 4),
                ),
            ]

        def complete(self, request):
            value = self.values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["final\n"],
    )
    value = stage_input(tmp_path)
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: LlmClient(
            {**settings, "max_retries": 1},
            tracker=tracker,
            provider=Provider(),
            sleep=lambda _: None,
        ),
    )
    assert output.status == "success"
    assert output.usage.calls == 2
    assert output.metrics["llm_generation_calls"] == 1


@pytest.mark.parametrize(
    "values,calls",
    [
        (
            [
                ProviderError("upstream unavailable", status=503),
                ProviderError("still unavailable", status=503),
            ],
            2,
        ),
        ([AuthError("bad key")], 1),
    ],
)
def test_terminal_llm_errors_preserve_every_failed_attempt(tmp_path, values, calls):
    class Provider:
        name = "replay"

        def complete(self, request):
            raise values.pop(0)

    tools = FakeTools(skeletons=[fn_record(0, "target", "target", [])])
    value = stage_input(tmp_path)
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=lambda settings, tracker: LlmClient(
            {**settings, "max_retries": 1},
            tracker=tracker,
            provider=Provider(),
            sleep=lambda _: None,
        ),
    )
    assert output.status == "failure"
    assert output.usage.calls == calls
    assert output.metrics["llm_generation_calls"] == 1


@pytest.mark.parametrize(
    "failed_target",
    [
        None,
        "deps_crate cargo build",
        "release cargo build --bin crat",
        "release cargo build --bin crat-tool",
    ],
)
def test_build_only_warms_both_crat_binaries_without_stage_io(
    tmp_path, monkeypatch, capsys, failed_target
):
    import main as stage_main

    stage_dir = tmp_path / "local-transformation"
    crat_dir = tmp_path / "crat"
    stage_dir.mkdir()
    (crat_dir / "deps_crate").mkdir(parents=True)
    monkeypatch.setattr(stage_main, "__file__", str(stage_dir / "main.py"))
    cargo_calls = []

    def runner(command, *, cwd=None, env=None):
        operation = _classify_tool_command(command, cwd)
        if operation == "git revision":
            return CommandResult(0, "fixture-revision\n")
        cargo_calls.append((command, cwd))
        if operation == failed_target:
            return CommandResult(7, "partial stdout\n", "tool failed\n")
        if operation == "release cargo build --bin crat":
            binary = crat_dir / "target/release/crat"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("crat\n")
        elif operation == "release cargo build --bin crat-tool":
            (crat_dir / "target/release/crat-tool").write_text("crat-tool\n")
        return CommandResult(0)

    tools = CratTools(
        stage_dir / "build.log",
        run_command=runner,
        environment_factory=lambda path: {},
    )
    status = stage_main.main(["--build-only"], tools=tools)
    captured = capsys.readouterr()
    expected = [
        (["cargo", "build"], crat_dir / "deps_crate"),
        (
            ["cargo", "build", "--release", "--bin", "crat"],
            crat_dir,
        ),
        (
            ["cargo", "build", "--release", "--bin", "crat-tool"],
            crat_dir,
        ),
    ]
    if failed_target is None:
        assert status == 0
        assert cargo_calls == expected
        assert captured.out.splitlines() == [
            str(crat_dir / "target/release/crat"),
            str(crat_dir / "target/release/crat-tool"),
        ]
    else:
        failed_index = TOOL_OPERATIONS.index(failed_target)
        assert status == 1
        assert cargo_calls == expected[: failed_index + 1]
        assert failed_target in captured.err
        assert "exit code 7" in captured.err
    assert not (stage_dir / "current").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_input",
        "missing_output",
        "missing_work",
        "missing_manifest",
        "missing_lib",
        "nonstring_lib",
        "nested_lib",
        "parent_lib",
        "escaping_lib",
        "absolute_lib",
        "empty_crat",
        "wrong_crat",
        "wrong_dump",
        "unknown_config",
        "output_inside_input",
        "work_equals_input",
        "artifacts_inside_input",
        "usage_inside_input",
        "crat_inside_input",
    ],
)
def test_missing_required_paths_fail_before_side_effects(tmp_path, mutation):
    from dataclasses import replace

    value = stage_input(tmp_path)
    if mutation == "missing_input":
        value = replace(value, inputs=replace(value.inputs, rust_project=None))
    elif mutation == "missing_output":
        value = replace(value, outputs=replace(value.outputs, rust_project=None))
    elif mutation == "missing_work":
        value = replace(value, framework=replace(value.framework, workdir=None))
    elif mutation == "missing_manifest":
        (value.inputs.rust_project / "Cargo.toml").unlink()
    elif mutation == "missing_lib":
        (value.inputs.rust_project / "Cargo.toml").write_text("[package]\nname='p'\n")
    elif mutation == "nonstring_lib":
        (value.inputs.rust_project / "Cargo.toml").write_text("[lib]\npath=7\n")
    elif mutation == "nested_lib":
        (value.inputs.rust_project / "src").mkdir()
        (value.inputs.rust_project / "src/lib.rs").write_text("nested\n")
        (value.inputs.rust_project / "Cargo.toml").write_text(
            '[lib]\npath="src/lib.rs"\n'
        )
    elif mutation == "parent_lib":
        (value.inputs.rust_project / "src").mkdir()
        (value.inputs.rust_project / "Cargo.toml").write_text(
            '[lib]\npath="src/../lib.rs"\n'
        )
    elif mutation == "escaping_lib":
        (value.inputs.rust_project / "Cargo.toml").write_text(
            '[lib]\npath="../lib.rs"\n'
        )
    elif mutation == "absolute_lib":
        (value.inputs.rust_project / "Cargo.toml").write_text(
            '[lib]\npath="/outside/lib.rs"\n'
        )
    elif mutation == "empty_crat":
        value = replace(value, config={"crat_dir": ""})
    elif mutation == "wrong_crat":
        value = replace(value, config={"crat_dir": 7})
    elif mutation == "wrong_dump":
        value = replace(value, config={"dump_llm_exchanges": "yes"})
    elif mutation == "unknown_config":
        value = replace(value, config={"unknown": True})
    elif mutation == "output_inside_input":
        value = replace(
            value,
            outputs=replace(
                value.outputs,
                rust_project=value.inputs.rust_project / "generated-output",
            ),
        )
    elif mutation == "work_equals_input":
        value = replace(
            value,
            framework=replace(value.framework, workdir=value.inputs.rust_project),
        )
    elif mutation == "artifacts_inside_input":
        value = replace(
            value,
            outputs=replace(
                value.outputs,
                artifacts_dir=value.inputs.rust_project / "artifacts",
            ),
        )
    elif mutation == "usage_inside_input":
        value = replace(
            value,
            framework=replace(
                value.framework,
                usage_log=value.inputs.rust_project / "usage.jsonl",
            ),
        )
    elif mutation == "crat_inside_input":
        value = replace(
            value,
            config={"crat_dir": str(value.inputs.rust_project / "crat")},
        )
    input_path = value.inputs.rust_project

    def input_files():
        if input_path is None:
            return {}
        return {
            path.relative_to(input_path): (
                path.read_bytes() if path.is_file() else None
            )
            for path in input_path.rglob("*")
        }

    original_files = input_files()
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure" and not tools.events
    assert original_files == input_files()


@pytest.mark.parametrize("relation", ["equal", "ancestor", "descendant"])
def test_artifact_destination_must_not_overlap_current_workspace(tmp_path, relation):
    value = stage_input(tmp_path)
    current = value.framework.workdir / "current"
    artifacts = {
        "equal": current,
        "ancestor": value.framework.workdir,
        "descendant": current / "artifacts",
    }[relation]
    value = replace(
        value,
        outputs=replace(value.outputs, artifacts_dir=artifacts),
    )
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "overlaps working Rust project" in output.error
    assert not tools.events


def _pointer_metadata(label=0, *, complete=True, variables=None, before=None):
    return {
        "label": label,
        "before_statement": before or f"#[proctor({label})]\n*pointer += 1;",
        "pointer_variables_complete": complete,
        "pointer_variables": (
            [
                {
                    "name": "pointer",
                    "origin": {"kind": "parameter", "index": 0},
                    "before_type": "*mut i32",
                    "selected_target_type": "&mut i32",
                    "before_type_is_inferred": False,
                }
            ]
            if variables is None
            else variables
        ),
    }


def test_skeleton_loader_requires_exact_matching_statement_metadata():
    record = fn_record(
        7,
        "module::function",
        "function",
        [],
        transformation_labels=[2],
        statement_pair_metadata=[
            _pointer_metadata(
                2,
                variables=[
                    {
                        "name": "pointer",
                        "origin": {"kind": "parameter", "index": 0},
                        "before_type": "Option<\n    *mut i32,\n>",
                        "selected_target_type": "Option<&mut i32>",
                        "before_type_is_inferred": False,
                    },
                    {
                        "name": "alias",
                        "origin": {"kind": "local", "declaration_label": 1},
                        "before_type": "*mut i32",
                        "selected_target_type": "&mut i32",
                        "before_type_is_inferred": True,
                    },
                ],
            )
        ],
    )
    parsed = loaded([record])[0]
    assert parsed.statement_pair_metadata[0].pointer_variables_complete is True
    assert parsed.statement_pair_metadata[0].pointer_variables[0].before_type == (
        "Option<\n    *mut i32,\n>"
    )
    assert [
        row.name for row in parsed.statement_pair_metadata[0].pointer_variables
    ] == [
        "pointer",
        "alias",
    ]

    malformed_records = []
    missing = copy.deepcopy(record)
    del missing["statement_pair_metadata"]
    malformed_records.append(missing)
    unknown = copy.deepcopy(record)
    unknown["statement_pair_metadata"][0]["unknown"] = 1
    malformed_records.append(unknown)
    wrong_labels = copy.deepcopy(record)
    wrong_labels["statement_pair_metadata"][0]["label"] = 3
    malformed_records.append(wrong_labels)
    newline = copy.deepcopy(record)
    newline["statement_pair_metadata"][0]["before_statement"] += "\n"
    malformed_records.append(newline)
    non_boolean = copy.deepcopy(record)
    non_boolean["statement_pair_metadata"][0]["pointer_variables_complete"] = 1
    malformed_records.append(non_boolean)
    bad_name = copy.deepcopy(record)
    bad_name["statement_pair_metadata"][0]["pointer_variables"][0]["name"] = "a\nb"
    malformed_records.append(bad_name)
    bad_origin = copy.deepcopy(record)
    bad_origin["statement_pair_metadata"][0]["pointer_variables"][0]["origin"] = {
        "kind": "parameter",
        "declaration_label": 0,
    }
    malformed_records.append(bad_origin)
    duplicate_origin = copy.deepcopy(record)
    duplicate_origin["statement_pair_metadata"][0]["pointer_variables"][1]["origin"] = {
        "kind": "parameter",
        "index": 0,
    }
    malformed_records.append(duplicate_origin)
    empty_type = copy.deepcopy(record)
    empty_type["statement_pair_metadata"][0]["pointer_variables"][0][
        "selected_target_type"
    ] = ""
    malformed_records.append(empty_type)
    inferred_integer = copy.deepcopy(record)
    inferred_integer["statement_pair_metadata"][0]["pointer_variables"][0][
        "before_type_is_inferred"
    ] = 0
    malformed_records.append(inferred_integer)
    for bad_metadata in (None, {}, "metadata"):
        malformed = copy.deepcopy(record)
        malformed["statement_pair_metadata"] = bad_metadata
        malformed_records.append(malformed)
    nonobject_statement = copy.deepcopy(record)
    nonobject_statement["statement_pair_metadata"][0] = []
    malformed_records.append(nonobject_statement)
    for bad_label in (True, -1, 2**32):
        malformed = copy.deepcopy(record)
        malformed["statement_pair_metadata"][0]["label"] = bad_label
        malformed_records.append(malformed)
    empty_before = copy.deepcopy(record)
    empty_before["statement_pair_metadata"][0]["before_statement"] = ""
    malformed_records.append(empty_before)
    wrong_before_type = copy.deepcopy(record)
    wrong_before_type["statement_pair_metadata"][0]["before_statement"] = 7
    malformed_records.append(wrong_before_type)
    carriage_return = copy.deepcopy(record)
    carriage_return["statement_pair_metadata"][0]["before_statement"] += "\r"
    malformed_records.append(carriage_return)
    wrong_variables = copy.deepcopy(record)
    wrong_variables["statement_pair_metadata"][0]["pointer_variables"] = {}
    malformed_records.append(wrong_variables)
    nonobject_variable = copy.deepcopy(record)
    nonobject_variable["statement_pair_metadata"][0]["pointer_variables"][0] = "row"
    malformed_records.append(nonobject_variable)
    missing_variable_key = copy.deepcopy(record)
    del missing_variable_key["statement_pair_metadata"][0]["pointer_variables"][0][
        "before_type"
    ]
    malformed_records.append(missing_variable_key)
    unknown_variable_key = copy.deepcopy(record)
    unknown_variable_key["statement_pair_metadata"][0]["pointer_variables"][0][
        "unknown"
    ] = 1
    malformed_records.append(unknown_variable_key)
    for field in ("name", "before_type", "selected_target_type"):
        malformed = copy.deepcopy(record)
        malformed["statement_pair_metadata"][0]["pointer_variables"][0][field] = ""
        malformed_records.append(malformed)
        malformed = copy.deepcopy(record)
        malformed["statement_pair_metadata"][0]["pointer_variables"][0][field] = 7
        malformed_records.append(malformed)
    carriage_name = copy.deepcopy(record)
    carriage_name["statement_pair_metadata"][0]["pointer_variables"][0]["name"] = (
        "pointer\rname"
    )
    malformed_records.append(carriage_name)
    for bad_origin in (
        None,
        {"kind": "unknown", "index": 0},
        {"kind": "parameter", "index": True},
        {"kind": "parameter", "index": 2**32},
        {"kind": "parameter", "index": 0, "extra": 1},
        {"kind": "local", "declaration_label": -1},
        {"kind": "local", "index": 0},
    ):
        malformed = copy.deepcopy(record)
        malformed["statement_pair_metadata"][0]["pointer_variables"][0]["origin"] = (
            bad_origin
        )
        malformed_records.append(malformed)
    for malformed in malformed_records:
        with pytest.raises(SkeletonError):
            loaded([malformed])

    ordered = fn_record(
        8,
        "ordered",
        "ordered",
        [],
        transformation_labels=[2, 4],
        statement_pair_metadata=[
            _pointer_metadata(2, complete=False),
            _pointer_metadata(4),
        ],
    )
    parsed_ordered = loaded([ordered])[0]
    assert [entry.label for entry in parsed_ordered.statement_pair_metadata] == [2, 4]
    assert parsed_ordered.statement_pair_metadata[0].pointer_variables_complete is False
    for labels in ([4], [2, 3, 4], [4, 2]):
        malformed = copy.deepcopy(ordered)
        by_label = {
            entry["label"]: entry for entry in malformed["statement_pair_metadata"]
        }
        malformed["statement_pair_metadata"] = [
            by_label.get(label, _pointer_metadata(label)) for label in labels
        ]
        with pytest.raises(SkeletonError):
            loaded([malformed])


def test_replacement_sidecar_loader_is_strict_and_cross_checks_the_scc(tmp_path):
    record = fn_record(
        7,
        "module::function",
        "function",
        [],
        transformation_labels=[2, 4],
        statement_pair_metadata=[_pointer_metadata(2), _pointer_metadata(4)],
    )
    records = {7: loaded([record])[0]}
    path = tmp_path / "pairs.json"
    valid = {
        "schema_version": 1,
        "statements": [
            {
                "item_id": 7,
                "path": "module::function",
                "label": 2,
                "after_statement": "#[proctor(2)]\nfirst();",
            },
            {
                "item_id": 7,
                "path": "module::function",
                "label": 4,
                "after_statement": "#[proctor(4)]\nsecond();",
            },
        ],
    }
    path.write_text(json.dumps(valid))
    assert [
        pair.label for pair in _load_replacement_statement_pairs(path, (7,), records)
    ] == [2, 4]

    malformed_values = []
    for field, value in (
        ("schema_version", 2),
        ("schema_version", True),
        ("statements", {}),
    ):
        malformed = copy.deepcopy(valid)
        malformed[field] = value
        malformed_values.append(malformed)
    unknown = copy.deepcopy(valid)
    unknown["unknown"] = 0
    malformed_values.append(unknown)
    missing_top = copy.deepcopy(valid)
    del missing_top["statements"]
    malformed_values.append(missing_top)
    unknown_statement = copy.deepcopy(valid)
    unknown_statement["statements"][0]["unknown"] = 0
    malformed_values.append(unknown_statement)
    missing_statement_key = copy.deepcopy(valid)
    del missing_statement_key["statements"][0]["after_statement"]
    malformed_values.append(missing_statement_key)
    nonobject_statement = copy.deepcopy(valid)
    nonobject_statement["statements"][0] = []
    malformed_values.append(nonobject_statement)
    unsorted = copy.deepcopy(valid)
    unsorted["statements"].reverse()
    malformed_values.append(unsorted)
    missing = copy.deepcopy(valid)
    missing["statements"].pop()
    malformed_values.append(missing)
    wrong_path = copy.deepcopy(valid)
    wrong_path["statements"][0]["path"] = "other"
    malformed_values.append(wrong_path)
    newline_path = copy.deepcopy(valid)
    newline_path["statements"][0]["path"] += "\n"
    malformed_values.append(newline_path)
    carriage_path = copy.deepcopy(valid)
    carriage_path["statements"][0]["path"] += "\r"
    malformed_values.append(carriage_path)
    trailing = copy.deepcopy(valid)
    trailing["statements"][0]["after_statement"] += "\n"
    malformed_values.append(trailing)
    carriage_after = copy.deepcopy(valid)
    carriage_after["statements"][0]["after_statement"] = "#[proctor(2)]\r\nfirst();"
    malformed_values.append(carriage_after)
    empty_path = copy.deepcopy(valid)
    empty_path["statements"][0]["path"] = ""
    malformed_values.append(empty_path)
    empty_after = copy.deepcopy(valid)
    empty_after["statements"][0]["after_statement"] = ""
    malformed_values.append(empty_after)
    bad_id = copy.deepcopy(valid)
    bad_id["statements"][0]["item_id"] = 2**64
    malformed_values.append(bad_id)
    boolean_id = copy.deepcopy(valid)
    boolean_id["statements"][0]["item_id"] = True
    malformed_values.append(boolean_id)
    string_id = copy.deepcopy(valid)
    string_id["statements"][0]["item_id"] = "7"
    malformed_values.append(string_id)
    bad_label = copy.deepcopy(valid)
    bad_label["statements"][0]["label"] = -1
    malformed_values.append(bad_label)
    boolean_label = copy.deepcopy(valid)
    boolean_label["statements"][0]["label"] = False
    malformed_values.append(boolean_label)
    string_label = copy.deepcopy(valid)
    string_label["statements"][0]["label"] = "2"
    malformed_values.append(string_label)
    oversized_label = copy.deepcopy(valid)
    oversized_label["statements"][0]["label"] = 2**32
    malformed_values.append(oversized_label)
    duplicate = copy.deepcopy(valid)
    duplicate["statements"][1] = copy.deepcopy(duplicate["statements"][0])
    malformed_values.append(duplicate)
    outside_item = copy.deepcopy(valid)
    outside_item["statements"][0]["item_id"] = 8
    malformed_values.append(outside_item)
    wrong_path_type = copy.deepcopy(valid)
    wrong_path_type["statements"][0]["path"] = 7
    malformed_values.append(wrong_path_type)
    wrong_after_type = copy.deepcopy(valid)
    wrong_after_type["statements"][0]["after_statement"] = []
    malformed_values.append(wrong_after_type)
    extra_label = copy.deepcopy(valid)
    extra_label["statements"].append(
        {
            "item_id": 7,
            "path": "module::function",
            "label": 5,
            "after_statement": "#[proctor(5)]\nextra();",
        }
    )
    malformed_values.append(extra_label)
    for malformed in malformed_values:
        path.write_text(json.dumps(malformed))
        with pytest.raises(StageFailure):
            _load_replacement_statement_pairs(path, (7,), records)
    path.write_text("{")
    with pytest.raises(StageFailure, match="malformed JSON"):
        _load_replacement_statement_pairs(path, (7,), records)
    path.write_text("[]")
    with pytest.raises(StageFailure):
        _load_replacement_statement_pairs(path, (7,), records)

    preserved = loaded(
        [fn_record(8, "preserved", "preserved", [], needs_transformation=False)]
    )[0]
    path.write_text('{"schema_version":1,"statements":[]}')
    assert _load_replacement_statement_pairs(path, (8,), {8: preserved}) == ()


def test_crat_tools_replace_clears_and_requires_both_scratch_outputs(tmp_path):
    candidate = tmp_path / "candidate.rs"
    sidecar = tmp_path / "pairs.json"
    observation_source = tmp_path / "observation.rs"
    observation_metadata = tmp_path / "metadata.json"
    request = tmp_path / "request.json"
    current = tmp_path / "current"
    current.mkdir()
    request.write_text("{}")
    candidate.write_text("stale candidate")
    sidecar.write_text("stale sidecar")
    events = []

    def runner(command, *, cwd=None, env=None):
        assert not candidate.exists() and not sidecar.exists()
        events.append(command)
        candidate.write_text("candidate")
        sidecar.write_text('{"schema_version":1,"statements":[]}')
        observation_source.write_text("observation")
        observation_metadata.write_text("{}")
        return CommandResult(0)

    tools = CratTools(
        tmp_path / "log",
        run_command=runner,
        environment_factory=lambda path: {},
    )
    tools.crat_tool = Path("/tools/crat-tool")
    tools.replace(
        current,
        request,
        candidate,
        sidecar,
        observation_source,
        observation_metadata,
    )
    assert events[0][-1] == str(current)
    assert events[0][events[0].index("--statement-pairs-output") + 1] == str(sidecar)

    for missing in (candidate, sidecar):
        candidate.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)

        def incomplete_runner(command, *, cwd=None, env=None):
            other = sidecar if missing == candidate else candidate
            other.write_text("output")
            return CommandResult(0)

        broken = CratTools(
            tmp_path / f"log-{missing.name}",
            run_command=incomplete_runner,
            environment_factory=lambda path: {},
        )
        broken.crat_tool = Path("/tools/crat-tool")
        with pytest.raises(StageFailure):
            broken.replace(
                current,
                request,
                candidate,
                sidecar,
                observation_source,
                observation_metadata,
            )
        assert not candidate.exists() and not sidecar.exists()

    for stale in (candidate, sidecar):
        candidate.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        stale.mkdir()
        untouched = stale / "untouched"
        untouched.write_text("keep")
        with pytest.raises(StageFailure):
            tools.replace(
                current,
                request,
                candidate,
                sidecar,
                observation_source,
                observation_metadata,
            )
        assert untouched.read_text() == "keep"
        untouched.unlink()
        stale.rmdir()

    for stale in (candidate, sidecar):
        candidate.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        target = tmp_path / f"{stale.name}.stale-target"
        target.write_text("keep target")
        stale.symlink_to(target)
        tools.replace(
            current,
            request,
            candidate,
            sidecar,
            observation_source,
            observation_metadata,
        )
        assert not stale.is_symlink()
        assert target.read_text() == "keep target"

    for generated_kind in ("symlink", "directory", "fifo"):
        for generated in (candidate, sidecar):
            candidate.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)

            def irregular_runner(command, *, cwd=None, env=None):
                other = sidecar if generated == candidate else candidate
                other.write_text("regular")
                if generated_kind == "symlink":
                    generated.symlink_to(request)
                elif generated_kind == "directory":
                    generated.mkdir()
                else:
                    os.mkfifo(generated)
                return CommandResult(0)

            irregular = CratTools(
                tmp_path / f"new-{generated_kind}-{generated.name}.log",
                run_command=irregular_runner,
                environment_factory=lambda path: {},
            )
            irregular.crat_tool = Path("/tools/crat-tool")
            with pytest.raises(StageFailure):
                irregular.replace(
                    current,
                    request,
                    candidate,
                    sidecar,
                    observation_source,
                    observation_metadata,
                )
            assert not (sidecar if generated == candidate else candidate).exists()
            if generated_kind == "symlink":
                assert not generated.is_symlink()
            else:
                assert generated.exists()
                if generated_kind == "directory":
                    generated.rmdir()
                else:
                    generated.unlink()

    for stale_kind in ("directory", "fifo"):
        for stale in (candidate, sidecar):
            candidate.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            if stale_kind == "directory":
                stale.mkdir()
                child = stale / "untouched"
                child.write_text("keep")
            else:
                os.mkfifo(stale)
            invoked = False

            def must_not_run(command, *, cwd=None, env=None):
                nonlocal invoked
                invoked = True
                return CommandResult(0)

            rejecting = CratTools(
                tmp_path / f"stale-{stale_kind}-{stale.name}.log",
                run_command=must_not_run,
                environment_factory=lambda path: {},
            )
            rejecting.crat_tool = Path("/tools/crat-tool")
            with pytest.raises(StageFailure):
                rejecting.replace(
                    current,
                    request,
                    candidate,
                    sidecar,
                    observation_source,
                    observation_metadata,
                )
            assert not invoked
            if stale_kind == "directory":
                assert child.read_text() == "keep"
                child.unlink()
                stale.rmdir()
            else:
                assert stale.exists()
                stale.unlink()

    def failing_runner(command, *, cwd=None, env=None):
        candidate.write_text("partial candidate")
        sidecar.write_text("partial sidecar")
        return CommandResult(9, "partial stdout", "failed")

    failing = CratTools(
        tmp_path / "command-failure.log",
        run_command=failing_runner,
        environment_factory=lambda path: {},
    )
    failing.crat_tool = Path("/tools/crat-tool")
    with pytest.raises(StageFailure, match="exit code 9"):
        failing.replace(
            current,
            request,
            candidate,
            sidecar,
            observation_source,
            observation_metadata,
        )
    assert not candidate.exists() and not sidecar.exists()


def test_tooling_clears_requires_and_cleans_every_exact_output(tmp_path):
    outputs = (
        tmp_path / "candidate.rs",
        tmp_path / "pairs.json",
        tmp_path / "observation.rs",
        tmp_path / "metadata.json",
    )
    request = tmp_path / "request.json"
    request.write_text("{}")
    current = tmp_path / "current"
    current.mkdir()

    def make_tools(name, runner):
        tools = CratTools(
            tmp_path / f"{name}.log",
            run_command=runner,
            environment_factory=lambda path: {},
        )
        tools.crat_tool = Path("/tools/crat-tool")
        return tools

    for output in outputs:
        output.write_text("stale")

    def complete(command, *, cwd=None, env=None):
        assert all(not output.exists() for output in outputs)
        for output in outputs:
            output.write_text("fresh")
        return CommandResult(0)

    make_tools("complete", complete).replace(current, request, *outputs)
    assert all(output.read_text() == "fresh" for output in outputs)

    for missing in outputs:
        for output in outputs:
            output.unlink(missing_ok=True)

        def incomplete(command, *, cwd=None, env=None, missing=missing):
            for output in outputs:
                if output != missing:
                    output.write_text("partial")
            return CommandResult(0)

        with pytest.raises(StageFailure):
            make_tools(f"missing-{missing.name}", incomplete).replace(
                current, request, *outputs
            )
        assert all(not output.exists() for output in outputs)

    for stale in outputs:
        stale.mkdir()
        invoked = False

        def must_not_run(command, *, cwd=None, env=None):
            nonlocal invoked
            invoked = True
            return CommandResult(0)

        with pytest.raises(StageFailure):
            make_tools(f"stale-{stale.name}", must_not_run).replace(
                current, request, *outputs
            )
        assert not invoked
        stale.rmdir()

    extracted = tmp_path / "extracted.json"
    source, metadata = outputs[2], outputs[3]
    source.write_text("source")
    metadata.write_text("metadata")
    extracted.write_text("stale")

    def extract_complete(command, *, cwd=None, env=None):
        assert not extracted.exists()
        extracted.write_text("result")
        return CommandResult(0)

    make_tools("extract", extract_complete).extract_observations(
        source, metadata, extracted
    )
    assert extracted.read_text() == "result"

    def extract_partial(command, *, cwd=None, env=None):
        extracted.write_text("partial")
        return CommandResult(9, stderr="failed")

    with pytest.raises(StageFailure):
        make_tools("extract-fail", extract_partial).extract_observations(
            source, metadata, extracted
        )
    assert not extracted.exists()


def test_extraction_runs_only_after_successful_build(tmp_path):
    record = fn_record(
        0,
        "target",
        "target",
        [],
        statement_pair_metadata=[_pointer_metadata()],
    )
    sidecars = [
        {
            "schema_version": 1,
            "statements": [
                {
                    "item_id": 0,
                    "path": "target",
                    "label": 0,
                    "after_statement": "#[proctor(0)]\nrejected();",
                }
            ],
        },
        {
            "schema_version": 1,
            "statements": [
                {
                    "item_id": 0,
                    "path": "target",
                    "label": 0,
                    "after_statement": "#[proctor(0)]\naccepted();",
                }
            ],
        },
    ]
    tools = FakeTools(
        skeletons=[record],
        builds=[CommandResult(0), CommandResult(101), CommandResult(0)],
        validators=[VALID, VALID],
        candidates=["rejected source\n", "accepted source\n"],
        sidecars=sidecars,
    )
    value, output = run_fake(tmp_path, tools, FakeClient([response(), response()]))
    report = (value.outputs.artifacts_dir / "statement-pairs.md").read_text()
    assert output.status == "success"
    assert "accepted();" in report and "rejected();" not in report
    assert report.count("### Statement 0") == 1
    assert output.metrics["compilation_failures"] == 1
    assert output.metrics["repair_calls"] == 1
    assert output.metrics["cargo_builds"] == 3
    operations = [event[0] for event in tools.events]
    assert operations.count("extract_observations") == 1
    assert operations.index("extract_observations") > max(
        index
        for index, operation in enumerate(operations)
        if operation == "cargo_build"
    )
    assert not list(value.outputs.artifacts_dir.glob("*statement*pairs*.json"))


# Rule extraction consumes normalized JSON trees, so these builders intentionally
# model the wire grammar rather than Rust syntax.
RULE_I32 = {"kind": "primitive", "name": "i32"}
RULE_BOOL = {"kind": "primitive", "name": "bool"}
RULE_RAW_I32 = {"kind": "raw_pointer", "mutability": "const", "pointee": RULE_I32}
RULE_REF_I32 = {"kind": "reference", "mutability": "shared", "pointee": RULE_I32}
RULE_SLICE_I32 = {"kind": "slice", "element": RULE_I32}
RULE_MUT_SLICE_I32 = {
    "kind": "reference",
    "mutability": "mutable",
    "pointee": RULE_SLICE_I32,
}


def rule_var(sort, index):
    return {"kind": "variable", "sort": sort, "index": index}


def rule_binding(index):
    return {"kind": "path", "value": {"kind": "binding", "id": f"<id{index}>"}}


def rule_external(name):
    return {
        "kind": "path",
        "value": {"kind": "external", "crate": "fixture", "path": [name]},
    }


def rule_call(name, *arguments):
    return {"kind": "call", "callee": rule_external(name), "arguments": list(arguments)}


def rule_unary(operator, operand):
    return {"kind": "unary", "operator": operator, "operand": operand}


def rule_binary(operator, left, right):
    return {"kind": "binary", "operator": operator, "left": left, "right": right}


def rule_integer(value, ty):
    return {
        "kind": "literal",
        "value": {"kind": "integer", "value": str(value), "type": ty},
    }


def rule_method(receiver, crate, path, *arguments):
    return {
        "kind": "method_call",
        "receiver": receiver,
        "method": {"kind": "external", "crate": crate, "path": list(path)},
        "arguments": list(arguments),
    }


def rule_offset(base, amount):
    return rule_method(base, "core", ("ptr", "const_ptr", "offset"), amount)


def rule_range_from(start):
    return {"kind": "range", "start": start, "end": None, "limits": "half_open"}


def rule_index(base, value):
    return {"kind": "index", "base": base, "index": value}


def rule_mutable_slice_from(base, start):
    return {
        "kind": "address_of",
        "borrow": "reference",
        "mutability": "mut",
        "expression": rule_index(base, rule_range_from(start)),
    }


def rule_local_adt(kind="struct", index=0):
    return {"kind": "local", "id": f"<{kind}{index}>"}


def rule_local_adt_type(kind="struct", index=0):
    return {
        "kind": "adt",
        "adt_kind": kind,
        "identity": rule_local_adt(kind, index),
        "arguments": [],
    }


def rule_member(kind="field", owner_kind="struct", owner_index=0, index=0):
    return {
        "kind": "local",
        "owner": rule_local_adt(owner_kind, owner_index),
        "id": f"<{kind}{index}>",
    }


def rule_field(base, owner_kind="struct", owner_index=0, field_index=0):
    return {
        "kind": "field",
        "base": base,
        "field": rule_member("field", owner_kind, owner_index, field_index),
    }


def rule_anchor(index, target_type=RULE_MUT_SLICE_I32):
    return {
        "id": f"<id{index}>",
        "source_type": copy.deepcopy(RULE_RAW_I32),
        "target_type": copy.deepcopy(target_type),
    }


def rule_observation(source, target, *, anchors=None, root_types=None):
    if anchors is None:
        anchors = [rule_anchor(0)]
    if root_types is None:
        root_types = (RULE_I32, RULE_I32, RULE_I32, RULE_I32)
    return {
        "source_expression": source,
        "target_expression": target,
        "pointer_anchors": copy.deepcopy(anchors),
        "source_type": copy.deepcopy(root_types[0]),
        "source_adjusted_type": copy.deepcopy(root_types[1]),
        "target_type": copy.deepcopy(root_types[2]),
        "target_adjusted_type": copy.deepcopy(root_types[3]),
    }


def rule_document(*observations):
    return {"schema_version": 1, "observations": list(observations)}


def loaded_rule_document(*observations):
    return load_observations(json.dumps(rule_document(*observations)))


def minimal_rule_value():
    anchor = rule_var("anchor", 0)
    return {
        "schema_version": 1,
        "rules": [
            {
                "source_pattern": {
                    "kind": "unary",
                    "operator": "deref",
                    "operand": {"kind": "path", "value": copy.deepcopy(anchor)},
                },
                "target_pattern": {"kind": "path", "value": copy.deepcopy(anchor)},
                "pointer_anchors": [
                    {
                        "id": copy.deepcopy(anchor),
                        "source_type": copy.deepcopy(RULE_RAW_I32),
                        "target_type": copy.deepcopy(RULE_REF_I32),
                    }
                ],
                "source_type": copy.deepcopy(RULE_I32),
                "source_adjusted_type": copy.deepcopy(RULE_I32),
                "target_type": copy.deepcopy(RULE_I32),
                "target_adjusted_type": copy.deepcopy(RULE_I32),
            }
        ],
    }


def synthesized(*observations):
    for left_index, left in enumerate(observations):
        for right in observations[left_index + 1 :]:
            result = synthesize_pair(left, right)
            if result.rule is not None:
                _assert_pair_reconstructs(result, left, right)
    return synthesize_rules((loaded_rule_document(*observations),))


def test_minimal_exact_rule_document_round_trips():
    loaded = load_rules(json.dumps(minimal_rule_value()))
    assert load_rules(rules_to_json(loaded)) == loaded
    assert rules_to_json(loaded).endswith("}\n")


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("source_pattern", "operand", "value", "sort"), "value"),
        (("source_pattern", "operand", "value", "index"), True),
        (("source_pattern", "operand", "value", "index"), -1),
        (("source_pattern", "operand", "value", "sort"), "expression"),
        (("source_pattern", "operand", "value", "index"), 2**64),
    ],
)
def test_all_variable_positions_are_closed(path, replacement):
    value = minimal_rule_value()
    nested = value["rules"][0]
    for part in path[:-1]:
        nested = nested[part]
    nested[path[-1]] = replacement
    with pytest.raises(RuleError):
        load_rules(json.dumps(value))


def test_integer_magnitude_variable_is_only_literal_value():
    accepted = minimal_rule_value()
    accepted["rules"][0]["source_pattern"] = {
        "kind": "literal",
        "value": {
            "kind": "integer",
            "value": rule_var("integer_magnitude", 0),
            "type": "isize",
        },
    }
    load_rules(json.dumps(accepted))
    wrong_value = copy.deepcopy(accepted)
    wrong_value["rules"][0]["source_pattern"]["value"]["value"] = rule_var(
        "expression", 0
    )
    complete = copy.deepcopy(accepted)
    complete["rules"][0]["source_pattern"] = rule_var("integer_magnitude", 0)
    for invalid in (wrong_value, complete):
        with pytest.raises(RuleError):
            load_rules(json.dumps(invalid))


def test_local_member_owner_remains_structural():
    value = minimal_rule_value()
    rule = value["rules"][0]
    member = {
        "kind": "local",
        "owner": rule_var("struct", 0),
        "id": rule_var("field", 0),
    }
    pattern = {
        "kind": "field",
        "base": {"kind": "path", "value": rule_var("anchor", 0)},
        "field": member,
    }
    rule["source_pattern"] = pattern
    rule["target_pattern"] = copy.deepcopy(pattern)
    rule["source_type"] = {
        "kind": "adt",
        "adt_kind": "struct",
        "identity": rule_var("struct", 0),
        "arguments": [],
    }
    load_rules(json.dumps(value))
    invalid = copy.deepcopy(value)
    invalid["rules"][0]["source_pattern"]["field"] = rule_var("field", 0)
    with pytest.raises(RuleError):
        load_rules(json.dumps(invalid))


def test_document_rule_and_nested_unknown_fields_reject():
    for path in ((), ("rules", 0), ("rules", 0, "source_pattern")):
        value = minimal_rule_value()
        nested = value
        for part in path:
            nested = nested[part]
        nested["extra"] = True
        with pytest.raises(RuleError):
            load_rules(json.dumps(value))
    for version in (2, 0, True, "1"):
        value = minimal_rule_value()
        value["schema_version"] = version
        with pytest.raises(RuleError):
            load_rules(json.dumps(value))


def test_canonical_indices_and_target_availability_are_checked():
    noncanonical = minimal_rule_value()
    noncanonical["rules"][0]["pointer_anchors"][0]["id"]["index"] = 1
    missing = minimal_rule_value()
    missing["rules"][0]["source_pattern"] = rule_var("expression", 1)
    target_only = minimal_rule_value()
    target_only["rules"][0]["target_pattern"] = rule_var("expression", 0)
    for value in (noncanonical, missing, target_only):
        with pytest.raises(RuleError):
            load_rules(json.dumps(value))


def test_empty_rule_document_has_exact_bytes():
    assert rules_to_json(RuleDocument(rules=())) == (
        '{\n  "schema_version": 1,\n  "rules": []\n}\n'
    )


def test_rule_loader_rejects_concrete_local_ids():
    value = minimal_rule_value()
    value["rules"][0]["pointer_anchors"][0]["id"] = "<id0>"
    with pytest.raises(RuleError):
        load_rules(json.dumps(value))


def test_observation_loader_rejects_every_variable_position():
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    document = rule_document(value)
    document["observations"][0]["source_expression"] = rule_var("expression", 0)
    with pytest.raises(ObservationError):
        load_observations(json.dumps(document))


def test_anchor_identity_is_valid_in_binding_pattern():
    block = {
        "kind": "block",
        "block": {
            "statements": [
                {
                    "kind": "let",
                    "pattern": {
                        "kind": "binding",
                        "id": "<id0>",
                        "mutability": "mutable",
                        "by_ref": "no",
                    },
                    "type": copy.deepcopy(RULE_RAW_I32),
                    "initializer": None,
                },
                {
                    "kind": "expression",
                    "expression": rule_binding(0),
                    "semicolon": False,
                },
            ]
        },
    }
    value = rule_observation(block, copy.deepcopy(block), anchors=[rule_anchor(0)])
    result = synthesized(value, copy.deepcopy(value))
    pattern_id = result.rules[0]["source_pattern"]["block"]["statements"][0]["pattern"][
        "id"
    ]
    assert pattern_id == rule_var("anchor", 0)
    load_rules(rules_to_json(result))


@pytest.mark.parametrize("adt_kind", ["struct", "enum", "union"])
def test_observation_adt_namespace_must_match_kind(adt_kind):
    valid = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(rule_local_adt_type(adt_kind),) * 4,
    )
    loaded_rule_document(valid)
    invalid = copy.deepcopy(valid)
    other = next(kind for kind in ("struct", "enum", "union") if kind != adt_kind)
    invalid["source_type"]["identity"] = rule_local_adt(other)
    with pytest.raises(ObservationError):
        loaded_rule_document(invalid)


def test_integer_magnitude_correspondence():
    rules = synthesized(
        rule_observation(
            rule_offset(rule_binding(0), rule_integer(1, "isize")),
            rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
        ),
        rule_observation(
            rule_offset(rule_binding(0), rule_integer(2, "isize")),
            rule_mutable_slice_from(rule_binding(0), rule_integer(2, "usize")),
        ),
    )
    assert len(rules.rules) == 1
    text = rules_to_json(rules)
    assert text.count('"sort": "integer_magnitude"') == 2


def _reconstruct_rule_value(value, substitutions, seed, role=None):
    if isinstance(value, dict):
        if value.get("kind") == "variable":
            variable = value
            concrete = substitutions[(variable["sort"], variable["index"])][seed]
            if variable["sort"] == "expression":
                return copy.deepcopy(concrete)
            if variable["sort"] == "integer_magnitude" or role == "id":
                return concrete
            if role == "adt":
                return {"kind": "local", "id": concrete}
            assert role == "value"
            kind = "binding" if variable["sort"] == "anchor" else variable["sort"]
            return {"kind": kind, "id": concrete}
        kind = value.get("kind")
        if kind == "path":
            return {
                "kind": "path",
                "value": _reconstruct_rule_value(
                    value["value"], substitutions, seed, "value"
                ),
            }
        if kind == "constructor":
            return {
                "kind": "constructor",
                "adt": _reconstruct_rule_value(
                    value["adt"], substitutions, seed, "adt"
                ),
                "variant": _reconstruct_rule_value(
                    value["variant"], substitutions, seed, "member"
                )
                if value["variant"] is not None
                else None,
            }
        if kind == "local" and "owner" in value:
            return {
                "kind": "local",
                "owner": _reconstruct_rule_value(
                    value["owner"], substitutions, seed, "adt"
                ),
                "id": _reconstruct_rule_value(value["id"], substitutions, seed, "id"),
            }
        if kind == "adt":
            return {
                "kind": "adt",
                "adt_kind": value["adt_kind"],
                "identity": _reconstruct_rule_value(
                    value["identity"], substitutions, seed, "adt"
                ),
                "arguments": [
                    _reconstruct_rule_value(child, substitutions, seed)
                    for child in value["arguments"]
                ],
            }
        if kind == "method_call":
            return {
                "kind": "method_call",
                "receiver": _reconstruct_rule_value(
                    value["receiver"], substitutions, seed
                ),
                "method": _reconstruct_rule_value(
                    value["method"], substitutions, seed, "value"
                ),
                "arguments": [
                    _reconstruct_rule_value(child, substitutions, seed)
                    for child in value["arguments"]
                ],
            }
        if kind == "field":
            return {
                "kind": "field",
                "base": _reconstruct_rule_value(value["base"], substitutions, seed),
                "field": _reconstruct_rule_value(
                    value["field"], substitutions, seed, "member"
                ),
            }
        if kind == "struct":
            return {
                "kind": "struct",
                "adt": _reconstruct_rule_value(
                    value["adt"], substitutions, seed, "adt"
                ),
                "variant": _reconstruct_rule_value(
                    value["variant"], substitutions, seed, "member"
                )
                if value["variant"] is not None
                else None,
                "fields": [
                    {
                        "field": _reconstruct_rule_value(
                            field["field"], substitutions, seed, "member"
                        ),
                        "value": _reconstruct_rule_value(
                            field["value"], substitutions, seed
                        ),
                    }
                    for field in value["fields"]
                ],
                "rest": _reconstruct_rule_value(value["rest"], substitutions, seed)
                if value["rest"] is not None
                else None,
            }
        if kind == "binding" and isinstance(value.get("id"), dict):
            result = copy.deepcopy(value)
            result["id"] = _reconstruct_rule_value(
                value["id"], substitutions, seed, "id"
            )
            return result
        if value.get("kind") == "literal" and isinstance(
            value["value"].get("value"), dict
        ):
            result = copy.deepcopy(value)
            variable = result["value"]["value"]
            result["value"]["value"] = substitutions[
                (variable["sort"], variable["index"])
            ][seed]
            return result
        return {
            key: _reconstruct_rule_value(child, substitutions, seed)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_reconstruct_rule_value(child, substitutions, seed) for child in value]
    return copy.deepcopy(value)


def _assert_pair_reconstructs(result, left, right):
    assert result.rule is not None and result.substitutions is not None
    rule = result.rule
    for seed, expected in enumerate((left, right)):
        anchors = [
            {
                "id": _reconstruct_rule_value(
                    anchor["id"], result.substitutions, seed, "id"
                ),
                "source_type": _reconstruct_rule_value(
                    anchor["source_type"], result.substitutions, seed
                ),
                "target_type": _reconstruct_rule_value(
                    anchor["target_type"], result.substitutions, seed
                ),
            }
            for anchor in rule["pointer_anchors"]
        ]
        reconstructed = {
            "source_expression": _reconstruct_rule_value(
                rule["source_pattern"], result.substitutions, seed
            ),
            "target_expression": _reconstruct_rule_value(
                rule["target_pattern"], result.substitutions, seed
            ),
            "pointer_anchors": anchors,
            "source_type": _reconstruct_rule_value(
                rule["source_type"], result.substitutions, seed
            ),
            "source_adjusted_type": _reconstruct_rule_value(
                rule["source_adjusted_type"], result.substitutions, seed
            ),
            "target_type": _reconstruct_rule_value(
                rule["target_type"], result.substitutions, seed
            ),
            "target_adjusted_type": _reconstruct_rule_value(
                rule["target_adjusted_type"], result.substitutions, seed
            ),
        }
        assert reconstructed == expected


def test_accepted_rule_reconstructs_both_seed_transformations_exactly():
    left = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
    )
    right = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(2, "usize")),
    )
    result = synthesize_pair(left, right)
    _assert_pair_reconstructs(result, left, right)


def test_complete_expression_correspondence():
    left = rule_binary("add", rule_binding(1), rule_integer(1, "isize"))
    right = rule_binary("multiply", rule_binding(1), rule_integer(2, "isize"))
    rules = synthesized(
        rule_observation(
            rule_offset(rule_binding(0), left),
            rule_mutable_slice_from(rule_binding(0), copy.deepcopy(left)),
        ),
        rule_observation(
            rule_offset(rule_binding(0), right),
            rule_mutable_slice_from(rule_binding(0), copy.deepcopy(right)),
        ),
    )
    assert len(rules.rules) == 1
    assert rules_to_json(rules).count('"sort": "expression"') == 2


def test_repeated_complete_expression_correspondence():
    left = rule_binary("add", rule_binding(1), rule_integer(1, "isize"))
    right = rule_binary("multiply", rule_binding(1), rule_integer(2, "isize"))
    rules = synthesized(
        rule_observation(
            rule_call(
                "pair",
                rule_offset(rule_binding(0), left),
                rule_offset(rule_binding(0), copy.deepcopy(left)),
            ),
            rule_call(
                "pair",
                rule_mutable_slice_from(rule_binding(0), copy.deepcopy(left)),
                rule_mutable_slice_from(rule_binding(0), copy.deepcopy(left)),
            ),
        ),
        rule_observation(
            rule_call(
                "pair",
                rule_offset(rule_binding(0), right),
                rule_offset(rule_binding(0), copy.deepcopy(right)),
            ),
            rule_call(
                "pair",
                rule_mutable_slice_from(rule_binding(0), copy.deepcopy(right)),
                rule_mutable_slice_from(rule_binding(0), copy.deepcopy(right)),
            ),
        ),
    )
    assert len(rules.rules) == 1
    assert '"index": 1' not in rules_to_json(rules)


def test_source_only_magnitude_variable():
    rules = synthesized(
        rule_observation(
            rule_method(
                rule_offset(rule_binding(0), rule_integer(1, "isize")),
                "core",
                ("ptr", "const_ptr", "is_null"),
            ),
            {"kind": "literal", "value": {"kind": "bool", "value": False}},
            root_types=(RULE_BOOL,) * 4,
        ),
        rule_observation(
            rule_method(
                rule_offset(rule_binding(0), rule_integer(2, "isize")),
                "core",
                ("ptr", "const_ptr", "is_null"),
            ),
            {"kind": "literal", "value": {"kind": "bool", "value": False}},
            root_types=(RULE_BOOL,) * 4,
        ),
    )
    assert len(rules.rules) == 1


def test_reordered_binding_identities():
    value = rule_observation(
        rule_call(
            "mix",
            rule_unary("deref", rule_binding(0)),
            rule_binding(1),
            rule_binding(2),
        ),
        rule_call("mix", rule_binding(0), rule_binding(2), rule_binding(1)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    result = synthesized(value, copy.deepcopy(value))
    assert len(result.rules) == 1
    text = rules_to_json(result)
    assert text.count('"sort": "binding"') == 4
    assert "<id" not in text


def test_ordered_disagreement_pairs_do_not_collapse():
    result = synthesized(
        rule_observation(
            rule_call(
                "triple",
                rule_unary("deref", rule_binding(0)),
                rule_integer(1, "usize"),
                rule_integer(2, "usize"),
            ),
            rule_call(
                "triple",
                rule_binding(0),
                rule_integer(1, "usize"),
                rule_integer(2, "usize"),
            ),
            anchors=[rule_anchor(0, RULE_REF_I32)],
        ),
        rule_observation(
            rule_call(
                "triple",
                rule_unary("deref", rule_binding(0)),
                rule_integer(2, "usize"),
                rule_integer(1, "usize"),
            ),
            rule_call(
                "triple",
                rule_binding(0),
                rule_integer(2, "usize"),
                rule_integer(1, "usize"),
            ),
            anchors=[rule_anchor(0, RULE_REF_I32)],
        ),
    )
    assert len(result.rules) == 1
    assert '"index": 1' in rules_to_json(result)


def test_exact_rule_from_repeated_equal_observations():
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert len(synthesized(value, copy.deepcopy(value)).rules) == 1
    assert synthesized(value).rules == ()


def test_expression_equal_source_and_target_is_retained():
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_unary("deref", rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert len(synthesized(value, copy.deepcopy(value)).rules) == 1


def test_ordinary_binding_identities_are_explicit():
    amount = rule_binary("add", rule_binding(1), rule_binding(2))
    value = rule_observation(
        rule_offset(rule_binding(0), amount),
        rule_mutable_slice_from(rule_binding(0), copy.deepcopy(amount)),
    )
    text = rules_to_json(synthesized(value, copy.deepcopy(value)))
    assert text.count('"sort": "binding"') == 4


def test_identity_encapsulated_by_one_expression_variable():
    left = rule_observation(
        rule_binary(
            "add",
            rule_integer(1, "i32"),
            rule_unary(
                "deref",
                rule_offset(
                    rule_binding(0),
                    rule_binary("add", rule_binding(1), rule_binding(2)),
                ),
            ),
        ),
        rule_binary(
            "add",
            rule_integer(1, "i32"),
            rule_index(
                rule_binding(0), rule_binary("add", rule_binding(1), rule_binding(2))
            ),
        ),
    )
    right = rule_observation(
        rule_binary(
            "add",
            rule_binding(0),
            rule_unary(
                "deref",
                rule_offset(
                    rule_binding(1),
                    rule_binary("add", rule_binding(2), rule_binding(3)),
                ),
            ),
        ),
        rule_binary(
            "add",
            rule_binding(0),
            rule_index(
                rule_binding(1), rule_binary("add", rule_binding(2), rule_binding(3))
            ),
        ),
        anchors=[rule_anchor(1)],
    )
    assert len(synthesized(left, right).rules) == 1


def test_updated_named_struct_owner_and_field_identity():
    def named(value):
        return {
            "kind": "struct",
            "adt": rule_local_adt(),
            "variant": None,
            "fields": [{"field": rule_member(), "value": value}],
            "rest": None,
        }

    ty = rule_local_adt_type()
    value = rule_observation(
        named(rule_unary("deref", rule_binding(0))),
        named(rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(ty,) * 4,
    )
    text = rules_to_json(synthesized(value, copy.deepcopy(value)))
    assert '"sort": "struct"' in text and '"sort": "field"' in text


def test_promoted_local_field_identity_and_owner():
    value = rule_observation(
        rule_field(rule_unary("deref", rule_binding(0))),
        rule_field(rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    result = synthesized(value, copy.deepcopy(value))
    assert len(result.rules) == 1
    assert '"sort": "field"' in rules_to_json(result)


def test_rigid_external_function_is_preserved():
    result = synthesized(
        rule_observation(
            rule_call("load", rule_offset(rule_binding(0), rule_integer(1, "isize"))),
            rule_call(
                "load",
                rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
            ),
        ),
        rule_observation(
            rule_call("load", rule_offset(rule_binding(0), rule_integer(2, "isize"))),
            rule_call(
                "load",
                rule_mutable_slice_from(rule_binding(0), rule_integer(2, "usize")),
            ),
        ),
    )
    assert len(result.rules) == 1
    assert '"load"' in rules_to_json(result)


def test_child_list_arity_is_hidden_by_enclosing_expression():
    left_call = rule_call("f", rule_binding(1))
    right_call = rule_call("f", rule_binding(1), rule_binding(2))
    result = synthesized(
        rule_observation(
            rule_offset(rule_binding(0), left_call),
            rule_mutable_slice_from(rule_binding(0), copy.deepcopy(left_call)),
        ),
        rule_observation(
            rule_offset(rule_binding(0), right_call),
            rule_mutable_slice_from(rule_binding(0), copy.deepcopy(right_call)),
        ),
    )
    assert len(result.rules) == 1


def test_operator_difference_is_hidden_by_expression():
    left = rule_binary("add", rule_binding(1), rule_integer(1, "isize"))
    right = rule_binary("subtract", rule_binding(1), rule_integer(1, "isize"))
    result = synthesized(
        rule_observation(
            rule_offset(rule_binding(0), left),
            rule_mutable_slice_from(rule_binding(0), copy.deepcopy(left)),
        ),
        rule_observation(
            rule_offset(rule_binding(0), right),
            rule_mutable_slice_from(rule_binding(0), copy.deepcopy(right)),
        ),
    )
    assert len(result.rules) == 1


def test_target_only_magnitude_disagreement_rejects():
    result = synthesized(
        rule_observation(
            rule_method(rule_binding(0), "core", ("ptr", "const_ptr", "read")),
            rule_integer(0, "i32"),
        ),
        rule_observation(
            rule_method(rule_binding(0), "core", ("ptr", "const_ptr", "read")),
            rule_integer(1, "i32"),
        ),
    )
    assert result.rules == ()


def test_different_source_and_target_disagreement_pairs_reject():
    result = synthesized(
        rule_observation(
            rule_offset(rule_binding(0), rule_integer(1, "isize")),
            rule_mutable_slice_from(rule_binding(0), rule_integer(0, "usize")),
        ),
        rule_observation(
            rule_offset(rule_binding(0), rule_integer(2, "isize")),
            rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
        ),
    )
    assert result.rules == ()


def test_identical_sources_with_conflicting_targets_reject():
    source = rule_unary("deref", rule_binding(0))
    result = synthesized(
        rule_observation(
            source, rule_binding(0), anchors=[rule_anchor(0, RULE_REF_I32)]
        ),
        rule_observation(
            copy.deepcopy(source),
            {
                "kind": "address_of",
                "borrow": "reference",
                "mutability": "mut",
                "expression": copy.deepcopy(source),
            },
            anchors=[rule_anchor(0, RULE_REF_I32)],
        ),
    )
    assert result.rules == ()


def test_lone_expression_source_is_degenerate():
    left = rule_observation(
        rule_call("left", rule_unary("deref", rule_binding(0))),
        rule_call("left", rule_unary("deref", rule_binding(0))),
    )
    right = rule_observation(
        rule_call("right", rule_offset(rule_binding(0), rule_integer(1, "isize"))),
        rule_call("right", rule_offset(rule_binding(0), rule_integer(1, "isize"))),
    )
    assert synthesize_pair(left, right).rejection == PairRejection.DEGENERATE_SOURCE


def test_anchor_hidden_by_expression_rejects():
    left = rule_observation(
        rule_call("consume", rule_unary("deref", rule_binding(0))),
        rule_call("consume", rule_unary("deref", rule_binding(0))),
    )
    right = rule_observation(
        rule_call("consume", rule_offset(rule_binding(0), rule_integer(1, "isize"))),
        rule_call("consume", rule_offset(rule_binding(0), rule_integer(1, "isize"))),
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CARRIER


def test_anchor_hidden_despite_explicit_occurrence_rejects():
    left = rule_observation(
        rule_call(
            "combine",
            rule_unary("deref", rule_binding(0)),
            rule_call("read", rule_binding(0)),
        ),
        rule_call("combine", rule_binding(0), rule_call("read", rule_binding(0))),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    right = rule_observation(
        rule_call(
            "combine",
            rule_unary("deref", rule_binding(0)),
            rule_offset(rule_binding(0), rule_integer(1, "isize")),
        ),
        rule_call(
            "combine",
            rule_binding(0),
            rule_offset(rule_binding(0), rule_integer(1, "isize")),
        ),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CARRIER


def test_one_of_multiple_anchors_hidden_rejects():
    left = rule_observation(
        rule_call(
            "combine",
            rule_unary("deref", rule_binding(0)),
            rule_call("read", rule_binding(1)),
        ),
        rule_call("combine", rule_binding(0), rule_call("read", rule_binding(1))),
        anchors=[rule_anchor(0, RULE_REF_I32), rule_anchor(1)],
    )
    right = rule_observation(
        rule_call(
            "combine",
            rule_unary("deref", rule_binding(0)),
            rule_offset(rule_binding(1), rule_integer(1, "isize")),
        ),
        rule_call(
            "combine",
            rule_binding(0),
            rule_offset(rule_binding(1), rule_integer(1, "isize")),
        ),
        anchors=[rule_anchor(0, RULE_REF_I32), rule_anchor(1)],
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CARRIER


def test_local_identity_split_between_explicit_and_expression_rejects():
    left = rule_observation(
        rule_binary(
            "add",
            rule_binding(0),
            rule_unary("deref", rule_offset(rule_binding(1), rule_binding(0))),
        ),
        rule_binary(
            "add", rule_binding(0), rule_index(rule_binding(1), rule_binding(0))
        ),
        anchors=[rule_anchor(1)],
    )
    right = rule_observation(
        rule_binary(
            "add",
            rule_binding(0),
            rule_unary("deref", rule_offset(rule_binding(1), rule_integer(1, "usize"))),
        ),
        rule_binary(
            "add",
            rule_binding(0),
            rule_index(rule_binding(1), rule_integer(1, "usize")),
        ),
        anchors=[rule_anchor(1)],
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CARRIER


def test_inconsistent_binding_equality_partition_rejects():
    left = rule_observation(
        rule_call(
            "combine",
            rule_unary("deref", rule_binding(0)),
            rule_binding(1),
            rule_binding(1),
        ),
        rule_call("combine", rule_binding(0), rule_binding(1), rule_binding(1)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    right = rule_observation(
        rule_call(
            "combine",
            rule_unary("deref", rule_binding(0)),
            rule_binding(1),
            rule_binding(2),
        ),
        rule_call("combine", rule_binding(0), rule_binding(1), rule_binding(2)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CARRIER


def test_target_only_local_field_identity_rejects():
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_field(rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert synthesized(value, copy.deepcopy(value)).rules == ()


def test_different_rigid_external_functions_reject():
    left = rule_observation(
        rule_call("load", rule_unary("deref", rule_binding(0))),
        rule_call("load", rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    right = rule_observation(
        rule_call("peek", rule_unary("deref", rule_binding(0))),
        rule_call("peek", rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert synthesize_pair(left, right).rejection == PairRejection.DEGENERATE_SOURCE


def test_remaining_local_identity_sorts_are_emitted():
    local_values = [
        {"kind": "path", "value": {"kind": "function", "id": "<fn0>"}},
        {"kind": "path", "value": {"kind": "constant", "id": "<const0>"}},
        {"kind": "path", "value": {"kind": "static", "id": "<static0>"}},
        {
            "kind": "method_call",
            "receiver": rule_binding(1),
            "method": {"kind": "method", "id": "<method0>"},
            "arguments": [],
        },
    ]
    enum_expression = {
        "kind": "struct",
        "adt": rule_local_adt("enum"),
        "variant": rule_member("variant", "enum"),
        "fields": [
            {
                "field": rule_member("field", "enum"),
                "value": {
                    "kind": "call",
                    "callee": local_values[0],
                    "arguments": [
                        rule_unary("deref", rule_binding(0)),
                        *local_values[1:],
                    ],
                },
            }
        ],
        "rest": None,
    }
    target = copy.deepcopy(enum_expression)
    target["fields"][0]["value"]["arguments"][0] = rule_binding(0)
    value = rule_observation(
        enum_expression, target, anchors=[rule_anchor(0, RULE_REF_I32)]
    )
    text = rules_to_json(synthesized(value, copy.deepcopy(value)))
    for sort in (
        "function",
        "enum",
        "field",
        "variant",
        "constant",
        "static",
        "method",
    ):
        assert f'"sort": "{sort}"' in text


def test_local_nominal_context_alignment_is_one_environment():
    ty = rule_local_adt_type()
    left = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(ty,) * 4,
    )
    right = copy.deepcopy(left)
    result = synthesized(left, right)
    assert rules_to_json(result).count('"sort": "struct"') == 4


def test_anchor_count_order_and_type_are_context():
    one = rule_observation(rule_binding(0), rule_binding(0), anchors=[rule_anchor(0)])
    two = rule_observation(
        rule_call("f", rule_binding(0), rule_binding(1)),
        rule_call("f", rule_binding(0), rule_binding(1)),
        anchors=[rule_anchor(0), rule_anchor(1)],
    )
    assert synthesize_pair(one, two).rejection == PairRejection.CONTEXT
    left = rule_observation(
        rule_call("f", rule_binding(0), rule_binding(1)),
        rule_call("f", rule_binding(0), rule_binding(1)),
        anchors=[rule_anchor(0, RULE_REF_I32), rule_anchor(1)],
    )
    right = rule_observation(
        rule_call("f", rule_binding(0), rule_binding(1)),
        rule_call("f", rule_binding(0), rule_binding(1)),
        anchors=[rule_anchor(0), rule_anchor(1, RULE_REF_I32)],
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CONTEXT


def test_root_type_constructor_external_and_arity_mismatch_skip():
    ty = rule_local_adt_type()
    left = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(ty,) * 4,
    )
    right = copy.deepcopy(left)
    right["source_type"] = copy.deepcopy(RULE_I32)
    assert synthesize_pair(left, right).rejection == PairRejection.CONTEXT


def test_namespace_bijection_conflict_in_context_skips():
    left_type = {
        "kind": "tuple",
        "elements": [
            rule_local_adt_type("struct", 0),
            rule_local_adt_type("struct", 0),
        ],
    }
    right_type = {
        "kind": "tuple",
        "elements": [
            rule_local_adt_type("struct", 0),
            rule_local_adt_type("struct", 1),
        ],
    }
    left = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(left_type,) * 4,
    )
    right = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(right_type,) * 4,
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CONTEXT


def test_integer_literal_type_controls_narrow_magnitude_rule():
    left = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")), rule_binding(0)
    )
    right = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "usize")), rule_binding(0)
    )
    text = rules_to_json(synthesized(left, right))
    assert '"sort": "expression"' in text
    assert '"sort": "integer_magnitude"' not in text


@pytest.mark.parametrize(
    ("left_value", "right_value", "expected_sort"),
    [
        ("01", "01", None),
        ("01", "02", "expression"),
        ("1", "02", "expression"),
        ("١", "١", None),
        ("١", "٢", "expression"),
        ("0", "1", "integer_magnitude"),
        ("9", "10", "integer_magnitude"),
    ],
)
def test_magnitude_variables_require_canonical_ascii_decimal(
    left_value, right_value, expected_sort
):
    left = rule_observation(
        rule_offset(rule_binding(0), rule_integer(left_value, "isize")), rule_binding(0)
    )
    right = rule_observation(
        rule_offset(rule_binding(0), rule_integer(right_value, "isize")),
        rule_binding(0),
    )
    loaded_rule_document(left, right)
    text = rules_to_json(synthesized(left, right))
    if expected_sort is None:
        assert '"sort": "expression"' not in text
        assert '"sort": "integer_magnitude"' not in text
    else:
        assert f'"sort": "{expected_sort}"' in text


def test_rigid_identity_conflict_can_hide_in_larger_nonroot_expression():
    left = rule_observation(
        rule_call(
            "outer",
            rule_call("load", rule_integer(0, "i32")),
            rule_unary("deref", rule_binding(0)),
        ),
        rule_call("outer", rule_call("load", rule_integer(0, "i32")), rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    right = rule_observation(
        rule_call(
            "outer",
            rule_call("peek", rule_integer(1, "i32")),
            rule_unary("deref", rule_binding(0)),
        ),
        rule_call("outer", rule_call("peek", rule_integer(1, "i32")), rule_binding(0)),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert len(synthesized(left, right).rules) == 1


def test_source_variable_may_be_unused_by_target():
    result = synthesized(
        rule_observation(
            rule_call(
                "select", rule_unary("deref", rule_binding(0)), rule_integer(1, "i32")
            ),
            rule_binding(0),
            anchors=[rule_anchor(0, RULE_REF_I32)],
        ),
        rule_observation(
            rule_call(
                "select", rule_unary("deref", rule_binding(0)), rule_integer(2, "i32")
            ),
            rule_binding(0),
            anchors=[rule_anchor(0, RULE_REF_I32)],
        ),
    )
    assert len(result.rules) == 1


def test_target_lookup_does_not_widen_or_fallback():
    left_amount = rule_binary("add", rule_binding(1), rule_integer(1, "isize"))
    right_amount = rule_binary("multiply", rule_binding(1), rule_integer(2, "isize"))
    result = synthesized(
        rule_observation(
            rule_offset(rule_binding(0), left_amount),
            rule_mutable_slice_from(rule_binding(0), rule_integer(1, "isize")),
        ),
        rule_observation(
            rule_offset(rule_binding(0), right_amount),
            rule_mutable_slice_from(rule_binding(0), rule_integer(2, "isize")),
        ),
    )
    assert result.rules == ()


def test_member_owner_and_member_have_independent_carriers():
    value = rule_observation(
        rule_call(
            "pair",
            rule_field(rule_unary("deref", rule_binding(0)), field_index=0),
            rule_field(rule_unary("deref", rule_binding(0)), field_index=1),
        ),
        rule_call(
            "pair",
            rule_field(rule_binding(0), field_index=0),
            rule_field(rule_binding(0), field_index=1),
        ),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert len(synthesized(value, copy.deepcopy(value)).rules) == 1


def test_distinct_expression_variables_may_have_equal_substitutions():
    left = rule_observation(
        rule_call(
            "triple",
            rule_call("left", rule_integer(0, "i32")),
            rule_call("left", rule_integer(0, "i32")),
            rule_unary("deref", rule_binding(0)),
        ),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    right = rule_observation(
        rule_call(
            "triple",
            rule_call("right", rule_integer(1, "i32")),
            rule_call("other", rule_integer(2, "i32")),
            rule_unary("deref", rule_binding(0)),
        ),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    text = rules_to_json(synthesized(left, right))
    assert '"index": 1' in text


def test_conflicting_and_specific_rules_are_all_retained():
    a = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
    )
    b = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(2, "usize")),
    )
    c = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")),
        rule_call("alternate", rule_binding(0), rule_integer(1, "usize")),
    )
    d = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "isize")),
        rule_call("alternate", rule_binding(0), rule_integer(2, "usize")),
    )
    assert len(synthesized(a, b, copy.deepcopy(a), c, d).rules) == 3


@pytest.mark.parametrize(
    "source,target",
    [
        (
            {"kind": "array", "elements": [rule_unary("deref", rule_binding(0))]},
            {"kind": "array", "elements": [rule_binding(0)]},
        ),
        (
            {
                "kind": "cast",
                "expression": rule_unary("deref", rule_binding(0)),
                "type": RULE_I32,
            },
            {"kind": "cast", "expression": rule_binding(0), "type": RULE_I32},
        ),
        (
            {"kind": "return", "value": rule_unary("deref", rule_binding(0))},
            {"kind": "return", "value": rule_binding(0)},
        ),
        (
            {
                "kind": "repeat",
                "value": rule_unary("deref", rule_binding(0)),
                "count": rule_integer(2, "usize"),
            },
            {
                "kind": "repeat",
                "value": rule_binding(0),
                "count": rule_integer(2, "usize"),
            },
        ),
        (
            {
                "kind": "tuple",
                "elements": [
                    rule_integer(0, "i32"),
                    rule_unary("deref", rule_binding(0)),
                ],
            },
            {"kind": "tuple", "elements": [rule_integer(0, "i32"), rule_binding(0)]},
        ),
        (
            {
                "kind": "if",
                "condition": {
                    "kind": "literal",
                    "value": {"kind": "bool", "value": True},
                },
                "then": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_unary("deref", rule_binding(0)),
                            "semicolon": False,
                        }
                    ]
                },
                "else": rule_unary("deref", rule_binding(0)),
            },
            {
                "kind": "if",
                "condition": {
                    "kind": "literal",
                    "value": {"kind": "bool", "value": True},
                },
                "then": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_binding(0),
                            "semicolon": False,
                        }
                    ]
                },
                "else": rule_binding(0),
            },
        ),
        (
            {
                "kind": "while",
                "condition": {
                    "kind": "literal",
                    "value": {"kind": "bool", "value": True},
                },
                "body": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_unary("deref", rule_binding(0)),
                            "semicolon": False,
                        }
                    ]
                },
            },
            {
                "kind": "while",
                "condition": {
                    "kind": "literal",
                    "value": {"kind": "bool", "value": True},
                },
                "body": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_binding(0),
                            "semicolon": False,
                        }
                    ]
                },
            },
        ),
        (
            {
                "kind": "loop",
                "body": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_unary("deref", rule_binding(0)),
                            "semicolon": False,
                        }
                    ]
                },
            },
            {
                "kind": "loop",
                "body": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_binding(0),
                            "semicolon": False,
                        }
                    ]
                },
            },
        ),
        (
            {
                "kind": "assign",
                "left": rule_integer(0, "i32"),
                "right": rule_unary("deref", rule_binding(0)),
            },
            {
                "kind": "assign",
                "left": rule_integer(0, "i32"),
                "right": rule_binding(0),
            },
        ),
        (
            {
                "kind": "assign_op",
                "operator": "add",
                "left": rule_integer(0, "i32"),
                "right": rule_unary("deref", rule_binding(0)),
            },
            {
                "kind": "assign_op",
                "operator": "add",
                "left": rule_integer(0, "i32"),
                "right": rule_binding(0),
            },
        ),
        (
            {
                "kind": "range",
                "start": rule_integer(0, "usize"),
                "end": rule_unary("deref", rule_binding(0)),
                "limits": "closed",
            },
            {
                "kind": "range",
                "start": rule_integer(0, "usize"),
                "end": rule_binding(0),
                "limits": "closed",
            },
        ),
        (
            {
                "kind": "address_of",
                "borrow": "raw",
                "mutability": "const",
                "expression": rule_unary("deref", rule_binding(0)),
            },
            {
                "kind": "address_of",
                "borrow": "raw",
                "mutability": "const",
                "expression": rule_binding(0),
            },
        ),
        (
            {"kind": "break", "value": rule_unary("deref", rule_binding(0))},
            {"kind": "break", "value": rule_binding(0)},
        ),
        (
            {
                "kind": "block",
                "block": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_unary("deref", rule_binding(0)),
                            "semicolon": False,
                        }
                    ]
                },
            },
            {
                "kind": "block",
                "block": {
                    "statements": [
                        {
                            "kind": "expression",
                            "expression": rule_binding(0),
                            "semicolon": False,
                        }
                    ]
                },
            },
        ),
        (
            {
                "kind": "array",
                "elements": [
                    {"kind": "continue"},
                    rule_unary("deref", rule_binding(0)),
                ],
            },
            {"kind": "array", "elements": [{"kind": "continue"}, rule_binding(0)]},
        ),
    ],
)
def test_every_closed_constructor_traverses_all_children(source, target):
    value = rule_observation(source, target, anchors=[rule_anchor(0, RULE_REF_I32)])
    assert len(synthesized(value, copy.deepcopy(value)).rules) == 1


def test_binding_pattern_block_initializer_reconstructs_exactly():
    source = {
        "kind": "block",
        "block": {
            "statements": [
                {
                    "kind": "let",
                    "pattern": {
                        "kind": "binding",
                        "id": "<id0>",
                        "mutability": "mutable",
                        "by_ref": "no",
                    },
                    "type": copy.deepcopy(RULE_I32),
                    "initializer": rule_unary("deref", rule_binding(1)),
                },
                {
                    "kind": "expression",
                    "expression": rule_binding(0),
                    "semicolon": False,
                },
            ]
        },
    }
    target = copy.deepcopy(source)
    target["block"]["statements"][0]["initializer"] = rule_binding(1)
    value = rule_observation(source, target, anchors=[rule_anchor(1, RULE_REF_I32)])
    result = synthesized(value, copy.deepcopy(value))
    rule = result.rules[0]
    pattern_id = rule["source_pattern"]["block"]["statements"][0]["pattern"]["id"]
    later_path = rule["source_pattern"]["block"]["statements"][1]["expression"]["value"]
    initializer_path = rule["source_pattern"]["block"]["statements"][0]["initializer"][
        "operand"
    ]["value"]
    assert pattern_id == later_path == rule_var("binding", 0)
    assert initializer_path == rule_var("anchor", 0)


@pytest.mark.parametrize(
    "root_type",
    [
        RULE_I32,
        {"kind": "slice", "element": RULE_I32},
        {"kind": "array", "element": RULE_I32, "length": 4},
        RULE_RAW_I32,
        RULE_REF_I32,
        {"kind": "tuple", "elements": [RULE_I32, RULE_REF_I32]},
        rule_local_adt_type("struct"),
        {
            "kind": "adt",
            "adt_kind": "enum",
            "identity": {
                "kind": "external",
                "crate": "core",
                "path": ["option", "Option"],
            },
            "arguments": [RULE_REF_I32],
        },
    ],
)
def test_every_closed_type_constructor_is_retained(root_type):
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(root_type,) * 4,
    )
    assert len(synthesized(value, copy.deepcopy(value)).rules) == 1


def test_root_array_length_mismatch_rejects_context():
    left_type = {"kind": "array", "element": copy.deepcopy(RULE_I32), "length": 4}
    right_type = copy.deepcopy(left_type)
    right_type["length"] = 5
    left = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(left_type,) * 4,
    )
    right = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(right_type,) * 4,
    )
    assert synthesize_pair(left, right).rejection == PairRejection.CONTEXT


def test_local_and_external_root_adt_identity_mismatch_rejects_context():
    local_type = rule_local_adt_type("struct")
    external_type = {
        "kind": "adt",
        "adt_kind": "struct",
        "identity": {
            "kind": "external",
            "crate": "fixture",
            "path": ["External"],
        },
        "arguments": [],
    }
    left = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(local_type,) * 4,
    )
    right = copy.deepcopy(left)
    right["source_type"] = external_type
    assert synthesize_pair(left, right).rejection == PairRejection.CONTEXT


def test_local_root_adt_argument_arity_mismatch_rejects_context():
    left_type = rule_local_adt_type("struct")
    right_type = copy.deepcopy(left_type)
    right_type["arguments"] = [copy.deepcopy(RULE_I32)]
    left = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
        root_types=(left_type,) * 4,
    )
    right = copy.deepcopy(left)
    right["source_type"] = right_type
    assert synthesize_pair(left, right).rejection == PairRejection.CONTEXT


def test_all_loader_reachable_target_only_namespaces_reject():
    targets = [
        rule_field(rule_binding(0)),
        {
            "kind": "cast",
            "expression": rule_binding(0),
            "type": rule_local_adt_type("struct"),
        },
        {
            "kind": "cast",
            "expression": rule_binding(0),
            "type": rule_local_adt_type("union"),
        },
        {
            "kind": "call",
            "callee": {
                "kind": "path",
                "value": {
                    "kind": "constructor",
                    "adt": rule_local_adt("enum"),
                    "variant": rule_member("variant", "enum"),
                },
            },
            "arguments": [rule_binding(0)],
        },
        rule_call(
            "pair",
            rule_binding(0),
            {"kind": "path", "value": {"kind": "constant", "id": "<const0>"}},
        ),
        rule_call(
            "pair",
            rule_binding(0),
            {"kind": "path", "value": {"kind": "static", "id": "<static0>"}},
        ),
        rule_call(
            "pair",
            rule_binding(0),
            {
                "kind": "method_call",
                "receiver": rule_binding(0),
                "method": {"kind": "method", "id": "<method0>"},
                "arguments": [],
            },
        ),
    ]
    for target in targets:
        value = rule_observation(
            rule_unary("deref", rule_binding(0)),
            target,
            anchors=[rule_anchor(0, RULE_REF_I32)],
        )
        assert synthesized(value, copy.deepcopy(value)).rules == ()


def test_constructor_value_identity_preserves_owned_variant():
    constructor = {
        "kind": "path",
        "value": {
            "kind": "constructor",
            "adt": rule_local_adt("enum"),
            "variant": rule_member("variant", "enum"),
        },
    }
    source = {
        "kind": "call",
        "callee": constructor,
        "arguments": [rule_unary("deref", rule_binding(0))],
    }
    target = {
        "kind": "call",
        "callee": copy.deepcopy(constructor),
        "arguments": [rule_binding(0)],
    }
    value = rule_observation(source, target, anchors=[rule_anchor(0, RULE_REF_I32)])
    text = rules_to_json(synthesized(value, copy.deepcopy(value)))
    assert '"sort": "enum"' in text and '"sort": "variant"' in text


def test_foreign_identities_and_noninteger_literals_remain_rigid():
    foreign = {
        "kind": "path",
        "value": {"kind": "foreign_function", "symbol": "ffi_read"},
    }
    source = {
        "kind": "call",
        "callee": foreign,
        "arguments": [
            {"kind": "literal", "value": {"kind": "char", "value": "x"}},
            rule_unary("deref", rule_binding(0)),
        ],
    }
    target = copy.deepcopy(source)
    target["arguments"][1] = rule_binding(0)
    value = rule_observation(source, target, anchors=[rule_anchor(0, RULE_REF_I32)])
    text = rules_to_json(synthesized(value, copy.deepcopy(value)))
    assert "ffi_read" in text and '"value": "x"' in text


def test_canonical_first_occurrence_order_is_context_then_patterns():
    root = rule_local_adt_type("struct", 1)
    value = rule_observation(
        rule_call(
            "ordered",
            rule_unary("deref", rule_binding(0)),
            rule_unary("deref", rule_binding(1)),
            rule_binding(2),
            rule_field(rule_binding(2), owner_index=0),
        ),
        rule_call(
            "ordered",
            rule_binding(0),
            rule_binding(1),
            rule_binding(2),
            rule_field(rule_binding(2), owner_index=0),
        ),
        anchors=[rule_anchor(0, RULE_REF_I32), rule_anchor(1, RULE_REF_I32)],
        root_types=(root,) * 4,
    )
    result = synthesized(value, copy.deepcopy(value))
    assert load_rules(rules_to_json(result)) == result


def test_duplicate_compression_crosses_documents(monkeypatch):
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    calls = []
    real = synthesize_pair

    def tracked(left, right):
        calls.append((left, right))
        return real(left, right)

    import rule_synthesis as synthesis_module

    monkeypatch.setattr(synthesis_module, "synthesize_pair", tracked)
    documents = tuple(loaded_rule_document(copy.deepcopy(value)) for _ in range(3))
    result = synthesize_rules(documents)
    assert len(calls) == 1
    assert len(result.rules) == 1


def test_singleton_never_self_pairs_and_empty_is_success():
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    assert synthesize_rules((loaded_rule_document(value),)).rules == ()
    assert synthesize_rules((loaded_rule_document(),)).rules == ()


def test_input_document_and_observation_permutations_are_byte_identical():
    a = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
    )
    b = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(2, "usize")),
    )
    c = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")),
        rule_call("alternate", rule_binding(0), rule_integer(1, "usize")),
    )
    d = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "isize")),
        rule_call("alternate", rule_binding(0), rule_integer(2, "usize")),
    )
    variants = [
        (loaded_rule_document(a, b), loaded_rule_document(c, d, copy.deepcopy(a))),
        (loaded_rule_document(a, c), loaded_rule_document(d, b, copy.deepcopy(a))),
        (
            loaded_rule_document(a),
            loaded_rule_document(copy.deepcopy(a)),
            loaded_rule_document(c, b, d),
        ),
    ]
    outputs = {rules_to_json(synthesize_rules(documents)) for documents in variants}
    assert len(outputs) == 1


def test_canonical_dedup_ignores_precanonical_variable_indices():
    value = minimal_rule_value()["rules"][0]
    first = copy.deepcopy(value)
    second = copy.deepcopy(value)
    for candidate, index in ((first, 4), (second, 9)):
        candidate["pointer_anchors"][0]["id"]["index"] = index
        candidate["source_pattern"]["operand"]["value"]["index"] = index
        candidate["target_pattern"]["value"]["index"] = index
    assert canonicalize_rule(first) == canonicalize_rule(second)


def test_command_arguments_and_exact_success_file(tmp_path, capsys):
    value = rule_observation(
        rule_unary("deref", rule_binding(0)),
        rule_binding(0),
        anchors=[rule_anchor(0, RULE_REF_I32)],
    )
    source = tmp_path / "a.json"
    source.write_text(
        json.dumps(rule_document(value, copy.deepcopy(value))), encoding="utf-8"
    )
    output = tmp_path / "rules.json"
    assert extract_rules_main(["--output", str(output), str(source)]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    expected = rules_to_json(
        synthesize_rules((load_observations(source.read_text(encoding="utf-8")),))
    )
    assert output.read_text(encoding="utf-8") == expected
    assert set(tmp_path.iterdir()) == {source, output}


@pytest.mark.parametrize("content", ["{}\n", "not json\n", "\xff"])
def test_command_rejects_input_shape_and_path_aliases_before_write(
    tmp_path, capsys, content
):
    source = tmp_path / "a.json"
    if content == "\xff":
        source.write_bytes(b"\xff")
    else:
        source.write_text(content, encoding="utf-8")
    output = tmp_path / "rules.json"
    assert extract_rules_main(["--output", str(output), str(source)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("extract_rules: ") and captured.err.count("\n") == 1
    assert not output.exists()


def test_command_path_rejections(tmp_path, capsys):
    value = rule_document()
    source = tmp_path / "a.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    for arguments in (
        ["--output", str(tmp_path / "out.json")],
        ["--output", str(tmp_path / "out.json"), str(tmp_path / "missing.json")],
        ["--output", str(tmp_path / "out.json"), str(tmp_path)],
        ["--output", str(tmp_path / "out.json"), str(source), str(source)],
        ["--output", str(source), str(source)],
    ):
        assert extract_rules_main(arguments) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("extract_rules: ")


def test_publication_is_atomic_and_preserves_old_output_on_failure(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "a.json"
    source.write_text(json.dumps(rule_document()), encoding="utf-8")
    output = tmp_path / "rules.json"
    output.write_bytes(b"old\n")

    def fail_replace(source_path, destination_path):
        raise OSError("replace failed")

    monkeypatch.setattr(extract_rules_module.os, "replace", fail_replace)
    assert extract_rules_main(["--output", str(output), str(source)]) == 1
    assert output.read_bytes() == b"old\n"
    assert not list(tmp_path.glob(".rules.json.*.tmp"))
    assert capsys.readouterr().err.startswith("extract_rules: replace failed")


@pytest.mark.parametrize("failure_point", ["create", "write", "flush", "close"])
def test_publication_preserves_old_output_for_each_io_failure(
    tmp_path, monkeypatch, failure_point
):
    output = tmp_path / "rules.json"
    output.write_bytes(b"old\n")
    if failure_point == "create":
        monkeypatch.setattr(
            extract_rules_module.tempfile,
            "mkstemp",
            lambda **kwargs: (_ for _ in ()).throw(OSError("create failed")),
        )
    else:
        real_fdopen = extract_rules_module.os.fdopen

        class FailingStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def write(self, text):
                if failure_point == "write":
                    raise OSError("write failed")
                return self.stream.write(text)

            def flush(self):
                if failure_point == "flush":
                    raise OSError("flush failed")
                return self.stream.flush()

            def __exit__(self, exc_type, exc, traceback):
                self.stream.close()
                if failure_point == "close":
                    raise OSError("close failed")
                return False

        monkeypatch.setattr(
            extract_rules_module.os,
            "fdopen",
            lambda descriptor, *args, **kwargs: FailingStream(
                real_fdopen(descriptor, *args, **kwargs)
            ),
        )
    with pytest.raises(OSError, match=failure_point):
        extract_rules_module._publish_output(output, "new\n")
    assert output.read_bytes() == b"old\n"
    assert not list(tmp_path.glob(".rules.json.*.tmp"))


def test_descriptor_close_and_unlink_failures_are_reported(tmp_path, monkeypatch):
    output = tmp_path / "rules.json"
    output.write_bytes(b"old\n")
    real_close = extract_rules_module.os.close
    descriptors = []

    def fail_fdopen(descriptor, *args, **kwargs):
        descriptors.append(descriptor)
        raise OSError("fdopen failed")

    monkeypatch.setattr(extract_rules_module.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(
        extract_rules_module.os,
        "close",
        lambda descriptor: (_ for _ in ()).throw(OSError("raw close failed")),
    )
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda self, **kwargs: (_ for _ in ()).throw(OSError("unlink failed")),
    )
    with pytest.raises(OSError) as raised:
        extract_rules_module._publish_output(output, "new\n")
    message = str(raised.value)
    assert "fdopen failed" in message
    assert "raw close failed" in message
    assert "unlink failed" in message
    assert output.read_bytes() == b"old\n"
    monkeypatch.undo()
    for descriptor in descriptors:
        real_close(descriptor)
    for temporary in tmp_path.glob(".rules.json.*.tmp"):
        real_unlink(temporary)


def test_publication_replaces_symlink_and_rejects_nonregular_nodes(tmp_path):
    target = tmp_path / "target.json"
    target.write_bytes(b"target\n")
    output = tmp_path / "rules.json"
    output.symlink_to(target)
    extract_rules_module._publish_output(output, "new\n")
    assert not output.is_symlink()
    assert output.read_text(encoding="utf-8") == "new\n"
    assert target.read_bytes() == b"target\n"
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(OSError, match="regular file or symlink"):
        extract_rules_module._publish_output(directory, "new\n")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="regular file or symlink"):
        extract_rules_module._publish_output(fifo, "new\n")


def test_command_error_is_one_physical_line_for_line_separator_path(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "observations\nnext.json"
    source.write_text(json.dumps(rule_document()), encoding="utf-8")
    monkeypatch.setattr(
        extract_rules_module,
        "load_observations",
        lambda text: (_ for _ in ()).throw(ValueError("first\r\nsecond\u2028third")),
    )
    assert (
        extract_rules_main(["--output", str(tmp_path / "rules.json"), str(source)]) == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["extract_rules: first second third"]


def test_pair_nonresults_do_not_mask_fatal_command_errors(tmp_path, capsys):
    left = rule_observation(
        rule_method(rule_binding(0), "core", ("ptr", "const_ptr", "read")),
        rule_integer(0, "i32"),
    )
    right = rule_observation(
        rule_method(rule_binding(0), "core", ("ptr", "const_ptr", "read")),
        rule_integer(1, "i32"),
    )
    source = tmp_path / "a.json"
    source.write_text(json.dumps(rule_document(left, right)), encoding="utf-8")
    output = tmp_path / "rules.json"
    assert extract_rules_main(["--output", str(output), str(source)]) == 0
    assert output.read_text(encoding="utf-8") == rules_to_json(RuleDocument(rules=()))
    assert capsys.readouterr().err == ""


def test_synthesis_never_mutates_nested_inputs():
    left = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
    )
    right = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(2, "usize")),
    )
    loaded = loaded_rule_document(left, right)
    before = copy.deepcopy(loaded.observations)
    first = synthesize_rules((loaded,))
    assert loaded.observations == before
    second = synthesize_rules((loaded,))
    assert loaded.observations == before
    assert rules_to_json(first) == rules_to_json(second)


def test_recursive_json_member_order_is_semantically_irrelevant():
    def reverse_members(value):
        if isinstance(value, dict):
            return {key: reverse_members(value[key]) for key in reversed(tuple(value))}
        if isinstance(value, list):
            return [reverse_members(child) for child in value]
        return value

    left = rule_observation(
        rule_offset(rule_binding(0), rule_integer(1, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(1, "usize")),
    )
    right = rule_observation(
        rule_offset(rule_binding(0), rule_integer(2, "isize")),
        rule_mutable_slice_from(rule_binding(0), rule_integer(2, "usize")),
    )
    original = rule_document(left, right, copy.deepcopy(left))
    normal = load_observations(json.dumps(original))
    reversed_document = load_observations(json.dumps(reverse_members(original)))
    assert rules_to_json(synthesize_rules((normal,))) == rules_to_json(
        synthesize_rules((reversed_document,))
    )


def test_help_is_successful_and_side_effect_free(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert extract_rules_main(["--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("usage: extract_rules.py")
    assert "--output" in captured.out
    assert captured.out.endswith("\n")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    [StageFailure("extract failed"), "{ malformed"],
)
def test_post_acceptance_extraction_failure_is_fatal_not_repairable(tmp_path, failure):
    tools = FakeTools(
        skeletons=[
            fn_record(
                0,
                "target",
                "target",
                [],
                statement_pair_metadata=[_pointer_metadata()],
            )
        ],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["accepted source\n"],
        observations=[failure],
    )
    client = FakeClient([response()])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure"
    assert len(client.requests) == 1
    assert len([event for event in tools.events if event[0] == "replace"]) == 1
    assert (
        len([event for event in tools.events if event[0] == "extract_observations"])
        == 1
    )
    assert not value.outputs.rust_project.exists()
    assert not (value.outputs.artifacts_dir / "statement-pairs.md").exists()
    assert not (value.outputs.artifacts_dir / "observations.json").exists()
    for name in (
        "candidate.rs",
        "replacement-statement-pairs.json",
        "replacement-observation.rs",
        "replacement-observation-metadata.json",
        "extracted-observations.json",
    ):
        assert not (value.framework.workdir / name).exists()


def test_accepted_correspondence_promotes_after_extraction(tmp_path):
    records = [
        fn_record(0, "leaf", "leaf", []),
        fn_record(1, "root", "root", [0]),
    ]
    tools = FakeTools(
        skeletons=records,
        builds=[CommandResult(0), CommandResult(0), CommandResult(0)],
        validators=[VALID, VALID],
        candidates=["leaf accepted\n", "root accepted\n"],
    )
    _, output = run_fake(
        tmp_path,
        tools,
        FakeClient([response("leaf"), response("root")]),
    )
    assert output.status == "success"
    replacements = [event for event in tools.events if event[0] == "replace"]
    assert replacements[0][2]["accepted_correspondence"] == []
    assert replacements[1][2]["accepted_correspondence"] == [
        {
            "item_id": 0,
            "logical_path": "leaf",
            "implementation_path": "leaf",
            "wrapper_path": None,
        }
    ]
    assert (
        len([event for event in tools.events if event[0] == "extract_observations"])
        == 2
    )


def test_mechanical_scc_promotes_correspondence_without_extracting(tmp_path):
    records = [
        fn_record(0, "leaf", "leaf", [], needs_transformation=False),
        fn_record(1, "root", "root", [0]),
    ]
    tools = FakeTools(
        skeletons=records,
        builds=[CommandResult(0), CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["leaf mechanical\n", "root accepted\n"],
    )
    _, output = run_fake(tmp_path, tools, FakeClient([response("root")]))
    assert output.status == "success"
    replacements = [event for event in tools.events if event[0] == "replace"]
    assert replacements[1][2]["accepted_correspondence"][0]["logical_path"] == "leaf"
    assert (
        len([event for event in tools.events if event[0] == "extract_observations"])
        == 1
    )


def test_observations_retain_schedule_producer_and_duplicate_order(tmp_path):
    records = [
        fn_record(0, "leaf", "leaf", []),
        fn_record(1, "root", "root", [0]),
    ]
    base = _valid_observation_document()["observations"][0]
    leaf = copy.deepcopy(base)
    root0 = copy.deepcopy(base)
    root1 = copy.deepcopy(base)
    leaf["source_type"] = {"kind": "primitive", "name": "u8"}
    root0["source_type"] = {"kind": "primitive", "name": "u16"}
    root1["source_type"] = {"kind": "primitive", "name": "u32"}
    tools = FakeTools(
        skeletons=records,
        builds=[CommandResult(0), CommandResult(0), CommandResult(0)],
        validators=[VALID, VALID],
        candidates=["leaf accepted\n", "root accepted\n"],
        observations=[
            {"schema_version": 1, "observations": [leaf, copy.deepcopy(leaf)]},
            {"schema_version": 1, "observations": [root0, root1]},
        ],
    )
    value, output = run_fake(
        tmp_path,
        tools,
        FakeClient([response("leaf"), response("root")]),
    )
    assert output.status == "success"
    published = json.loads(
        (value.outputs.artifacts_dir / "observations.json").read_text()
    )
    assert published["observations"] == [leaf, leaf, root0, root1]


def test_malformed_sidecar_is_fatal_before_candidate_installation(tmp_path):
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0)],
        validators=[VALID],
        candidates=["must-not-install\n"],
        sidecars=[{"schema_version": 1, "statements": []}],
    )
    client = FakeClient([response()])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure"
    assert "do not exactly match" in output.error
    assert (value.framework.workdir / "current/lib.rs").read_text() == "normalized\n"
    assert len([event for event in tools.events if event[0] == "cargo_build"]) == 1
    assert len(client.requests) == 1
    assert not value.outputs.rust_project.exists()
    assert not (value.outputs.artifacts_dir / "statement-pairs.md").exists()
    for name in (
        "candidate.rs",
        "replacement-statement-pairs.json",
        "replacement-observation.rs",
        "replacement-observation-metadata.json",
    ):
        assert not (value.framework.workdir / name).exists()


def test_accepted_pairs_sort_by_item_and_label_not_scc_schedule(tmp_path):
    records = [
        fn_record(
            0,
            "root",
            "root",
            [2],
            transformation_labels=[0, 3],
            statement_pair_metadata=[_pointer_metadata(0), _pointer_metadata(3)],
        ),
        fn_record(1, "independent", "independent", []),
        fn_record(2, "leaf", "leaf", []),
    ]
    tools = FakeTools(
        skeletons=records,
        builds=[CommandResult(0)] * 4,
        validators=[VALID] * 3,
        candidates=["independent\n", "leaf\n", "root\n"],
    )
    value, output = run_fake(
        tmp_path,
        tools,
        FakeClient([response("independent"), response("leaf"), response("root")]),
    )
    assert output.status == "success"
    processed = [
        event[2]["items"][0]["id"] for event in tools.events if event[0] == "replace"
    ]
    assert processed == [1, 2, 0]
    report = (value.outputs.artifacts_dir / "statement-pairs.md").read_text()
    positions = [report.index(f"## Item {item_id}:") for item_id in (0, 1, 2)]
    assert positions == sorted(positions)
    assert report.index("### Statement 0") < report.index("### Statement 3")
    assert report.count("## Item 0:") == 1


def test_markdown_renders_origins_completeness_and_escaping_exactly():
    variables = (
        PointerVariableMetadata(
            name="shadow|`\\",
            origin=PointerVariableOrigin("parameter", 0),
            before_type=" \tOption<\r\n    &'a mut [core::mem::MaybeUninit<i32>;\f32],\n> ",
            selected_target_type="&mut [i32]",
            before_type_is_inferred=False,
        ),
        PointerVariableMetadata(
            name="shadow|`\\",
            origin=PointerVariableOrigin("local", 3),
            before_type="*mut i32",
            selected_target_type="*mut i32",
            before_type_is_inferred=True,
        ),
    )
    metadata = StatementPairMetadata(
        label=2,
        before_statement="#[proctor(2)]\n*pointer += 1;",
        pointer_variables_complete=False,
        pointer_variables=variables,
    )
    pair = AcceptedStatementPair(
        item_id=7,
        path="module<&>|`\\::function",
        metadata=metadata,
        after_statement="#[proctor(2)]\n*pointer += 2;",
    )
    report = _render_statement_pairs({(7, 2): pair})
    assert report == (
        "# Before/After Statement Pairs\n\n"
        "This report contains build-accepted local-transformation statement pairs.\n\n"
        "## Item 7: "
        "<code>module&lt;&amp;&gt;&#124;&#96;&#92;::function</code>\n\n"
        "### Statement 2\n\n"
        "#### Before\n\n"
        "```rust\n"
        "#[proctor(2)]\n"
        "*pointer += 1;\n"
        "```\n\n"
        "#### After\n\n"
        "```rust\n"
        "#[proctor(2)]\n"
        "*pointer += 2;\n"
        "```\n\n"
        "#### Pointer variables\n\n"
        "> **Warning:** Pointer-variable metadata is incomplete because Crat could "
        "not\n"
        "> resolve every possible binding occurrence in this source statement.\n\n"
        "| Variable | Origin | Before type | Selected target type | "
        "Before type inferred |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| <code>shadow&#124;&#96;&#92;</code> | <code>parameter 0</code> | "
        "<code>Option&lt; &amp;'a mut "
        "[core::mem::MaybeUninit&lt;i32&gt;; 32], &gt;</code> | "
        "<code>&amp;mut [i32]</code> | no |\n"
        "| <code>shadow&#124;&#96;&#92;</code> | "
        "<code>local statement 3</code> | <code>*mut i32</code> | "
        "<code>*mut i32</code> | yes |\n"
    )
    assert (
        _type_code_value(variables[0].before_type) == "<code>Option&lt; &amp;'a mut "
        "[core::mem::MaybeUninit&lt;i32&gt;; 32], &gt;</code>"
    )
    assert _code_value("&<>|`\\") == ("<code>&amp;&lt;&gt;&#124;&#96;&#92;</code>")
    with pytest.raises(StageFailure):
        _code_value("not\nsingle")
    assert "<code>parameter 0</code>" in report
    assert "<code>local statement 3</code>" in report
    assert "| no |" in report and "| yes |" in report
    assert report.count("<code>shadow&#124;&#96;&#92;</code>") == 2
    assert "> **Warning:** Pointer-variable metadata is incomplete" in report
    assert _rust_fence("plain") == "```rust\nplain\n```"
    assert _rust_fence("a```b") == "````rust\na```b\n````"
    assert _rust_fence("a`````b") == "``````rust\na`````b\n``````"

    complete_empty = replace(
        metadata,
        pointer_variables_complete=True,
        pointer_variables=(),
    )
    incomplete_empty = replace(metadata, pointer_variables=())
    complete_empty_report = _render_statement_pairs(
        {(7, 2): replace(pair, metadata=complete_empty)}
    )
    empty_report_prefix = (
        "# Before/After Statement Pairs\n\n"
        "This report contains build-accepted local-transformation statement pairs.\n\n"
        "## Item 7: <code>module&lt;&amp;&gt;&#124;&#96;&#92;::function</code>\n\n"
        "### Statement 2\n\n"
        "#### Before\n\n"
        "```rust\n"
        "#[proctor(2)]\n"
        "*pointer += 1;\n"
        "```\n\n"
        "#### After\n\n"
        "```rust\n"
        "#[proctor(2)]\n"
        "*pointer += 2;\n"
        "```\n\n"
        "#### Pointer variables\n\n"
    )
    assert complete_empty_report == (
        empty_report_prefix
        + "_No existing source raw-pointer parameter or simple local binding appears in\n"
        "this statement._\n"
    )
    incomplete_empty_report = _render_statement_pairs(
        {(7, 2): replace(pair, metadata=incomplete_empty)}
    )
    assert incomplete_empty_report == (
        empty_report_prefix
        + "> **Warning:** Pointer-variable metadata is incomplete because Crat could "
        "not\n"
        "> resolve every possible binding occurrence in this source statement.\n\n"
        "_No eligible pointer-variable binding could be resolved for this statement._\n"
    )


def test_markdown_uses_complete_before_and_canonical_after_snippets():
    parent = AcceptedStatementPair(
        item_id=4,
        path="choose",
        metadata=StatementPairMetadata(
            0,
            "#[proctor(0)]\nif flag {\n    #[proctor(1)]\n    *pointer\n}",
            True,
            (),
        ),
        after_statement=(
            "#[proctor(0)]\nif flag {\n    #[proctor(1)]\n    changed(pointer)\n}"
        ),
    )
    child = AcceptedStatementPair(
        item_id=4,
        path="choose",
        metadata=StatementPairMetadata(1, "#[proctor(1)]\n*pointer", True, ()),
        after_statement="#[proctor(1)]\nchanged(pointer)",
    )
    expansion = AcceptedStatementPair(
        item_id=5,
        path="expansion",
        metadata=StatementPairMetadata(2, "#[proctor(2)]\n*pointer", True, ()),
        after_statement=(
            "#[proctor(2)]\nlet proctor_temp_var_0 = pointer;\n"
            "#[proctor(2)]\n*proctor_temp_var_0"
        ),
    )
    report = _render_statement_pairs({(4, 0): parent, (4, 1): child, (5, 2): expansion})
    assert report.count("#[proctor(1)]") == 4
    assert report.count("#[proctor(2)]") == 3
    assert "proctor_temp_var_0" in report
    assert "raw rejected" not in report


def test_nonempty_artifact_is_pretty_deterministic_and_data_only(tmp_path):
    observation = _valid_observation_document()["observations"][0]
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID],
        candidates=["accepted source\n"],
        observations=[{"schema_version": 1, "observations": [observation]}],
    )
    value, output = run_fake(tmp_path, tools, FakeClient([response()]))
    assert output.status == "success"
    path = value.outputs.artifacts_dir / "observations.json"
    expected = (
        json.dumps({"schema_version": 1, "observations": [observation]}, indent=2)
        + "\n"
    )
    assert path.read_text() == expected
    assert not (value.outputs.rust_project / "observations.json").exists()
    assert "observations.json" not in output.logs
    assert "proctor" not in path.read_text().lower()


def test_empty_document_is_always_published_beside_statement_pairs(tmp_path):
    empty_report = (
        "# Before/After Statement Pairs\n\n"
        "This report contains build-accepted local-transformation statement pairs.\n\n"
        "_No statements required local transformation._\n"
    )
    for artifacts in (True, False):
        case = tmp_path / ("artifacts" if artifacts else "workdir")
        case.mkdir()
        tools = FakeTools(builds=[CommandResult(0)])
        value, output = run_fake(case, tools, FakeClient([]), artifacts=artifacts)
        assert output.status == "success"
        report = (
            value.outputs.artifacts_dir / "statement-pairs.md"
            if artifacts
            else value.framework.workdir / "statement-pairs.md"
        )
        assert report.read_text() == empty_report
        observations = report.with_name("observations.json")
        assert observations.read_text() == (
            '{\n  "schema_version": 1,\n  "observations": []\n}\n'
        )
        assert not (value.outputs.rust_project / "statement-pairs.md").exists()
        assert not (value.outputs.rust_project / "observations.json").exists()
        assert output.logs == (("local-transformation.log",) if artifacts else ())

    for stale_kind in ("file", "symlink"):
        successful_case = tmp_path / f"successful-stale-{stale_kind}"
        successful_case.mkdir()
        value = stage_input(successful_case)
        report = value.outputs.artifacts_dir / "statement-pairs.md"
        target = successful_case / "stale-target"
        if stale_kind == "file":
            report.write_text("stale")
        else:
            target.write_text("target stays")
            report.symlink_to(target)
        output = run_stage(
            value,
            stage_dir=STAGE_DIR,
            tools=FakeTools(builds=[CommandResult(0)]),
            llm_client_factory=lambda settings, tracker: FakeClient([]),
        )
        assert output.status == "success"
        assert report.read_text() == empty_report
        assert not report.is_symlink()
        if stale_kind == "symlink":
            assert target.read_text() == "target stays"

    for stale_kind in ("file", "symlink"):
        stale_case = tmp_path / f"failed-stale-{stale_kind}"
        stale_case.mkdir()
        value = stage_input(stale_case)
        report = value.outputs.artifacts_dir / "statement-pairs.md"
        target = stale_case / "stale-target"
        if stale_kind == "file":
            report.write_text("stale")
        else:
            target.write_text("target stays")
            report.symlink_to(target)
        output = run_stage(
            value,
            stage_dir=STAGE_DIR,
            tools=FakeTools(builds=[CommandResult(101)]),
            llm_client_factory=lambda settings, tracker: FakeClient([]),
        )
        assert output.status == "failure" and not report.exists()
        if stale_kind == "symlink":
            assert target.read_text() == "target stays"

    directory_case = tmp_path / "directory"
    directory_case.mkdir()
    value = stage_input(directory_case)
    report = value.outputs.artifacts_dir / "statement-pairs.md"
    report.mkdir()
    sibling = value.outputs.artifacts_dir / "sibling"
    sibling.write_text("keep")
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert report.is_dir() and sibling.read_text() == "keep" and not tools.events

    fifo_case = tmp_path / "fifo"
    fifo_case.mkdir()
    value = stage_input(fifo_case)
    report = value.outputs.artifacts_dir / "statement-pairs.md"
    os.mkfifo(report)
    sibling = value.outputs.artifacts_dir / "sibling"
    sibling.write_text("keep")
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert report.exists() and sibling.read_text() == "keep" and not tools.events

    for relation in ("equal", "report_descendant", "report_ancestor"):
        overlap_case = tmp_path / f"overlap-{relation}"
        overlap_case.mkdir()
        value = stage_input(overlap_case)
        artifacts_dir = overlap_case / "new-artifacts"
        report_path = artifacts_dir / "statement-pairs.md"
        if relation == "equal":
            rust_output = report_path
        elif relation == "report_descendant":
            rust_output = artifacts_dir
        else:
            rust_output = report_path / "rust"
        value = replace(
            value,
            outputs=replace(
                value.outputs,
                artifacts_dir=artifacts_dir,
                rust_project=rust_output,
            ),
        )
        tools = FakeTools()
        output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
        assert output.status == "failure"
        assert "report path overlaps" in output.error
        assert not tools.events


def test_project_markdown_json_publish_as_one_cleanup_transaction(
    tmp_path, monkeypatch
):
    current = tmp_path / "current"
    current.mkdir()
    (current / "lib.rs").write_text("accepted")
    (current / "target").mkdir()
    destination = tmp_path / "output"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    report = artifacts / "statement-pairs.md"
    sibling = artifacts / "sibling"
    sibling.write_text("keep")
    events = []
    real_copy = stage_module._copy_final
    real_replace = stage_module.os.replace
    real_fdopen = stage_module.os.fdopen
    real_mkstemp = stage_module.tempfile.mkstemp
    real_remove_output = stage_module._remove_exact_output

    def observed_copy(source, output, mark_destination_created):
        events.append("copy")
        real_copy(source, output, mark_destination_created)

    def observed_replace(source, output):
        events.append("publish")
        real_replace(source, output)

    monkeypatch.setattr(stage_module, "_copy_final", observed_copy)
    monkeypatch.setattr(stage_module.os, "replace", observed_replace)
    _publish_final_outputs(current, destination, report, "report\n")
    assert events[-1] == "publish"
    assert report.read_text() == "report\n"
    assert (destination / "lib.rs").read_text() == "accepted"
    assert not (destination / "target").exists()

    failure_root = tmp_path / "publication-failure"
    failure_root.mkdir()
    failed_destination = failure_root / "output"
    failed_artifacts = failure_root / "artifacts"
    failed_artifacts.mkdir()
    failed_report = failed_artifacts / "statement-pairs.md"
    failed_sibling = failed_artifacts / "sibling"
    failed_sibling.write_text("keep")

    def publication_failure(source, output):
        raise OSError("atomic publication failed")

    monkeypatch.setattr(stage_module, "_copy_final", real_copy)
    monkeypatch.setattr(stage_module.os, "replace", publication_failure)
    with pytest.raises(StageFailure, match="atomic publication failed"):
        _publish_final_outputs(
            current,
            failed_destination,
            failed_report,
            "report\n",
        )
    assert not failed_destination.exists()
    assert not failed_report.exists()
    assert not list(failed_artifacts.glob(".statement-pairs.md.*.tmp"))
    assert failed_sibling.read_text() == "keep"

    ownership_root = tmp_path / "publication-ownership"
    ownership_root.mkdir()
    ownership_destination = ownership_root / "output"
    ownership_artifacts = ownership_root / "artifacts"
    ownership_artifacts.mkdir()
    ownership_report = ownership_artifacts / "statement-pairs.md"

    def external_report_then_failure(source, output):
        ownership_report.write_text("external report")
        raise OSError("publication raced")

    monkeypatch.setattr(stage_module.os, "replace", external_report_then_failure)
    with pytest.raises(StageFailure, match="publication raced"):
        _publish_final_outputs(
            current,
            ownership_destination,
            ownership_report,
            "our report\n",
        )
    assert ownership_report.read_text() == "external report"
    assert not ownership_destination.exists()
    assert not list(ownership_artifacts.glob(".statement-pairs.md.*.tmp"))

    for external_kind in ("file", "symlink", "directory"):
        external_root = tmp_path / f"copy-ownership-{external_kind}"
        external_root.mkdir()
        external_destination = external_root / "output"
        external_report = external_root / "statement-pairs.md"
        external_target = external_root / "external-target"

        def external_destination_then_failure(source, output, mark_destination_created):
            if external_kind == "file":
                output.write_text("external file")
            elif external_kind == "symlink":
                external_target.write_text("external target")
                output.symlink_to(external_target)
            else:
                output.mkdir()
                (output / "external").write_text("external tree")
            raise OSError(f"copy raced with external {external_kind}")

        monkeypatch.setattr(
            stage_module,
            "_copy_final",
            external_destination_then_failure,
        )
        with pytest.raises(StageFailure, match="copy raced with external"):
            _publish_final_outputs(
                current,
                external_destination,
                external_report,
                "report\n",
            )
        if external_kind == "file":
            assert external_destination.read_text() == "external file"
        elif external_kind == "symlink":
            assert external_destination.is_symlink()
            assert external_target.read_text() == "external target"
        else:
            assert (external_destination / "external").read_text() == "external tree"
        assert not external_report.exists()
    monkeypatch.setattr(stage_module, "_copy_final", real_copy)

    temp_failure_root = tmp_path / "temp-write-failure"
    temp_failure_root.mkdir()
    temp_destination = temp_failure_root / "output"
    temp_artifacts = temp_failure_root / "artifacts"
    temp_artifacts.mkdir()
    temp_report = temp_artifacts / "statement-pairs.md"

    class FailingWriter:
        def __init__(self, descriptor):
            self.output = real_fdopen(descriptor, "w", encoding="utf-8", newline="")

        def __enter__(self):
            return self

        def write(self, value):
            self.output.write(value[:4])
            raise OSError("temporary write failed")

        def __exit__(self, *args):
            self.output.close()

    monkeypatch.setattr(
        stage_module.os,
        "fdopen",
        lambda descriptor, *args, **kwargs: FailingWriter(descriptor),
    )
    monkeypatch.setattr(stage_module.os, "replace", real_replace)
    with pytest.raises(StageFailure, match="temporary write failed"):
        _publish_final_outputs(current, temp_destination, temp_report, "report\n")
    assert not temp_destination.exists() and not temp_report.exists()
    assert not list(temp_artifacts.glob(".statement-pairs.md.*.tmp"))
    monkeypatch.setattr(stage_module.os, "fdopen", real_fdopen)

    partial_destination = tmp_path / "partial-output"
    partial_report = artifacts / "partial-report.md"

    def partial_copy(source, output, mark_destination_created):
        output.mkdir()
        mark_destination_created()
        (output / "partial").write_text("partial")
        raise OSError("partial copy")

    monkeypatch.setattr(stage_module, "_copy_final", partial_copy)
    monkeypatch.setattr(stage_module.os, "replace", real_replace)
    with pytest.raises(StageFailure, match="partial copy"):
        _publish_final_outputs(current, partial_destination, partial_report, "report\n")
    assert not partial_destination.exists() and not partial_report.exists()
    assert sibling.read_text() == "keep"

    cleanup_destination = tmp_path / "cleanup-output"
    cleanup_report = artifacts / "cleanup-report.md"

    def cleanup_copy(source, output, mark_destination_created):
        output.mkdir()
        mark_destination_created()
        raise OSError("primary copy failure")

    def cleanup_failure(path):
        real_remove_output(path)
        raise OSError("reported cleanup failure")

    monkeypatch.setattr(stage_module, "_copy_final", cleanup_copy)
    monkeypatch.setattr(stage_module, "_remove_exact_output", cleanup_failure)
    with pytest.raises(StageFailure) as cleanup_error:
        _publish_final_outputs(current, cleanup_destination, cleanup_report, "report\n")
    assert "primary copy failure" in str(cleanup_error.value)
    assert "cleanup failures" in str(cleanup_error.value)
    assert "reported cleanup failure" in str(cleanup_error.value)
    assert not cleanup_destination.exists()

    monkeypatch.setattr(stage_module, "_copy_final", real_copy)
    monkeypatch.setattr(stage_module, "_remove_exact_output", real_remove_output)
    monkeypatch.setattr(stage_module.tempfile, "mkstemp", real_mkstemp)
    monkeypatch.setattr(stage_module.os, "replace", real_replace)

    later_case = tmp_path / "later-fatal-scc"
    later_case.mkdir()
    later_records = [
        fn_record(0, "leaf", "leaf", []),
        fn_record(1, "caller", "caller", [0]),
    ]
    valid_leaf_sidecar = {
        "schema_version": 1,
        "statements": [
            {
                "item_id": 0,
                "path": "leaf",
                "label": 0,
                "after_statement": "#[proctor(0)]\naccepted_leaf();",
            }
        ],
    }
    tools = FakeTools(
        skeletons=later_records,
        builds=[CommandResult(0), CommandResult(0)],
        validators=[VALID, VALID],
        candidates=["accepted leaf\n", "must not install\n"],
        sidecars=[valid_leaf_sidecar, {"schema_version": 1, "statements": []}],
    )
    value, output = run_fake(
        later_case,
        tools,
        FakeClient([response("leaf"), response("caller")]),
    )
    assert output.status == "failure"
    assert not value.outputs.rust_project.exists()
    assert not (value.outputs.artifacts_dir / "statement-pairs.md").exists()

    exhausted_case = tmp_path / "exhausted-repair"
    exhausted_case.mkdir()
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        builds=[CommandResult(0)],
        validators=[INVALID] * 11,
    )
    value, output = run_fake(
        exhausted_case,
        tools,
        FakeClient([response()] * 11),
    )
    assert output.status == "failure"
    assert output.metrics["repair_calls"] == 10
    assert not value.outputs.rust_project.exists()
    assert not (value.outputs.artifacts_dir / "statement-pairs.md").exists()

    mutation_case = tmp_path / "last-mutation"
    mutation_case.mkdir()
    value = stage_input(mutation_case)
    report_path = value.outputs.artifacts_dir / "statement-pairs.md"
    observations_path = value.outputs.artifacts_dir / "observations.json"
    mutations = []

    def run_copy(source, output, mark_destination_created):
        mutations.append(("copy", output))
        real_copy(source, output, mark_destination_created)

    def run_mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        mutations.append(("temporary", Path(name)))
        return descriptor, name

    def run_replace(source, output):
        real_replace(source, output)
        if Path(output) in (report_path, observations_path):
            mutations.append(("publish", Path(output)))

    monkeypatch.setattr(stage_module, "_copy_final", run_copy)
    monkeypatch.setattr(stage_module.tempfile, "mkstemp", run_mkstemp)
    monkeypatch.setattr(stage_module.os, "replace", run_replace)
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=FakeTools(builds=[CommandResult(0)]),
        llm_client_factory=lambda settings, tracker: FakeClient([]),
    )
    assert output.status == "success"
    assert mutations[-2:] == [
        ("publish", report_path),
        ("publish", observations_path),
    ]
    assert (
        value.outputs.rust_project.exists()
        and report_path.exists()
        and observations_path.exists()
    )
    assert not list(value.outputs.artifacts_dir.glob("*statement*pairs*.json"))
