from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

U64_MAX = 2**64 - 1
U32_MAX = 2**32 - 1
VALUE_KINDS = frozenset({"Fn", "Static", "Const"})
TYPE_KINDS = frozenset({"TyAlias", "Enum", "Struct", "Union"})
ALL_KINDS = VALUE_KINDS | TYPE_KINDS


class SkeletonError(ValueError):
    pass


class ContextOverflow(ValueError):
    pass


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class CallableCorrespondence:
    item_id: int
    logical_path: str
    implementation_path: str
    wrapper_path: str | None


@dataclass(frozen=True)
class CurrentObservationItem:
    item_id: int
    logical_path: str
    source_copy_path: str
    implementation_path: str
    wrapper_path: str | None
    transform_labels: tuple[int, ...]


@dataclass(frozen=True)
class SourceStub:
    item_id: int
    path: str


@dataclass(frozen=True)
class ReplacementMetadata:
    candidate_sha256: str
    statement_pairs_sha256: str
    observation_source_sha256: str
    accepted_correspondence: tuple[CallableCorrespondence, ...]
    new_correspondence: tuple[CallableCorrespondence, ...]
    current_items: tuple[CurrentObservationItem, ...]
    source_stubs: tuple[SourceStub, ...]
    dependency_correspondence: tuple[CallableCorrespondence, ...]
    frozen_project_sha256: str
    unfilled_stub_items: tuple[int, ...]


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class UnstructuredFailure:
    diagnostics: str
    failed_transformation: str | None


@dataclass(frozen=True)
class UnstructuredReplacement:
    source: str
    candidate_sha256: str
    frozen_project_sha256: str
    new_correspondence: tuple[CallableCorrespondence, ...]


def load_unstructured_replacement(
    text: str,
) -> UnstructuredReplacement | UnstructuredFailure:
    value = json.loads(text)
    if isinstance(value, dict) and value.get("status") == "invalid":
        value = _exact_object(
            value,
            {"status", "diagnostics", "failed_transformation"},
            "unstructured response",
        )
        diagnostics = value["diagnostics"]
        if not isinstance(diagnostics, str) or not diagnostics:
            raise ObservationError("unstructured diagnostics must be a nonempty string")
        failed = value["failed_transformation"]
        if failed is not None and not isinstance(failed, str):
            raise ObservationError(
                "unstructured failed_transformation must be a string or null"
            )
        return UnstructuredFailure(diagnostics, failed)
    value = _exact_object(
        value,
        {
            "status",
            "source",
            "candidate_sha256",
            "frozen_project_sha256",
            "new_correspondence",
        },
        "unstructured response",
    )
    if value["status"] != "valid" or not isinstance(value["source"], str):
        raise ObservationError("unstructured response must contain valid source")
    for key in ("candidate_sha256", "frozen_project_sha256"):
        if not isinstance(value[key], str) or _DIGEST.fullmatch(value[key]) is None:
            raise ObservationError(
                f"unstructured response {key} must be a SHA-256 digest"
            )
    if not isinstance(value["new_correspondence"], list):
        raise ObservationError("unstructured new_correspondence must be an array")
    return UnstructuredReplacement(
        value["source"],
        value["candidate_sha256"],
        value["frozen_project_sha256"],
        tuple(
            _correspondence(record, "unstructured new_correspondence")
            for record in value["new_correspondence"]
        ),
    )


def _exact_object(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ObservationError(f"{where} must contain exactly {sorted(keys)}")
    return value


def _path(value: Any, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(not _rust_identifier_segment(segment) for segment in value.split("::"))
    ):
        raise ObservationError(f"{where} must be a canonical crate-relative Rust path")
    return value


_RUST_KEYWORDS = frozenset(
    "Self abstract as async await become box break const continue crate do dyn else enum "
    "extern false final fn for gen if impl in let loop macro match mod move mut override "
    "priv pub ref return self static struct super trait true try type typeof union unsafe "
    "unsized use virtual where while yield".split()
)


def _rust_identifier_segment(segment: str) -> bool:
    raw = segment.startswith("r#")
    identifier = segment[2:] if raw else segment
    if not identifier or identifier == "_" or not identifier.isidentifier():
        return False
    if raw:
        return identifier not in {"Self", "crate", "self", "super"}
    return identifier not in _RUST_KEYWORDS


def _wire_integer(value: Any, where: str, maximum: int = U64_MAX) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ObservationError(f"{where} must be an integer")
    if not 0 <= value <= maximum:
        raise ObservationError(f"{where} must be in the unsigned integer range")
    return value


def _wire_u32_labels(value: Any, where: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ObservationError(f"{where} must be an array")
    result = tuple(_wire_integer(item, f"{where} entry", U32_MAX) for item in value)
    for previous, current in pairwise(result):
        if current <= previous:
            detail = "duplicate" if current == previous else "out-of-order"
            raise ObservationError(f"{where} has {detail} label {current}")
    return result


def _correspondence(value: Any, where: str) -> CallableCorrespondence:
    value = _exact_object(
        value,
        {"item_id", "logical_path", "implementation_path", "wrapper_path"},
        where,
    )
    wrapper = value["wrapper_path"]
    if wrapper is not None:
        wrapper = _path(wrapper, f"{where}.wrapper_path")
    return CallableCorrespondence(
        item_id=_wire_integer(value["item_id"], f"{where}.item_id"),
        logical_path=_path(value["logical_path"], f"{where}.logical_path"),
        implementation_path=_path(
            value["implementation_path"], f"{where}.implementation_path"
        ),
        wrapper_path=wrapper,
    )


def load_replacement_metadata(text: str) -> ReplacementMetadata:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ObservationError(
            f"replacement metadata JSON decode failure: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ObservationError("replacement metadata must be an object")
    keys = {
        "candidate_sha256",
        "statement_pairs_sha256",
        "observation_source_sha256",
        "accepted_correspondence",
        "new_correspondence",
        "current_items",
        "source_stubs",
        "dependency_correspondence",
        "frozen_project_sha256",
        "unfilled_stub_items",
    }
    value = _exact_object(
        value,
        keys,
        "replacement metadata",
    )
    digests: dict[str, str] = {}
    for key in (
        "candidate_sha256",
        "statement_pairs_sha256",
        "observation_source_sha256",
        "frozen_project_sha256",
    ):
        digest = value[key]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ObservationError(
                f"replacement metadata {key} must be 64 lowercase hexadecimal digits"
            )
        digests[key] = digest
    correspondence_lists: dict[str, tuple[CallableCorrespondence, ...]] = {}
    for key in (
        "accepted_correspondence",
        "new_correspondence",
        "dependency_correspondence",
    ):
        raw = value[key]
        if not isinstance(raw, list):
            raise ObservationError(f"replacement metadata {key} must be an array")
        correspondence_lists[key] = tuple(
            _correspondence(item, f"replacement metadata {key}[{index}]")
            for index, item in enumerate(raw)
        )
    raw_current = value["current_items"]
    if not isinstance(raw_current, list):
        raise ObservationError("replacement metadata current_items must be an array")
    current: list[CurrentObservationItem] = []
    for index, item in enumerate(raw_current):
        where = f"replacement metadata current_items[{index}]"
        item = _exact_object(
            item,
            {
                "item_id",
                "logical_path",
                "source_copy_path",
                "implementation_path",
                "wrapper_path",
                "transform_labels",
            },
            where,
        )
        wrapper = item["wrapper_path"]
        if wrapper is not None:
            wrapper = _path(wrapper, f"{where}.wrapper_path")
        current.append(
            CurrentObservationItem(
                item_id=_wire_integer(item["item_id"], f"{where}.item_id"),
                logical_path=_path(item["logical_path"], f"{where}.logical_path"),
                source_copy_path=_path(
                    item["source_copy_path"], f"{where}.source_copy_path"
                ),
                implementation_path=_path(
                    item["implementation_path"], f"{where}.implementation_path"
                ),
                wrapper_path=wrapper,
                transform_labels=_wire_u32_labels(
                    item["transform_labels"], f"{where}.transform_labels"
                ),
            )
        )
    raw_stubs = value["source_stubs"]
    if not isinstance(raw_stubs, list):
        raise ObservationError("replacement metadata source_stubs must be an array")
    stubs: list[SourceStub] = []
    for index, item in enumerate(raw_stubs):
        where = f"replacement metadata source_stubs[{index}]"
        item = _exact_object(item, {"item_id", "path"}, where)
        stubs.append(
            SourceStub(
                item_id=_wire_integer(item["item_id"], f"{where}.item_id"),
                path=_path(item["path"], f"{where}.path"),
            )
        )
    if any(left.item_id >= right.item_id for left, right in pairwise(stubs)):
        raise ObservationError(
            "replacement metadata source_stubs must be sorted by unique item ID"
        )
    raw_unfilled = value["unfilled_stub_items"]
    if not isinstance(raw_unfilled, list):
        raise ObservationError(
            "replacement metadata unfilled_stub_items must be an array"
        )
    unfilled = tuple(
        _wire_integer(item, "replacement metadata unfilled_stub_items entry")
        for item in raw_unfilled
    )
    if any(left >= right for left, right in pairwise(unfilled)):
        raise ObservationError(
            "replacement metadata unfilled_stub_items must be sorted by unique item ID"
        )
    return ReplacementMetadata(
        **digests,
        accepted_correspondence=correspondence_lists["accepted_correspondence"],
        new_correspondence=correspondence_lists["new_correspondence"],
        current_items=tuple(current),
        source_stubs=tuple(stubs),
        dependency_correspondence=correspondence_lists["dependency_correspondence"],
        unfilled_stub_items=unfilled,
    )


@dataclass(frozen=True)
class PointerVariableOrigin:
    kind: str
    value: int


@dataclass(frozen=True)
class PointerVariableMetadata:
    name: str
    origin: PointerVariableOrigin
    before_type: str
    selected_target_type: str
    before_type_is_inferred: bool


@dataclass(frozen=True)
class PrintfTemplateMetadata:
    rust_format: str
    argument_count: int


@dataclass(frozen=True)
class StatementPairMetadata:
    label: int
    before_statement: str
    pointer_variables_complete: bool
    pointer_variables: tuple[PointerVariableMetadata, ...]
    printf_template: PrintfTemplateMetadata | None = None


@dataclass(frozen=True)
class StatementDisposition:
    label: int
    disposition: str
    children: tuple[StatementDisposition, ...]


@dataclass(frozen=True)
class SkeletonView:
    skeleton: str
    needs_transformation: bool
    statement_dispositions: tuple[StatementDisposition, ...]
    statement_pair_metadata: tuple[StatementPairMetadata, ...]

    @property
    def transform_labels(self) -> tuple[int, ...]:
        def visit(nodes: tuple[StatementDisposition, ...]) -> tuple[int, ...]:
            labels: list[int] = []
            for node in nodes:
                if node.disposition == "transform":
                    labels.append(node.label)
                labels.extend(visit(node.children))
            return tuple(labels)

        return visit(self.statement_dispositions)

    @property
    def report_labels(self) -> tuple[int, ...]:
        return tuple(
            node.label
            for node in _walk_dispositions(self.statement_dispositions)
            if node.disposition in {"transform", "mechanical"}
        )

    @property
    def contains_rule_application(self) -> bool:
        def visit(nodes: tuple[StatementDisposition, ...]) -> bool:
            return any(
                node.disposition == "rule_applied" or visit(node.children)
                for node in nodes
            )

        return visit(self.statement_dispositions)


@dataclass(frozen=True)
class ItemRecord:
    id: int
    path: str
    kind: str
    dependencies: tuple[int, ...]
    signature_dependencies: tuple[int, ...] = ()
    name: str | None = None
    annotated_source: str | None = None
    baseline: SkeletonView | None = None
    applied: SkeletonView | None = None
    source_signature: str | None = None
    target_signature: str | None = None
    printf_format_specifiers: tuple[str, ...] = ()
    proctor_libc_function_paths: tuple[str, ...] = ()
    foreign_function_names: tuple[str, ...] = ()
    foreign_static_names: tuple[str, ...] = ()
    declaration: str | None = None
    definition: str | None = None

    @property
    def is_function(self) -> bool:
        return self.kind == "Fn"


def _integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkeletonError(f"{where} must be an integer")
    if not 0 <= value <= U64_MAX:
        raise SkeletonError(f"{where} must be in the u64 range")
    return value


def _u32_labels(value: Any, where: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise SkeletonError(f"{where} must be an array")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise SkeletonError(f"{where} entries must be integers")
        if not 0 <= item <= U32_MAX:
            raise SkeletonError(f"{where} entries must be in the u32 range")
        result.append(item)
    for previous, current in pairwise(result):
        if current <= previous:
            detail = "duplicate" if current == previous else "out-of-order"
            raise SkeletonError(f"{where} has {detail} label {current}")
    return tuple(result)


def _string(data: dict[str, Any], key: str, record_id: int) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise SkeletonError(f"record {record_id} field {key!r} must be a string")
    if key in {"path", "name"} and not value:
        raise SkeletonError(f"record {record_id} field {key!r} must not be empty")
    return value


def _dependencies(data: dict[str, Any], key: str, record_id: int) -> tuple[int, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise SkeletonError(f"record {record_id} field {key!r} must be an array")
    result = tuple(
        _integer(item, f"record {record_id} field {key!r} entry") for item in value
    )
    for previous, current in pairwise(result):
        if current <= previous:
            detail = "duplicate" if current == previous else "out-of-order"
            raise SkeletonError(
                f"record {record_id} field {key!r} has {detail} dependency {current}"
            )
    return result


def _foreign_names(data: dict[str, Any], key: str, record_id: int) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        raise SkeletonError(f"record {record_id} field {key!r} must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SkeletonError(
                f"record {record_id} field {key!r} entries must be strings"
            )
        if not item:
            raise SkeletonError(
                f"record {record_id} field {key!r} entries must not be empty"
            )
        result.append(item)
    for previous, current in pairwise(result):
        if current <= previous:
            detail = "duplicate" if current == previous else "out-of-order"
            raise SkeletonError(
                f"record {record_id} field {key!r} has {detail} name {current!r}"
            )
    return tuple(result)


def _u32(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkeletonError(f"{where} must be an integer")
    if not 0 <= value <= U32_MAX:
        raise SkeletonError(f"{where} must be in the u32 range")
    return value


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise SkeletonError(f"{where} must be a string")
    if not value:
        raise SkeletonError(f"{where} must not be empty")
    return value


def _pointer_origin(value: Any, where: str) -> PointerVariableOrigin:
    if not isinstance(value, dict):
        raise SkeletonError(f"{where} must be an object")
    kind = value.get("kind")
    if kind == "parameter":
        if set(value) != {"kind", "index"}:
            raise SkeletonError(
                f"{where} parameter origin must contain exactly ['index', 'kind']"
            )
        return PointerVariableOrigin(
            kind="parameter",
            value=_u32(value["index"], f"{where}.index"),
        )
    if kind == "local":
        if set(value) != {"kind", "declaration_label"}:
            raise SkeletonError(
                f"{where} local origin must contain exactly "
                "['declaration_label', 'kind']"
            )
        return PointerVariableOrigin(
            kind="local",
            value=_u32(
                value["declaration_label"],
                f"{where}.declaration_label",
            ),
        )
    raise SkeletonError(f"{where}.kind must be 'parameter' or 'local'")


def _statement_pair_metadata(
    value: Any,
    record_id: int,
    expected_labels: tuple[int, ...],
    *,
    field: str = "statement_pair_metadata",
) -> tuple[StatementPairMetadata, ...]:
    where = f"record {record_id} {field}"
    if not isinstance(value, list):
        raise SkeletonError(f"{where} must be an array")
    result: list[StatementPairMetadata] = []
    for statement_index, statement in enumerate(value):
        statement_where = f"{where}[{statement_index}]"
        if not isinstance(statement, dict):
            raise SkeletonError(f"{statement_where} must be an object")
        expected_statement_keys = {
            "label",
            "before_statement",
            "printf_template",
            "pointer_variables_complete",
            "pointer_variables",
        }
        if set(statement) != expected_statement_keys:
            raise SkeletonError(
                f"{statement_where} must contain exactly "
                f"{sorted(expected_statement_keys)}"
            )
        label = _u32(statement["label"], f"{statement_where}.label")
        before_statement = _nonempty_string(
            statement["before_statement"],
            f"{statement_where}.before_statement",
        )
        if before_statement.endswith(("\r", "\n")):
            raise SkeletonError(
                f"{statement_where}.before_statement must not end in a newline"
            )
        printf_template_value = statement["printf_template"]
        printf_template: PrintfTemplateMetadata | None
        if printf_template_value is None:
            printf_template = None
        else:
            if not isinstance(printf_template_value, dict) or set(
                printf_template_value
            ) != {"rust_format", "argument_count"}:
                raise SkeletonError(
                    f"{statement_where}.printf_template must be null or contain exactly "
                    "['argument_count', 'rust_format']"
                )
            rust_format = printf_template_value["rust_format"]
            if not isinstance(rust_format, str):
                raise SkeletonError(
                    f"{statement_where}.printf_template.rust_format must be a string"
                )
            printf_template = PrintfTemplateMetadata(
                rust_format=rust_format,
                argument_count=_u32(
                    printf_template_value["argument_count"],
                    f"{statement_where}.printf_template.argument_count",
                ),
            )
        complete = statement["pointer_variables_complete"]
        if not isinstance(complete, bool):
            raise SkeletonError(
                f"{statement_where}.pointer_variables_complete must be a Boolean"
            )
        variables_value = statement["pointer_variables"]
        if not isinstance(variables_value, list):
            raise SkeletonError(f"{statement_where}.pointer_variables must be an array")
        variables: list[PointerVariableMetadata] = []
        origins: set[tuple[str, int]] = set()
        for variable_index, variable in enumerate(variables_value):
            variable_where = f"{statement_where}.pointer_variables[{variable_index}]"
            if not isinstance(variable, dict):
                raise SkeletonError(f"{variable_where} must be an object")
            expected_variable_keys = {
                "name",
                "origin",
                "before_type",
                "selected_target_type",
                "before_type_is_inferred",
            }
            if set(variable) != expected_variable_keys:
                raise SkeletonError(
                    f"{variable_where} must contain exactly "
                    f"{sorted(expected_variable_keys)}"
                )
            name = _nonempty_string(variable["name"], f"{variable_where}.name")
            if "\r" in name or "\n" in name:
                raise SkeletonError(f"{variable_where}.name must not contain a newline")
            origin = _pointer_origin(variable["origin"], f"{variable_where}.origin")
            origin_key = (origin.kind, origin.value)
            if origin_key in origins:
                raise SkeletonError(
                    f"{statement_where} has duplicate pointer-variable origin "
                    f"{origin.kind} {origin.value}"
                )
            origins.add(origin_key)
            before_type = _nonempty_string(
                variable["before_type"],
                f"{variable_where}.before_type",
            )
            selected_target_type = _nonempty_string(
                variable["selected_target_type"],
                f"{variable_where}.selected_target_type",
            )
            inferred = variable["before_type_is_inferred"]
            if not isinstance(inferred, bool):
                raise SkeletonError(
                    f"{variable_where}.before_type_is_inferred must be a Boolean"
                )
            variables.append(
                PointerVariableMetadata(
                    name=name,
                    origin=origin,
                    before_type=before_type,
                    selected_target_type=selected_target_type,
                    before_type_is_inferred=inferred,
                )
            )
        result.append(
            StatementPairMetadata(
                label=label,
                before_statement=before_statement,
                printf_template=printf_template,
                pointer_variables_complete=complete,
                pointer_variables=tuple(variables),
            )
        )
    labels = tuple(statement.label for statement in result)
    if labels != expected_labels:
        raise SkeletonError(
            f"{where} labels must exactly match recursive transform labels "
            "in depth-first order"
        )
    return tuple(result)


def _statement_dispositions(
    value: Any, record_id: int, view_name: str
) -> tuple[StatementDisposition, ...]:
    where = f"record {record_id} {view_name}.statement_dispositions"
    seen: set[int] = set()

    def load_nodes(raw: Any, node_where: str) -> tuple[StatementDisposition, ...]:
        if not isinstance(raw, list):
            raise SkeletonError(f"{node_where} must be an array")
        result: list[StatementDisposition] = []
        for index, item in enumerate(raw):
            item_where = f"{node_where}[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "label",
                "disposition",
                "children",
            }:
                raise SkeletonError(
                    f"{item_where} must contain exactly "
                    "['children', 'disposition', 'label']"
                )
            label = _u32(item["label"], f"{item_where}.label")
            if label in seen:
                raise SkeletonError(f"{where} has duplicate label {label}")
            seen.add(label)
            disposition = item["disposition"]
            if disposition not in {
                "preserve",
                "preserve_shell",
                "transform",
                "rule_applied",
                "mechanical",
            }:
                raise SkeletonError(
                    f"{item_where}.disposition must be 'preserve', "
                    "'preserve_shell', 'transform', 'rule_applied', or 'mechanical'"
                )
            children = load_nodes(item["children"], f"{item_where}.children")
            if disposition in {"preserve", "mechanical"} and any(
                descendant.disposition != "preserve"
                for descendant in _walk_dispositions(children)
            ):
                raise SkeletonError(
                    f"{item_where} {disposition} node has a non-preserve descendant"
                )
            result.append(
                StatementDisposition(
                    label=label,
                    disposition=disposition,
                    children=children,
                )
            )
        return tuple(result)

    result = load_nodes(value, where)
    labels = tuple(node.label for node in _walk_dispositions(result))
    for previous, current in pairwise(labels):
        if current <= previous:
            detail = "duplicate" if current == previous else "out-of-order"
            raise SkeletonError(f"{where} has {detail} label {current}")
    return result


def _walk_dispositions(
    nodes: tuple[StatementDisposition, ...],
) -> tuple[StatementDisposition, ...]:
    result: list[StatementDisposition] = []
    for node in nodes:
        result.append(node)
        result.extend(_walk_dispositions(node.children))
    return tuple(result)


def _load_skeleton_view(value: Any, record_id: int, view_name: str) -> SkeletonView:
    where = f"record {record_id} {view_name}"
    if not isinstance(value, dict) or set(value) != {
        "skeleton",
        "needs_transformation",
        "statement_dispositions",
        "statement_pair_metadata",
    }:
        raise SkeletonError(
            f"{where} must contain exactly ['needs_transformation', 'skeleton', "
            "'statement_dispositions', 'statement_pair_metadata']"
        )
    skeleton = _nonempty_string(value["skeleton"], f"{where}.skeleton")
    needs = value["needs_transformation"]
    if not isinstance(needs, bool):
        raise SkeletonError(f"{where}.needs_transformation must be a Boolean")
    dispositions = _statement_dispositions(
        value["statement_dispositions"], record_id, view_name
    )
    transform_labels = tuple(
        node.label
        for node in _walk_dispositions(dispositions)
        if node.disposition == "transform"
    )
    if needs != bool(transform_labels):
        raise SkeletonError(
            f"{where}.needs_transformation is inconsistent with its dispositions"
        )
    metadata = _statement_pair_metadata(
        value["statement_pair_metadata"],
        record_id,
        tuple(
            node.label
            for node in _walk_dispositions(dispositions)
            if node.disposition in {"transform", "mechanical"}
        ),
        field=f"{view_name}.statement_pair_metadata",
    )
    view = SkeletonView(
        skeleton=skeleton,
        needs_transformation=needs,
        statement_dispositions=dispositions,
        statement_pair_metadata=metadata,
    )
    _validate_printf_template_metadata(record_id, view_name, view)
    return view


def _view_topology(view: SkeletonView) -> tuple[tuple[int, tuple[int, ...]], ...]:
    return tuple(
        (node.label, tuple(child.label for child in node.children))
        for node in _walk_dispositions(view.statement_dispositions)
    )


def _implicit_format_argument_count(value: str) -> int | None:
    index = 0
    count = 0
    while index < len(value):
        if value.startswith("{{", index) or value.startswith("}}", index):
            index += 2
        elif value[index] == "{":
            close = value.find("}", index + 1)
            if close == -1:
                return None
            field = value[index + 1 : close]
            if field and not field.startswith(":"):
                return None
            count += 1
            index = close + 1
        elif value[index] == "}":
            return None
        else:
            index += 1
    return count


def _validate_printf_template_metadata(
    record_id: int, view_name: str, view: SkeletonView
) -> None:
    dispositions = {
        node.label: node.disposition
        for node in _walk_dispositions(view.statement_dispositions)
    }
    for metadata in view.statement_pair_metadata:
        where = f"record {record_id} {view_name} printf template label {metadata.label}"
        template = metadata.printf_template
        disposition = dispositions[metadata.label]
        if template is None:
            if disposition == "mechanical":
                raise SkeletonError(f"{where} has no trusted printf metadata")
            continue
        if (
            _implicit_format_argument_count(template.rust_format)
            != template.argument_count
        ):
            raise SkeletonError(f"{where} contradicts its trusted metadata")
        if (disposition == "mechanical") != (template.argument_count == 0):
            raise SkeletonError(f"{where} argument count contradicts its disposition")


def _validate_cross_view_invariants(
    record_id: int, baseline: SkeletonView, applied: SkeletonView
) -> None:
    baseline_nodes = _walk_dispositions(baseline.statement_dispositions)
    applied_nodes = _walk_dispositions(applied.statement_dispositions)
    if any(node.disposition == "rule_applied" for node in baseline_nodes):
        raise SkeletonError(f"record {record_id} baseline contains rule_applied")
    if _view_topology(baseline) != _view_topology(applied):
        raise SkeletonError(
            f"record {record_id} baseline/applied label topology differs"
        )
    for before, after in zip(baseline_nodes, applied_nodes, strict=True):
        if before.disposition == "preserve" and after.disposition != "preserve":
            raise SkeletonError(
                f"record {record_id} applied view changes preserved label {before.label}"
            )
        if (
            before.disposition == "preserve_shell"
            and after.disposition != "preserve_shell"
        ):
            raise SkeletonError(
                f"record {record_id} applied view changes preserved-shell label "
                f"{before.label}"
            )
        if (before.disposition == "mechanical") != (after.disposition == "mechanical"):
            raise SkeletonError(
                f"record {record_id} applied view changes mechanical label {before.label}"
            )
        if before.disposition == "mechanical":
            baseline_metadata = next(
                metadata
                for metadata in baseline.statement_pair_metadata
                if metadata.label == before.label
            )
            applied_metadata = next(
                metadata
                for metadata in applied.statement_pair_metadata
                if metadata.label == after.label
            )
            if baseline_metadata != applied_metadata:
                raise SkeletonError(
                    f"record {record_id} mechanical label {before.label} "
                    "has different report metadata between skeleton views"
                )
        if after.disposition == "rule_applied" and before.disposition != "transform":
            raise SkeletonError(
                f"record {record_id} rule-applied label {after.label} was not transformable"
            )
        if before.disposition == "transform" and after.disposition in {
            "preserve",
            "preserve_shell",
            "mechanical",
        }:
            raise SkeletonError(
                f"record {record_id} applied view preserves transformable label "
                f"{after.label}"
            )
        if before.disposition == "transform" and after.disposition == "transform":
            baseline_template = next(
                metadata.printf_template
                for metadata in baseline.statement_pair_metadata
                if metadata.label == before.label
            )
            applied_template = next(
                metadata.printf_template
                for metadata in applied.statement_pair_metadata
                if metadata.label == after.label
            )
            if baseline_template != applied_template:
                raise SkeletonError(
                    f"record {record_id} printf metadata differs between skeleton views "
                    f"at label {before.label}"
                )


def _load_record(data: Any, index: int) -> ItemRecord:
    if not isinstance(data, dict):
        raise SkeletonError(f"record {index} must be an object")
    record_id = _integer(data.get("id"), f"record {index} id")
    path = _string(data, "path", record_id)
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in ALL_KINDS:
        raise SkeletonError(f"record {record_id} has unknown kind {kind!r}")
    if kind == "Fn":
        required = {
            "id",
            "path",
            "kind",
            "name",
            "annotated_source",
            "baseline",
            "applied",
            "source_signature",
            "target_signature",
            "printf_format_specifiers",
            "proctor_libc_function_paths",
            "foreign_function_names",
            "foreign_static_names",
            "signature_dependencies",
            "dependencies",
        }
        missing = sorted(required - set(data))
        if missing:
            raise SkeletonError(
                f"record {record_id} is missing required fields {missing}"
            )
        unknown = sorted(set(data) - required)
        if unknown:
            raise SkeletonError(f"record {record_id} contains unknown fields {unknown}")
        dependencies = _dependencies(data, "dependencies", record_id)
        signature_dependencies = _dependencies(
            data, "signature_dependencies", record_id
        )
        baseline = _load_skeleton_view(data["baseline"], record_id, "baseline")
        applied = _load_skeleton_view(data["applied"], record_id, "applied")
        _validate_cross_view_invariants(record_id, baseline, applied)
        return ItemRecord(
            id=record_id,
            path=path,
            kind=kind,
            name=_string(data, "name", record_id),
            annotated_source=_string(data, "annotated_source", record_id),
            baseline=baseline,
            applied=applied,
            source_signature=_string(data, "source_signature", record_id),
            target_signature=_string(data, "target_signature", record_id),
            printf_format_specifiers=_foreign_names(
                data, "printf_format_specifiers", record_id
            ),
            proctor_libc_function_paths=_foreign_names(
                data, "proctor_libc_function_paths", record_id
            ),
            foreign_function_names=_foreign_names(
                data, "foreign_function_names", record_id
            ),
            foreign_static_names=_foreign_names(
                data, "foreign_static_names", record_id
            ),
            signature_dependencies=signature_dependencies,
            dependencies=dependencies,
        )
    if kind in {"Static", "Const"}:
        required = {
            "id",
            "path",
            "kind",
            "declaration",
            "signature_dependencies",
            "dependencies",
        }
        if set(data) != required:
            raise SkeletonError(
                f"record {record_id} must contain exactly {sorted(required)}"
            )
        declaration = _string(data, "declaration", record_id)
        dependencies = _dependencies(data, "dependencies", record_id)
        signature_dependencies = _dependencies(
            data, "signature_dependencies", record_id
        )
        return ItemRecord(
            id=record_id,
            path=path,
            kind=kind,
            declaration=declaration,
            signature_dependencies=signature_dependencies,
            dependencies=dependencies,
        )
    required = {"id", "path", "kind", "definition", "dependencies"}
    if set(data) != required:
        raise SkeletonError(
            f"record {record_id} must contain exactly {sorted(required)}"
        )
    return ItemRecord(
        id=record_id,
        path=path,
        kind=kind,
        definition=_string(data, "definition", record_id),
        dependencies=_dependencies(data, "dependencies", record_id),
    )


def load_skeletons(text: str) -> tuple[ItemRecord, ...]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkeletonError(f"skeleton JSON decode failure: {exc}") from exc
    if not isinstance(raw, list):
        raise SkeletonError("skeleton JSON top level must be an array")
    records = tuple(_load_record(value, index) for index, value in enumerate(raw))
    by_id: dict[int, ItemRecord] = {}
    namespace_paths: set[tuple[str, str]] = set()
    for record in records:
        if record.id in by_id:
            raise SkeletonError(f"duplicate record id {record.id}")
        by_id[record.id] = record
        namespace = "value" if record.kind in VALUE_KINDS else "type"
        key = (namespace, record.path)
        if key in namespace_paths:
            raise SkeletonError(f"duplicate {namespace}-namespace path {record.path!r}")
        namespace_paths.add(key)
    for record in records:
        for field, dependencies in (
            ("signature_dependencies", record.signature_dependencies),
            ("dependencies", record.dependencies),
        ):
            for dependency in dependencies:
                if dependency not in by_id:
                    raise SkeletonError(
                        f"record {record.id} field {field!r} has unresolved dependency "
                        f"{dependency}"
                    )
        if not set(record.signature_dependencies).issubset(record.dependencies):
            raise SkeletonError(
                f"record {record.id} signature_dependencies must be a subset of "
                "dependencies"
            )
    return records


def function_graph(records: tuple[ItemRecord, ...]) -> dict[int, set[int]]:
    functions = {record.id: record for record in records if record.is_function}
    return {
        item_id: {
            dependency for dependency in record.dependencies if dependency in functions
        }
        for item_id, record in sorted(functions.items())
    }


def strongly_connected_components(
    graph: dict[int, set[int]],
) -> tuple[tuple[int, ...], ...]:
    index = 0
    indices: dict[int, int] = {}
    lowlinks: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    result: list[tuple[int, ...]] = []

    def visit(node: int) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in sorted(graph[node]):
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] == indices[node]:
            members: list[int] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                members.append(member)
                if member == node:
                    break
            result.append(tuple(sorted(members)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return tuple(sorted(result, key=lambda members: members[0]))


def render_dependency_entry(record: ItemRecord) -> str:
    name = record.path.rsplit("::", 1)[-1]
    if record.kind == "Fn":
        return (
            f"### Function `{name}`\n\n"
            "Source signature:\n"
            f"```rust\n{record.source_signature}\n```\n"
            "Target signature:\n"
            f"```rust\n{record.target_signature}\n```"
        )
    text = (
        record.declaration if record.kind in {"Static", "Const"} else record.definition
    )
    return f"### {record.kind} `{name}`\n\n```rust\n{text}\n```"


def render_transformation_targets(
    members: tuple[int, ...],
    records_by_id: dict[int, ItemRecord],
    views_by_id: dict[int, SkeletonView] | None = None,
) -> str:
    entries = []
    for item_id in sorted(members):
        record = records_by_id[item_id]
        view = views_by_id[item_id] if views_by_id is not None else record.applied
        if view is None:
            raise SkeletonError(
                f"function record {item_id} has no selected skeleton view"
            )
        foreign_references = ""
        if record.foreign_function_names:
            names = ", ".join(f"`{name}`" for name in record.foreign_function_names)
            foreign_references = f"Foreign function references: {names}\n\n"
        if record.foreign_static_names:
            names = ", ".join(f"`{name}`" for name in record.foreign_static_names)
            foreign_references += f"Foreign static references: {names}\n\n"
        entries.append(
            f"### Function `{record.name}`\n\n"
            f"{foreign_references}"
            f"Source:\n```rust\n{record.annotated_source}\n```\n"
            f"Target skeleton:\n```rust\n{view.skeleton}\n```"
        )
    return "\n\n".join(entries)


def dependency_context(
    members: tuple[int, ...],
    records_by_id: dict[int, ItemRecord],
    *,
    limit: int = 100_000,
) -> tuple[str, tuple[int, ...]]:
    members = tuple(sorted(members))
    member_set = set(members)
    recursive_singleton = (
        len(members) == 1 and members[0] in records_by_id[members[0]].dependencies
    )
    scc_signatures = set(members) if len(members) > 1 or recursive_singleton else set()
    direct = {
        dependency
        for member in members
        for dependency in records_by_id[member].dependencies
        if dependency not in member_set
    }
    selected = scc_signatures | direct

    def render(ids: set[int]) -> str:
        return "\n\n".join(
            render_dependency_entry(records_by_id[item_id]) for item_id in sorted(ids)
        )

    mandatory = render(selected)
    if len(mandatory) > limit:
        raise ContextOverflow(
            f"SCC {','.join(map(str, members))} mandatory dependency context has "
            f"{len(mandatory)} characters, exceeding limit {limit}"
        )
    frontier = set(direct)
    while frontier:
        next_depth: set[int] = set()
        for item_id in sorted(frontier):
            record = records_by_id[item_id]
            edges = (
                record.signature_dependencies
                if record.kind in VALUE_KINDS
                else record.dependencies
            )
            next_depth.update(edges)
        next_depth -= selected
        next_depth -= member_set
        if not next_depth:
            break
        tentative = selected | next_depth
        if len(render(tentative)) > limit:
            break
        selected = tentative
        frontier = next_depth
    return render(selected), tuple(sorted(selected))
