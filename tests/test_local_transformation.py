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
    PointerVariableMetadata,
    PointerVariableOrigin,
    SkeletonError,
    StatementPairMetadata,
    dependency_context,
    function_graph,
    leaf_schedule,
    load_replacement_metadata,
    load_skeletons,
    render_dependency_entry,
    render_transformation_targets,
    strongly_connected_components,
)
from protocol import (
    NO_FENCE_DIAGNOSTIC,
    PromptRenderInput,
    extract_code_block,
    extract_observations_command,
    llm_request,
    make_skeleton_command,
    merge_observations_command,
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

_load_and_validate_replacement_metadata_impl = _load_and_validate_replacement_metadata
_load_replacement_statement_pairs_impl = _load_replacement_statement_pairs


def _load_and_validate_replacement_metadata(
    path,
    candidate,
    statement_pairs,
    observation_source,
    members,
    records_by_id,
    accepted,
):
    return _load_and_validate_replacement_metadata_impl(
        path,
        candidate,
        statement_pairs,
        observation_source,
        members,
        records_by_id,
        {item_id: record.baseline for item_id, record in records_by_id.items()},
        accepted,
    )


def _load_replacement_statement_pairs(path, members, records_by_id):
    return _load_replacement_statement_pairs_impl(
        path,
        members,
        records_by_id,
        {item_id: record.baseline for item_id, record in records_by_id.items()},
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
    skeleton_labels = labels or [0]
    body = "\n".join(
        f"    #[proctor({label})]\n    "
        f"{'todo!()' if label in labels else '()'}"
        f"{';' if index + 1 < len(skeleton_labels) else ''}"
        for index, label in enumerate(skeleton_labels)
    )
    metadata = (
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
    )
    view = {
        "skeleton": f"unsafe fn {name}() {{\n{body}\n}}",
        "needs_transformation": needs_transformation,
        "statement_dispositions": [
            {
                "label": label,
                "disposition": "transform" if label in labels else "preserve",
                "children": [],
            }
            for label in skeleton_labels
        ],
        "statement_pair_metadata": metadata,
    }
    return {
        "id": item_id,
        "path": path,
        "kind": "Fn",
        "name": name,
        "annotated_source": (f"unsafe fn {name}() {{\n    #[proctor(0)]\n    ()\n}}"),
        "baseline": view,
        "applied": view,
        "source_signature": f"unsafe fn {name}()",
        "target_signature": f"unsafe fn {name}()",
        "foreign_function_names": (
            [] if foreign_function_names is None else foreign_function_names
        ),
        "signature_dependencies": (
            [] if signature_dependencies is None else signature_dependencies
        ),
        "dependencies": dependencies,
    }


def apply_rules(
    record: dict[str, object],
    *,
    rule_labels: list[int],
    transform_labels: list[int],
) -> dict[str, object]:
    applied = copy.deepcopy(record["baseline"])
    applied["skeleton"] = applied["skeleton"].replace("todo!()", "rule_fixed()")
    applied["needs_transformation"] = bool(transform_labels)
    applied["statement_dispositions"] = [
        {
            "label": label,
            "disposition": ("rule_applied" if label in rule_labels else "transform"),
            "children": [],
        }
        for label in sorted(rule_labels + transform_labels)
    ]
    baseline_metadata = {
        value["label"]: value for value in record["baseline"]["statement_pair_metadata"]
    }
    applied["statement_pair_metadata"] = [
        baseline_metadata[label] for label in transform_labels
    ]
    record["applied"] = applied
    return record


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
CONTEXT_RECORDS[0]["baseline"]["skeleton"] = (
    "unsafe fn target(mut p: &S) -> i32 {\n    #[proctor(0)]\n    todo!()\n}"
)

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
    record["baseline"]["skeleton"] = record["annotated_source"]
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
    local_abi["baseline"]["skeleton"] = (
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
    scan["baseline"]["skeleton"] = (
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
    scan["baseline"]["statement_dispositions"] = [
        {
            "label": label,
            "disposition": "transform" if label in {0, 1, 2, 4} else "preserve",
            "children": [],
        }
        for label in range(7)
    ]
    scan["baseline"]["statement_pair_metadata"] = [
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
    release["baseline"]["skeleton"] = (
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
    scalar["baseline"]["skeleton"] = scalar["annotated_source"]
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
    helper["baseline"]["skeleton"] = helper["annotated_source"]
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
    target["baseline"]["skeleton"] = target["annotated_source"]
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
    left["baseline"]["skeleton"] = left["annotated_source"]
    left["source_signature"] = "pub unsafe fn parse(mut value: i32) -> i32"
    left["target_signature"] = left["source_signature"]
    right = copy.deepcopy(left)
    right["id"] = 1
    right["path"] = "outer::right::parse"
    right["annotated_source"] = str(right["annotated_source"]).replace(
        "(value + 1)", "(value - 1)"
    )
    right["baseline"]["skeleton"] = right["annotated_source"]
    target = fn_record(2, "target", "target", [0, 1], needs_transformation=False)
    target["annotated_source"] = (
        "pub unsafe fn target(mut value: i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    (outer::left::parse(value) + outer::right::parse(value))\n"
        "}"
    )
    target["baseline"]["skeleton"] = target["annotated_source"]
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
    with pytest.raises(SkeletonError, match="must contain exactly"):
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
    assert parsed.baseline.needs_transformation is True
    assert parsed.baseline.transform_labels == (1, 3)
    for field in ("needs_transformation", "statement_dispositions"):
        malformed = copy.deepcopy(record)
        del malformed["baseline"][field]
        with pytest.raises(SkeletonError):
            loaded([malformed])
    for labels in ([3, 1], [1, 1], [-1], [2**32], [True]):
        malformed = copy.deepcopy(record)
        malformed["baseline"]["statement_dispositions"] = [
            {"label": label, "disposition": "transform", "children": []}
            for label in labels
        ]
        with pytest.raises(SkeletonError):
            loaded([malformed])
    for value in (0, 1, "true", None):
        malformed = copy.deepcopy(record)
        malformed["baseline"]["needs_transformation"] = value
        with pytest.raises(SkeletonError):
            loaded([malformed])
    for value in (None, 1, "1,3", {"labels": [1, 3]}):
        malformed = copy.deepcopy(record)
        malformed["baseline"]["statement_dispositions"] = value
        with pytest.raises(SkeletonError):
            loaded([malformed])
    malformed = copy.deepcopy(record)
    malformed["baseline"]["needs_transformation"] = False
    with pytest.raises(SkeletonError, match="inconsistent"):
        loaded([malformed])


def nested_view_record() -> dict[str, object]:
    record = fn_record(
        0,
        "nested",
        "nested",
        [],
        needs_transformation=True,
        transformation_labels=[0, 1, 2],
    )
    record["baseline"]["skeleton"] = (
        "unsafe fn nested(mut p: *mut i32) -> i32 {\n"
        "    #[proctor(0)]\n"
        "    if p.is_null() {\n"
        "        #[proctor(1)]\n"
        "        let mut value: i32 = todo!();\n"
        "    } else {\n"
        "        #[proctor(2)]\n"
        "        value = todo!();\n"
        "    };\n"
        "    0\n"
        "}"
    )
    record["baseline"]["statement_dispositions"] = [
        {
            "label": 0,
            "disposition": "transform",
            "children": [
                {"label": 1, "disposition": "transform", "children": []},
                {"label": 2, "disposition": "transform", "children": []},
            ],
        }
    ]
    record["applied"] = copy.deepcopy(record["baseline"])
    record["applied"]["skeleton"] = (
        record["applied"]["skeleton"]
        .replace("p.is_null()", "ready(p)")
        .replace("todo!()", "fixed()")
    )
    record["source_signature"] = "unsafe fn nested(mut p: *mut i32) -> i32"
    record["target_signature"] = record["source_signature"]
    return record


def test_dual_view_loader_checks_lexical_structure_not_header_whitespace():
    record = nested_view_record()
    record["applied"]["skeleton"] = record["applied"]["skeleton"].replace(
        "unsafe fn nested", "unsafe  /* normalized */ fn\n nested"
    )
    parsed = loaded([record])[0]
    assert parsed.baseline.transform_labels == (0, 1, 2)

    mutations = {
        "signature": lambda text: text.replace("fn nested", "fn renamed", 1),
        "declaration": lambda text: text.replace(
            "let mut value: i32", "let value: i32", 1
        ),
        "control": lambda text: text.replace("if ready(p)", "while ready(p)", 1),
        "label order": lambda text: (
            text.replace("#[proctor(1)]", "#[proctor(99)]", 1)
            .replace("#[proctor(2)]", "#[proctor(1)]", 1)
            .replace("#[proctor(99)]", "#[proctor(2)]", 1)
        ),
    }
    for mutation in mutations.values():
        malformed = nested_view_record()
        malformed["applied"]["skeleton"] = mutation(malformed["applied"]["skeleton"])
        with pytest.raises(SkeletonError):
            loaded([malformed])


def test_dual_view_loader_keeps_tail_children_in_their_control_branch():
    record = nested_view_record()
    for view_name in ("baseline", "applied"):
        skeleton = record[view_name]["skeleton"]
        replacement = (
            "branch_value()" if view_name == "baseline" else "fixed_branch_value()"
        )
        record[view_name]["skeleton"] = skeleton.replace(
            "let mut value: i32 = todo!();"
            if view_name == "baseline"
            else "let mut value: i32 = fixed();",
            replacement,
            1,
        )
    parsed = loaded([record])[0]
    assert parsed.applied.transform_labels == (0, 1, 2)


@pytest.mark.parametrize("declaration", ["let value: i32;", "let pointer: *mut i32;"])
def test_dual_view_loader_accepts_initializerless_local_declarations(declaration):
    record = fn_record(0, "declaration", "declaration", [])
    skeleton = (
        "unsafe fn declaration() {\n"
        f"    #[proctor(0)]\n    {declaration}\n"
        "}"
    )
    record["baseline"]["skeleton"] = skeleton
    record["applied"] = copy.deepcopy(record["baseline"])
    assert loaded([record])[0].baseline.transform_labels == (0,)

    malformed = copy.deepcopy(record)
    malformed["applied"]["skeleton"] = malformed["applied"]["skeleton"].replace(
        "let ", "let mut ", 1
    )
    with pytest.raises(SkeletonError, match="declaration topology"):
        loaded([malformed])


def test_dual_view_loader_tracks_let_else_child_slots():
    record = fn_record(
        0,
        "choose",
        "choose",
        [],
        transformation_labels=[0, 1, 2],
    )
    record["baseline"]["skeleton"] = (
        "unsafe fn choose(input: Option<i32>) {\n"
        "    #[proctor(0)]\n"
        "    let Some(value) = input else {\n"
        "        #[proctor(1)]\n"
        "        return;\n"
        "    };\n"
        "    #[proctor(2)]\n"
        "    consume(value);\n"
        "}"
    )
    record["baseline"]["statement_dispositions"] = [
        {
            "label": 0,
            "disposition": "transform",
            "children": [
                {"label": 1, "disposition": "transform", "children": []}
            ],
        },
        {"label": 2, "disposition": "transform", "children": []},
    ]
    record["applied"] = copy.deepcopy(record["baseline"])
    assert loaded([record])[0].baseline.transform_labels == (0, 1, 2)

    moved = copy.deepcopy(record)
    moved["applied"]["skeleton"] = (
        "unsafe fn choose(input: Option<i32>) {\n"
        "    #[proctor(0)]\n"
        "    let Some(value) = {\n"
        "        #[proctor(1)]\n"
        "        input\n"
        "    } else {};\n"
        "    #[proctor(2)]\n"
        "    consume(value);\n"
        "}"
    )
    with pytest.raises(SkeletonError, match="control child slots"):
        loaded([moved])


def test_dual_view_loader_ends_brace_macro_before_following_sibling():
    record = fn_record(
        0,
        "macros",
        "macros",
        [],
        transformation_labels=[0, 1],
    )
    record["baseline"]["skeleton"] = (
        "unsafe fn macros() {\n"
        "    #[proctor(0)]\n"
        "    crate::tokens! { #[proctor(99)] anything }\n"
        "    #[proctor(1)]\n"
        "    consume();\n"
        "}"
    )
    record["applied"] = copy.deepcopy(record["baseline"])
    assert loaded([record])[0].baseline.transform_labels == (0, 1)


def test_dual_view_loader_distinguishes_tail_and_semicolon_expression_shells():
    record = fn_record(0, "value", "value", [])
    record["baseline"]["skeleton"] = (
        "unsafe fn value() -> i32 {\n    #[proctor(0)]\n    1\n}"
    )
    record["applied"] = copy.deepcopy(record["baseline"])
    record["applied"]["skeleton"] = record["applied"]["skeleton"].replace(
        "    1\n}", "    1;\n}"
    )
    with pytest.raises(SkeletonError, match="control topology"):
        loaded([record])


@pytest.mark.parametrize("jump", ["return", "break"])
def test_dual_view_loader_distinguishes_expression_and_jump_payloads(jump):
    record = fn_record(
        0,
        "control",
        "control",
        [],
        transformation_labels=[0, 1],
    )
    record["baseline"]["skeleton"] = (
        "unsafe fn control() {\n"
        "    #[proctor(0)]\n"
        "    loop {\n"
        "        #[proctor(1)]\n"
        "        consume();\n"
        "    }\n"
        "}"
    )
    record["baseline"]["statement_dispositions"] = [
        {
            "label": 0,
            "disposition": "transform",
            "children": [
                {"label": 1, "disposition": "transform", "children": []}
            ],
        }
    ]
    record["applied"] = copy.deepcopy(record["baseline"])
    record["applied"]["skeleton"] = record["applied"]["skeleton"].replace(
        "consume();", f"{jump};"
    )
    with pytest.raises(SkeletonError, match="control topology"):
        loaded([record])


def test_dual_view_loader_checks_forest_correspondence_and_control_slots():
    missing = nested_view_record()
    missing["baseline"]["skeleton"] = missing["baseline"]["skeleton"].replace(
        "#[proctor(2)]\n", "", 1
    )
    with pytest.raises(SkeletonError, match="disposition topology"):
        loaded([missing])

    moved = nested_view_record()
    moved["applied"]["skeleton"] = moved["applied"]["skeleton"].replace(
        "    } else {\n        #[proctor(2)]\n        value = fixed();\n",
        "        #[proctor(2)]\n        value = fixed();\n    } else {\n",
        1,
    )
    with pytest.raises(SkeletonError, match="control child slots"):
        loaded([moved])

    reordered_forest = nested_view_record()
    children = reordered_forest["baseline"]["statement_dispositions"][0]["children"]
    reordered_forest["baseline"]["statement_dispositions"][0]["children"] = list(
        reversed(children)
    )
    metadata = reordered_forest["baseline"]["statement_pair_metadata"]
    reordered_forest["baseline"]["statement_pair_metadata"] = [
        metadata[0],
        metadata[2],
        metadata[1],
    ]
    reordered_forest["applied"] = copy.deepcopy(reordered_forest["baseline"])
    with pytest.raises(SkeletonError, match="out-of-order"):
        loaded([reordered_forest])


def test_dual_view_loader_enforces_cross_view_disposition_transitions():
    preserved = fn_record(0, "stable", "stable", [], needs_transformation=False)
    for disposition in ("transform", "rule_applied"):
        malformed = copy.deepcopy(preserved)
        malformed["applied"] = copy.deepcopy(malformed["baseline"])
        malformed["applied"]["statement_dispositions"][0]["disposition"] = disposition
        malformed["applied"]["needs_transformation"] = disposition == "transform"
        if disposition == "transform":
            malformed["applied"]["statement_pair_metadata"] = [
                {
                    "label": 0,
                    "before_statement": "#[proctor(0)]\n()",
                    "pointer_variables_complete": True,
                    "pointer_variables": [],
                }
            ]
        with pytest.raises(SkeletonError):
            loaded([malformed])

    transformed = fn_record(0, "open", "open", [])
    transformed["applied"] = copy.deepcopy(transformed["baseline"])
    transformed["applied"]["statement_dispositions"][0]["disposition"] = "rule_applied"
    transformed["applied"]["needs_transformation"] = False
    transformed["applied"]["statement_pair_metadata"] = []
    assert loaded([transformed])[0].applied.contains_rule_application

    preserved_transform = fn_record(0, "open", "open", [])
    preserved_transform["applied"] = copy.deepcopy(preserved_transform["baseline"])
    preserved_transform["applied"]["statement_dispositions"][0][
        "disposition"
    ] = "preserve"
    preserved_transform["applied"]["needs_transformation"] = False
    preserved_transform["applied"]["statement_pair_metadata"] = []
    with pytest.raises(SkeletonError, match="preserves transformable"):
        loaded([preserved_transform])

    preserved_shell = nested_view_record()
    for view_name in ("baseline", "applied"):
        preserved_shell[view_name]["statement_dispositions"][0][
            "disposition"
        ] = "preserve_shell"
        preserved_shell[view_name]["statement_pair_metadata"] = [
            entry
            for entry in preserved_shell[view_name]["statement_pair_metadata"]
            if entry["label"] != 0
        ]
    parsed = loaded([preserved_shell])[0]
    assert parsed.baseline.statement_dispositions[0].disposition == "preserve_shell"
    assert parsed.applied.transform_labels == (1, 2)

    changed_shell = copy.deepcopy(preserved_shell)
    changed_shell["applied"]["statement_dispositions"][0][
        "disposition"
    ] = "transform"
    changed_shell["applied"]["statement_pair_metadata"].insert(
        0,
        {
            "label": 0,
            "before_statement": "#[proctor(0)]\nif ready { todo!() }",
            "pointer_variables_complete": True,
            "pointer_variables": [],
        },
    )
    with pytest.raises(SkeletonError, match="changes preserved-shell"):
        loaded([changed_shell])


@pytest.mark.parametrize(
    ("outer", "first_child"),
    [
        ("transform", "transform"),
        ("rule_applied", "transform"),
        ("transform", "rule_applied"),
    ],
)
def test_dual_view_loader_accepts_nested_transform_and_rule_transitions(
    outer, first_child
):
    record = nested_view_record()
    applied = record["applied"]
    applied["statement_dispositions"][0]["disposition"] = outer
    applied["statement_dispositions"][0]["children"][0][
        "disposition"
    ] = first_child
    metadata_by_label = {
        entry["label"]: entry for entry in applied["statement_pair_metadata"]
    }
    transform_labels = [
        label
        for label, disposition in (
            (0, outer),
            (1, first_child),
            (2, "transform"),
        )
        if disposition == "transform"
    ]
    applied["statement_pair_metadata"] = [
        metadata_by_label[label] for label in transform_labels
    ]
    applied["needs_transformation"] = bool(transform_labels)
    parsed = loaded([record])[0]
    assert parsed.applied.transform_labels == tuple(transform_labels)


def test_function_records_and_python_helpers_add_statement_pair_metadata():
    plain = fn_record(0, "scalar", "scalar", [])
    assert list(plain) == [
        "id",
        "path",
        "kind",
        "name",
        "annotated_source",
        "baseline",
        "applied",
        "source_signature",
        "target_signature",
        "foreign_function_names",
        "signature_dependencies",
        "dependencies",
    ]
    assert plain["baseline"]["statement_pair_metadata"]
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
    records = record_map()
    request = validation_request(
        (3, 0),
        records,
        {item_id: record.baseline for item_id, record in records.items()},
        "code",
    )
    assert [value["id"] for value in request["expected_functions"]] == [0, 3]
    assert list(request["expected_functions"][0]) == [
        "id",
        "name",
        "view",
    ]
    assert request["transformation"] == "code"


def test_replacement_request_is_exact_and_member_ordered():
    records = record_map()
    request = replacement_request(
        (3, 0),
        records,
        {item_id: record.baseline for item_id, record in records.items()},
        "code",
    )
    assert [value["id"] for value in request["items"]] == [0, 3]
    assert list(request["items"][0]) == [
        "id",
        "path",
        "name",
        "view",
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
    views = {item_id: record.baseline for item_id, record in records_by_id.items()}
    validation = validation_request((2,), records_by_id, views, transformation)
    replacement = replacement_request((2,), records_by_id, views, transformation)
    assert validation["transformation"] == transformation
    assert replacement["transformation"] == transformation
    assert "foreign_function_names" not in validation["expected_functions"][0]
    assert "foreign_function_names" not in replacement["items"][0]
    assert list(validation["expected_functions"][0]) == [
        "id",
        "name",
        "view",
    ]
    assert list(replacement["items"][0]) == [
        "id",
        "path",
        "name",
        "view",
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
    assert make_skeleton_command(
        tool,
        Path("/work/current"),
        Path("/work/skeletons.json"),
        Path("/inputs/rules.json"),
    ) == [
        "/tools/crat-tool",
        "make-skeleton",
        "--output",
        "/work/skeletons.json",
        "--rules",
        "/inputs/rules.json",
        "/work/current",
    ]
    assert merge_observations_command(
        tool,
        Path("/work/merged.json"),
        (Path("/work/000.json"), Path("/work/001.json")),
    ) == [
        "/tools/crat-tool",
        "merge-observations",
        "--output",
        "/work/merged.json",
        "/work/000.json",
        "/work/001.json",
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


def _transform_labels(dispositions):
    return [
        label
        for disposition in dispositions
        for label in (
            (
                [disposition["label"]]
                if disposition["disposition"] == "transform"
                else []
            )
            + _transform_labels(disposition["children"])
        )
    ]


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
        merged_observations=None,
    ):
        self.skeletons = [] if skeletons is None else skeletons
        self.normalized = normalized
        self.builds = list(builds or [CommandResult(0)])
        self.validators = list(validators or [])
        self.candidates = list(candidates or [])
        self.sidecars = None if sidecars is None else list(sidecars)
        self.observations = None if observations is None else list(observations)
        self.merged_observations = merged_observations
        self.events = []

    def build_tools(self, crat_dir):
        self.events.append(("build_tools", crat_dir))
        return crat_dir / "target/release/crat", crat_dir / "target/release/crat-tool"

    def prepare(self, current, passes, use_print):
        self.events.append(("prepare", current, passes, use_print))

    def make_skeleton(self, current, output, rule_set=None):
        self.events.append(("make_skeleton", current, output, rule_set))
        output.write_text(json.dumps(self.skeletons), encoding="utf-8")

    def merge_observations(self, inputs, output):
        self.events.append(("merge_observations", inputs, output))
        if isinstance(self.merged_observations, Exception):
            raise self.merged_observations
        if isinstance(self.merged_observations, str):
            output.write_text(self.merged_observations, encoding="utf-8")
            return
        observations = []
        for path in inputs:
            observations.extend(json.loads(path.read_text())["observations"])
        output.write_text(
            json.dumps({"schema_version": 1, "observations": observations}, indent=2)
            + "\n",
            encoding="utf-8",
        )

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
                for label in _transform_labels(item["view"]["statement_dispositions"])
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
                    "transform_labels": _transform_labels(
                        item["view"]["statement_dispositions"]
                    ),
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


def stage_input(tmp_path, *, artifacts=True, config=None, llm=None, rule_set=None):
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
        inputs=InputArtifacts(rust_project=source, rule_set=rule_set),
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
            None,
        ),
        (
            "normalize",
            value.framework.workdir / "current/lib.rs",
            value.framework.workdir / "normalized.rs",
        ),
        ("cargo_build", value.framework.workdir / "current", "pub struct S;\n"),
        (
            "merge_observations",
            (),
            value.framework.workdir / "merged-observations.json",
        ),
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


def test_optional_rule_set_is_forwarded_only_to_skeleton_generation(tmp_path):
    rule_set = tmp_path / "input-rules.json"
    rule_set.write_text('{"schema_version":1,"rules":[]}\n')
    tools = FakeTools(builds=[CommandResult(0)])
    value, output = run_fake(
        tmp_path,
        tools,
        FakeClient([]),
        rule_set=rule_set,
    )
    assert output.status == "success"
    assert next(event for event in tools.events if event[0] == "make_skeleton") == (
        "make_skeleton",
        value.framework.workdir / "current",
        value.framework.workdir / "skeletons.json",
        rule_set,
    )
    assert all(rule_set not in event[1:] for event in tools.events[3:])


def test_rule_set_path_is_redacted_when_command_runner_raises(tmp_path):
    rule_set = tmp_path / "sensitive-rule-name.json"
    rule_set.write_text('{"schema_version":1,"rules":[]}\n')
    value = stage_input(tmp_path, rule_set=rule_set)
    log_path = value.outputs.artifacts_dir / "local-transformation.log"

    def raising_runner(command, *, cwd=None, env=None):
        raise RuntimeError(f"runner rejected argv {command!r} including {rule_set}")

    class ReadyCratTools(CratTools):
        def build_tools(self, crat_dir):
            self.crat = Path("/tools/crat")
            self.crat_tool = Path("/tools/crat-tool")
            self.environment = {}
            return self.crat, self.crat_tool

        def prepare(self, current_project, passes, use_print):
            return None

    tools = ReadyCratTools(
        log_path,
        run_command=raising_runner,
        environment_factory=lambda path: {},
    )
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert str(rule_set) not in output.error
    assert "<rule-set>" in output.error
    log = log_path.read_text()
    assert str(rule_set) not in log
    assert "<rule-set>" in log


def test_rule_set_rejects_symlinks_and_overlapping_input_paths(tmp_path):
    target = tmp_path / "real-rules.json"
    target.write_text("{}")
    link = tmp_path / "rules-link.json"
    link.symlink_to(target)
    tools = FakeTools()
    symlink_case = tmp_path / "symlink"
    symlink_case.mkdir()
    _, output = run_fake(symlink_case, tools, FakeClient([]), rule_set=link)
    assert output.status == "failure"
    assert "regular nonsymlink file" in output.error
    assert tools.events == []

    case = tmp_path / "overlap"
    case.mkdir()
    value = stage_input(case)
    overlapping = value.inputs.rust_project / "rules.json"
    overlapping.write_text("{}")
    value = replace(
        value,
        inputs=InputArtifacts(
            rust_project=value.inputs.rust_project,
            rule_set=overlapping,
        ),
    )
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert "overlaps input Rust project" in output.error
    assert tools.events == []


def test_applied_view_projection_is_consistent_across_tool_requests():
    raw = apply_rules(
        fn_record(0, "target", "target", [], transformation_labels=[0, 1]),
        rule_labels=[0],
        transform_labels=[1],
    )
    record = loaded([raw])[0]
    records = {0: record}
    views = {0: record.applied}
    validation = validation_request((0,), records, views, "generated")
    replacement = replacement_request((0,), records, views, "generated")
    assert (
        validation["expected_functions"][0]["view"] == replacement["items"][0]["view"]
    )
    assert validation["expected_functions"][0]["view"]["statement_dispositions"] == [
        {"label": 0, "disposition": "rule_applied", "children": []},
        {"label": 1, "disposition": "transform", "children": []},
    ]


def test_rule_complete_scc_is_mechanical_and_skips_observation_extraction(tmp_path):
    record = apply_rules(
        fn_record(0, "target", "target", []),
        rule_labels=[0],
        transform_labels=[],
    )
    tools = FakeTools(
        skeletons=[record],
        builds=[CommandResult(0), CommandResult(0)],
        candidates=["mechanically fixed\n"],
    )
    value, output = run_fake(tmp_path, tools, FakeClient([]))
    assert output.status == "success"
    replacement = next(event for event in tools.events if event[0] == "replace")
    assert replacement[2]["items"][0]["view"] == record["applied"]
    assert replacement[2]["transformation"] == record["applied"]["skeleton"]
    assert not [event for event in tools.events if event[0] == "validate"]
    assert not [event for event in tools.events if event[0] == "extract_observations"]
    assert (value.outputs.rust_project / "lib.rs").read_text() == "mechanically fixed\n"


def test_failed_applied_build_falls_back_once_to_baseline_with_shared_budget(tmp_path):
    record = apply_rules(
        fn_record(0, "target", "target", []),
        rule_labels=[0],
        transform_labels=[],
    )
    tools = FakeTools(
        skeletons=[record],
        builds=[CommandResult(0), CommandResult(101, "out0", "err0"), CommandResult(0)],
        validators=[VALID],
        candidates=["bad applied\n", "good baseline\n"],
    )
    client = FakeClient([response()])
    value, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    replacements = [event for event in tools.events if event[0] == "replace"]
    assert replacements[0][2]["items"][0]["view"] == record["applied"]
    assert replacements[1][2]["items"][0]["view"] == record["baseline"]
    assert len(client.requests) == 1
    assert "out0" in client.requests[0].messages[0].content
    assert "err0" in client.requests[0].messages[0].content
    assert output.metrics["repair_calls"] == 1
    assert output.metrics["compilation_failures"] == 1
    assert (value.outputs.rust_project / "lib.rs").read_text() == "good baseline\n"


def test_mixed_applied_scc_build_failure_switches_every_member_to_baseline(tmp_path):
    first = apply_rules(
        fn_record(0, "first", "first", [1]),
        rule_labels=[0],
        transform_labels=[],
    )
    second = fn_record(1, "second", "second", [0])
    tools = FakeTools(
        skeletons=[first, second],
        builds=[CommandResult(0), CommandResult(101, "out0", "err0"), CommandResult(0)],
        validators=[VALID, VALID],
        candidates=["bad mixed applied\n", "good whole baseline\n"],
    )
    client = FakeClient([response(), response()])
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    replacements = [event for event in tools.events if event[0] == "replace"]
    assert [item["view"] for item in replacements[0][2]["items"]] == [
        first["applied"],
        second["applied"],
    ]
    assert [item["view"] for item in replacements[1][2]["items"]] == [
        first["baseline"],
        second["baseline"],
    ]
    assert "unsafe fn target()" in client.requests[1].messages[0].content
    assert "out0" in client.requests[1].messages[0].content
    assert "err0" in client.requests[1].messages[0].content
    assert output.metrics == {
        "function_count": 2,
        "scc_count": 1,
        "llm_generation_calls": 2,
        "repair_calls": 1,
        "structural_failures": 0,
        "compilation_failures": 1,
        "cargo_builds": 3,
    }


def test_repairs_before_applied_build_failure_are_not_reset_on_fallback(tmp_path):
    record = apply_rules(
        fn_record(
            0,
            "target",
            "target",
            [],
            transformation_labels=[0, 1],
        ),
        rule_labels=[0],
        transform_labels=[1],
    )
    tools = FakeTools(
        skeletons=[record],
        builds=[CommandResult(0), CommandResult(101, "out2", "err2"), CommandResult(0)],
        validators=[INVALID, VALID, VALID],
        candidates=["bad applied repair two\n", "good baseline repair three\n"],
    )
    client = FakeClient(["missing fence", response(), response(), response()])
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "success"
    validations = [event for event in tools.events if event[0] == "validate"]
    assert [entry[1]["expected_functions"][0]["view"] for entry in validations] == [
        record["applied"],
        record["applied"],
        record["baseline"],
    ]
    assert "unsafe fn target()" in client.requests[3].messages[0].content
    assert output.metrics == {
        "function_count": 1,
        "scc_count": 1,
        "llm_generation_calls": 4,
        "repair_calls": 3,
        "structural_failures": 2,
        "compilation_failures": 1,
        "cargo_builds": 3,
    }


@pytest.mark.parametrize("mechanical_entry", [False, True])
def test_applied_fallback_and_baseline_repairs_share_ten_repair_limit(
    tmp_path, mechanical_entry
):
    if mechanical_entry:
        record = apply_rules(
            fn_record(0, "target", "target", []),
            rule_labels=[0],
            transform_labels=[],
        )
        response_count = 10
    else:
        record = apply_rules(
            fn_record(
                0,
                "target",
                "target",
                [],
                transformation_labels=[0, 1],
            ),
            rule_labels=[0],
            transform_labels=[1],
        )
        response_count = 11
    failed_builds = [
        CommandResult(101, f"out{index}", f"err{index}") for index in range(11)
    ]
    tools = FakeTools(
        skeletons=[record],
        builds=[CommandResult(0), *failed_builds],
        validators=[VALID] * response_count,
        candidates=[f"candidate {index}\n" for index in range(11)],
    )
    client = FakeClient([response()] * response_count)
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "failure"
    assert len(client.requests) == response_count
    assert output.metrics == {
        "function_count": 1,
        "scc_count": 1,
        "llm_generation_calls": response_count,
        "repair_calls": 10,
        "structural_failures": 0,
        "compilation_failures": 11,
        "cargo_builds": 12,
    }
    replacements = [event for event in tools.events if event[0] == "replace"]
    assert replacements[0][2]["items"][0]["view"] == record["applied"]
    assert all(
        event[2]["items"][0]["view"] == record["baseline"] for event in replacements[1:]
    )


def test_build_failure_without_rule_application_repairs_in_same_view(tmp_path):
    record = fn_record(0, "target", "target", [])
    tools = FakeTools(
        skeletons=[record],
        builds=[CommandResult(0), CommandResult(101, "out", "err"), CommandResult(0)],
        validators=[VALID, VALID],
        candidates=["bad ordinary\n", "good ordinary\n"],
    )
    _, output = run_fake(tmp_path, tools, FakeClient([response(), response()]))
    assert output.status == "success"
    replacements = [event for event in tools.events if event[0] == "replace"]
    assert all(
        event[2]["items"][0]["view"] == record["applied"] for event in replacements
    )
    assert output.metrics["repair_calls"] == 1
    assert output.metrics["compilation_failures"] == 1


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
    assert [item["view"]["needs_transformation"] for item in items] == [False, True]
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
    record["baseline"]["skeleton"] = (
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
    assert replacement[2]["transformation"] == record["baseline"]["skeleton"]
    assert "&mut i32" in replacement[2]["items"][0]["view"]["skeleton"]
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
    assert manifest["requires"] == {
        "rust_project": "required",
        "rule_set": "optional",
    }
    assert manifest["produces"] == {"rust_project": True}
    assert set(manifest["config"]) == {"crat_dir", "dump_llm_exchanges"}
    assert manifest["config"]["crat_dir"]["default"] == "../crat"
    assert manifest["config"]["dump_llm_exchanges"]["default"] is False
    assert not (
        {"c_project", "test_package"}
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
    assert parsed.baseline.statement_pair_metadata[0].pointer_variables_complete is True
    assert parsed.baseline.statement_pair_metadata[0].pointer_variables[
        0
    ].before_type == ("Option<\n    *mut i32,\n>")
    assert [
        row.name for row in parsed.baseline.statement_pair_metadata[0].pointer_variables
    ] == [
        "pointer",
        "alias",
    ]

    malformed_records = []
    missing = copy.deepcopy(record)
    del missing["baseline"]["statement_pair_metadata"]
    malformed_records.append(missing)
    unknown = copy.deepcopy(record)
    unknown["baseline"]["statement_pair_metadata"][0]["unknown"] = 1
    malformed_records.append(unknown)
    wrong_labels = copy.deepcopy(record)
    wrong_labels["baseline"]["statement_pair_metadata"][0]["label"] = 3
    malformed_records.append(wrong_labels)
    newline = copy.deepcopy(record)
    newline["baseline"]["statement_pair_metadata"][0]["before_statement"] += "\n"
    malformed_records.append(newline)
    non_boolean = copy.deepcopy(record)
    non_boolean["baseline"]["statement_pair_metadata"][0][
        "pointer_variables_complete"
    ] = 1
    malformed_records.append(non_boolean)
    bad_name = copy.deepcopy(record)
    bad_name["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
        "name"
    ] = "a\nb"
    malformed_records.append(bad_name)
    bad_origin = copy.deepcopy(record)
    bad_origin["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
        "origin"
    ] = {
        "kind": "parameter",
        "declaration_label": 0,
    }
    malformed_records.append(bad_origin)
    duplicate_origin = copy.deepcopy(record)
    duplicate_origin["baseline"]["statement_pair_metadata"][0]["pointer_variables"][1][
        "origin"
    ] = {
        "kind": "parameter",
        "index": 0,
    }
    malformed_records.append(duplicate_origin)
    empty_type = copy.deepcopy(record)
    empty_type["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
        "selected_target_type"
    ] = ""
    malformed_records.append(empty_type)
    inferred_integer = copy.deepcopy(record)
    inferred_integer["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
        "before_type_is_inferred"
    ] = 0
    malformed_records.append(inferred_integer)
    for bad_metadata in (None, {}, "metadata"):
        malformed = copy.deepcopy(record)
        malformed["baseline"]["statement_pair_metadata"] = bad_metadata
        malformed_records.append(malformed)
    nonobject_statement = copy.deepcopy(record)
    nonobject_statement["baseline"]["statement_pair_metadata"][0] = []
    malformed_records.append(nonobject_statement)
    for bad_label in (True, -1, 2**32):
        malformed = copy.deepcopy(record)
        malformed["baseline"]["statement_pair_metadata"][0]["label"] = bad_label
        malformed_records.append(malformed)
    empty_before = copy.deepcopy(record)
    empty_before["baseline"]["statement_pair_metadata"][0]["before_statement"] = ""
    malformed_records.append(empty_before)
    wrong_before_type = copy.deepcopy(record)
    wrong_before_type["baseline"]["statement_pair_metadata"][0]["before_statement"] = 7
    malformed_records.append(wrong_before_type)
    carriage_return = copy.deepcopy(record)
    carriage_return["baseline"]["statement_pair_metadata"][0]["before_statement"] += (
        "\r"
    )
    malformed_records.append(carriage_return)
    wrong_variables = copy.deepcopy(record)
    wrong_variables["baseline"]["statement_pair_metadata"][0]["pointer_variables"] = {}
    malformed_records.append(wrong_variables)
    nonobject_variable = copy.deepcopy(record)
    nonobject_variable["baseline"]["statement_pair_metadata"][0]["pointer_variables"][
        0
    ] = "row"
    malformed_records.append(nonobject_variable)
    missing_variable_key = copy.deepcopy(record)
    del missing_variable_key["baseline"]["statement_pair_metadata"][0][
        "pointer_variables"
    ][0]["before_type"]
    malformed_records.append(missing_variable_key)
    unknown_variable_key = copy.deepcopy(record)
    unknown_variable_key["baseline"]["statement_pair_metadata"][0]["pointer_variables"][
        0
    ]["unknown"] = 1
    malformed_records.append(unknown_variable_key)
    for field in ("name", "before_type", "selected_target_type"):
        malformed = copy.deepcopy(record)
        malformed["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
            field
        ] = ""
        malformed_records.append(malformed)
        malformed = copy.deepcopy(record)
        malformed["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
            field
        ] = 7
        malformed_records.append(malformed)
    carriage_name = copy.deepcopy(record)
    carriage_name["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
        "name"
    ] = "pointer\rname"
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
        malformed["baseline"]["statement_pair_metadata"][0]["pointer_variables"][0][
            "origin"
        ] = bad_origin
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
    assert [
        entry.label for entry in parsed_ordered.baseline.statement_pair_metadata
    ] == [2, 4]
    assert (
        parsed_ordered.baseline.statement_pair_metadata[0].pointer_variables_complete
        is False
    )
    for labels in ([4], [2, 3, 4], [4, 2]):
        malformed = copy.deepcopy(ordered)
        by_label = {
            entry["label"]: entry
            for entry in malformed["baseline"]["statement_pair_metadata"]
        }
        malformed["baseline"]["statement_pair_metadata"] = [
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
    leaf = {"opaque": "leaf"}
    root0 = {"opaque": "root-0"}
    root1 = {"opaque": "root-1"}
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
    observation = {"opaque": ["unchanged", 7, True]}
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


def test_merged_observation_output_is_published_as_opaque_bytes(tmp_path):
    merged = "opaque merge output\nwith no Python-readable JSON\n"
    tools = FakeTools(
        builds=[CommandResult(0)],
        merged_observations=merged,
    )
    value, output = run_fake(tmp_path, tools, FakeClient([]))
    assert output.status == "success"
    assert (value.outputs.artifacts_dir / "observations.json").read_text() == merged
    merge = next(event for event in tools.events if event[0] == "merge_observations")
    assert merge[1] == ()


def test_merge_failure_prevents_all_final_publication(tmp_path):
    tools = FakeTools(
        builds=[CommandResult(0)],
        merged_observations=StageFailure("merge rejected opaque inputs"),
    )
    value, output = run_fake(tmp_path, tools, FakeClient([]))
    assert output.status == "failure"
    assert "merge rejected opaque inputs" in output.error
    assert not value.outputs.rust_project.exists()
    assert not (value.outputs.artifacts_dir / "statement-pairs.md").exists()
    assert not (value.outputs.artifacts_dir / "observations.json").exists()


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
