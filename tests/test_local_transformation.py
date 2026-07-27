from __future__ import annotations

import copy
import hashlib
import json
import os
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
    SkeletonError,
    dependency_context,
    function_graph,
    leaf_schedule,
    load_skeletons,
    render_dependency_entry,
    render_transformation_targets,
    strongly_connected_components,
)
from protocol import (
    NO_FENCE_DIAGNOSTIC,
    PromptRenderInput,
    extract_code_block,
    llm_request,
    make_skeleton_command,
    normalize_safety_command,
    render_prompt,
    replace_command,
    replacement_request,
    validate_command,
    validation_request,
)
from stage import run_stage
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


def test_existing_function_records_and_helpers_adopt_the_final_shape():
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
        "foreign_function_names",
        "signature_dependencies",
        "dependencies",
    ]
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
    assert set(request) == {"schema_version", "items", "transformation"}


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


def test_crat_tool_argv_is_exact_for_all_four_operations():
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
    ) == [
        "/tools/crat-tool",
        "replace",
        "--request",
        "/work/replacement-request.json",
        "--output",
        "/work/candidate.rs",
        "/work/current",
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


class FakeTools:
    def __init__(
        self,
        skeletons=None,
        normalized="normalized\n",
        builds=None,
        validators=None,
        candidates=None,
    ):
        self.skeletons = [] if skeletons is None else skeletons
        self.normalized = normalized
        self.builds = list(builds or [CommandResult(0)])
        self.validators = list(validators or [])
        self.candidates = list(candidates or [])
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

    def replace(self, current, request, candidate):
        self.events.append(
            (
                "replace",
                current,
                json.loads(request.read_text()),
                (current / "lib.rs").read_text(),
            )
        )
        candidate.write_text(self.candidates.pop(0), encoding="utf-8")


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
        def replace(self, current, request, candidate):
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
        def replace(self, current, request, candidate):
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
