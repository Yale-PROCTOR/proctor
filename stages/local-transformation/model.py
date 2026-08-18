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
class ReplacementMetadata:
    candidate_sha256: str
    statement_pairs_sha256: str
    observation_source_sha256: str
    accepted_correspondence: tuple[CallableCorrespondence, ...]
    new_correspondence: tuple[CallableCorrespondence, ...]
    current_items: tuple[CurrentObservationItem, ...]


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


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
    value = _exact_object(
        value,
        {
            "schema_version",
            "candidate_sha256",
            "statement_pairs_sha256",
            "observation_source_sha256",
            "accepted_correspondence",
            "new_correspondence",
            "current_items",
        },
        "replacement metadata",
    )
    if isinstance(value["schema_version"], bool) or value["schema_version"] != 1:
        raise ObservationError(
            f"unsupported replacement metadata schema_version {value['schema_version']!r}"
        )
    digests: dict[str, str] = {}
    for key in (
        "candidate_sha256",
        "statement_pairs_sha256",
        "observation_source_sha256",
    ):
        digest = value[key]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ObservationError(
                f"replacement metadata {key} must be 64 lowercase hexadecimal digits"
            )
        digests[key] = digest
    correspondence_lists: dict[str, tuple[CallableCorrespondence, ...]] = {}
    for key in ("accepted_correspondence", "new_correspondence"):
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
    return ReplacementMetadata(
        **digests,
        accepted_correspondence=correspondence_lists["accepted_correspondence"],
        new_correspondence=correspondence_lists["new_correspondence"],
        current_items=tuple(current),
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
    foreign_function_names: tuple[str, ...] = ()
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


def _foreign_function_names(data: dict[str, Any], record_id: int) -> tuple[str, ...]:
    value = data.get("foreign_function_names")
    if not isinstance(value, list):
        raise SkeletonError(
            f"record {record_id} field 'foreign_function_names' must be an array"
        )
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SkeletonError(
                f"record {record_id} field 'foreign_function_names' entries "
                "must be strings"
            )
        if not item:
            raise SkeletonError(
                f"record {record_id} field 'foreign_function_names' entries "
                "must not be empty"
            )
        result.append(item)
    for previous, current in pairwise(result):
        if current <= previous:
            detail = "duplicate" if current == previous else "out-of-order"
            raise SkeletonError(
                f"record {record_id} field 'foreign_function_names' has {detail} "
                f"name {current!r}"
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


@dataclass(frozen=True)
class _SkeletonStatementShape:
    label: int
    parent: int | None
    root: str
    declaration: tuple[str, ...]
    control: tuple[Any, ...]
    child_slots: tuple[tuple[int, str], ...]
    payload: tuple[str, ...]


@dataclass(frozen=True)
class _SkeletonShape:
    signature: tuple[str, ...]
    statements: tuple[_SkeletonStatementShape, ...]


def _decode_canonical_rust_string(token: str, where: str) -> str:
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        raise SkeletonError(f"{where} format must be one ordinary Rust string literal")
    result: list[str] = []
    value = token[1:-1]
    index = 0
    simple = {
        '"': '"',
        "'": "'",
        "\\": "\\",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "0": "\0",
    }
    while index < len(value):
        character = value[index]
        if character != "\\":
            if character in "\r\n":
                raise SkeletonError(f"{where} format literal is not canonical")
            result.append(character)
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise SkeletonError(f"{where} format literal has an incomplete escape")
        escape = value[index]
        if escape in simple:
            result.append(simple[escape])
            index += 1
            continue
        if escape == "x" and index + 2 < len(value):
            digits = value[index + 1 : index + 3]
            try:
                result.append(chr(int(digits, 16)))
            except ValueError as error:
                raise SkeletonError(
                    f"{where} format literal has an invalid byte escape"
                ) from error
            index += 3
            continue
        if escape == "u" and index + 1 < len(value) and value[index + 1] == "{":
            close = value.find("}", index + 2)
            if close == -1:
                raise SkeletonError(
                    f"{where} format literal has an incomplete Unicode escape"
                )
            digits = value[index + 2 : close].replace("_", "")
            try:
                codepoint = int(digits, 16)
                if not digits or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                    raise ValueError
                result.append(chr(codepoint))
            except ValueError as error:
                raise SkeletonError(
                    f"{where} format literal has an invalid Unicode escape"
                ) from error
            index = close + 1
            continue
        raise SkeletonError(f"{where} format literal has an unsupported escape")
    return "".join(result)


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


def _printf_template_payload(
    payload: tuple[str, ...], where: str
) -> tuple[str, int] | None:
    prefix = ("::", "std", "::", "print", "!", "(")
    if len(payload) < 9 or payload[:6] != prefix or payload[-2:] != (")", ";"):
        return None
    body = payload[6:-2]
    rust_format = _decode_canonical_rust_string(body[0], where)
    remaining = body[1:]
    count = 0
    while remaining:
        if len(remaining) < 5 or remaining[:5] != (",", "todo", "!", "(", ")"):
            raise SkeletonError(
                f"{where} value arguments must be exact todo!() placeholders"
            )
        count += 1
        remaining = remaining[5:]
    return rust_format, count


def _validate_printf_template_metadata(
    record_id: int, view_name: str, view: SkeletonView
) -> None:
    shape = _skeleton_shape(view.skeleton, record_id, view_name)
    dispositions = {
        node.label: node.disposition
        for node in _walk_dispositions(view.statement_dispositions)
    }
    shapes = {statement.label: statement for statement in shape.statements}
    for metadata in view.statement_pair_metadata:
        where = f"record {record_id} {view_name} printf template label {metadata.label}"
        statement = shapes.get(metadata.label)
        if statement is None:
            raise SkeletonError(
                f"record {record_id} {view_name} skeleton labels do not match its "
                "disposition topology"
            )
        looks_like_print = statement.payload[:6] == (
            "::",
            "std",
            "::",
            "print",
            "!",
            "(",
        )
        if metadata.printf_template is None:
            if looks_like_print:
                raise SkeletonError(f"{where} has no trusted printf metadata")
            continue
        parsed = _printf_template_payload(statement.payload, where)
        if parsed is None:
            raise SkeletonError(f"{where} is not one canonical print template")
        rust_format, argument_count = parsed
        if (
            rust_format != metadata.printf_template.rust_format
            or argument_count != metadata.printf_template.argument_count
            or _implicit_format_argument_count(rust_format) != argument_count
        ):
            raise SkeletonError(f"{where} contradicts its trusted metadata")
        disposition = dispositions[metadata.label]
        if (disposition == "mechanical") != (argument_count == 0):
            raise SkeletonError(f"{where} argument count contradicts its disposition")


_RUST_MULTI_PUNCTUATION = (
    "<<=",
    ">>=",
    "...",
    "..=",
    "::",
    "->",
    "=>",
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "|=",
    "^=",
    "<<",
    ">>",
    "..",
)


def _rust_tokens(source: str, where: str) -> tuple[str, ...]:
    """Lex enough Rust to compare closed expected-skeleton structure."""
    tokens: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            depth = 1
            cursor = index + 2
            while cursor < length and depth:
                if source.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                elif source.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                else:
                    cursor += 1
            if depth:
                raise SkeletonError(f"{where} has an unterminated block comment")
            index = cursor
            continue

        raw_prefix = re.match(r"(?:br|r)(?P<hashes>#+)?\"", source[index:])
        if raw_prefix is not None:
            prefix = raw_prefix.group(0)
            hashes = raw_prefix.group("hashes") or ""
            terminator = '"' + hashes
            end = source.find(terminator, index + len(prefix))
            if end < 0:
                raise SkeletonError(f"{where} has an unterminated raw string")
            tokens.append(source[index : end + len(terminator)])
            index = end + len(terminator)
            continue

        string_prefix = next(
            (
                prefix
                for prefix in ('b"', 'c"', '"')
                if source.startswith(prefix, index)
            ),
            None,
        )
        if string_prefix is not None:
            cursor = index + len(string_prefix)
            escaped = False
            while cursor < length:
                current = source[cursor]
                cursor += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            else:
                raise SkeletonError(f"{where} has an unterminated string")
            tokens.append(source[index:cursor])
            index = cursor
            continue

        if character == "'":
            character_literal = re.match(r"'(?:\\.|[^'\\])+'", source[index:])
            if character_literal is not None:
                token = character_literal.group(0)
                tokens.append(token)
                index += len(token)
                continue
            lifetime = re.match(r"'[A-Za-z_][A-Za-z0-9_]*", source[index:])
            if lifetime is not None:
                token = lifetime.group(0)
                tokens.append(token)
                index += len(token)
                continue
        identifier = re.match(r"(?:r#)?[A-Za-z_][A-Za-z0-9_]*", source[index:])
        if identifier is not None:
            token = identifier.group(0)
            tokens.append(token)
            index += len(token)
            continue
        number = re.match(r"(?:0[xob][0-9A-Fa-f_]+|[0-9][0-9A-Za-z_]*)", source[index:])
        if number is not None:
            token = number.group(0)
            tokens.append(token)
            index += len(token)
            continue
        punctuation = next(
            (
                candidate
                for candidate in _RUST_MULTI_PUNCTUATION
                if source.startswith(candidate, index)
            ),
            None,
        )
        if punctuation is not None:
            tokens.append(punctuation)
            index += len(punctuation)
            continue
        tokens.append(character)
        index += 1
    return tuple(tokens)


def _matching_delimiter(
    tokens: tuple[str, ...], opening: int, limit: int, where: str
) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    opener = tokens[opening]
    if opener not in pairs:
        raise SkeletonError(f"{where} has malformed delimiter structure")
    stack = [pairs[opener]]
    for index in range(opening + 1, limit):
        token = tokens[index]
        if token in pairs:
            stack.append(pairs[token])
        elif token in pairs.values():
            if not stack or token != stack.pop():
                raise SkeletonError(f"{where} has unbalanced delimiters")
            if not stack:
                return index
    raise SkeletonError(f"{where} has an unterminated delimiter")


def _top_level_token(
    tokens: tuple[str, ...], start: int, stop: int, wanted: set[str]
) -> int | None:
    closing = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for index in range(start, stop):
        token = tokens[index]
        if not stack and token in wanted:
            return index
        if token in closing:
            stack.append(closing[token])
        elif token in closing.values():
            if stack and token == stack[-1]:
                stack.pop()
    return None


def _control_details(
    tokens: tuple[str, ...], start: int, limit: int, where: str
) -> tuple[int, str, tuple[str, ...], tuple[tuple[str, int, int], ...]]:
    root = tokens[start]
    if root == "{" or root in {"if", "while", "for", "loop", "match"}:
        opening = (
            start if root == "{" else _top_level_token(tokens, start + 1, limit, {"{"})
        )
        if opening is None:
            raise SkeletonError(f"{where} has a control without a body")
        closing = _matching_delimiter(tokens, opening, limit, where)
    else:
        raise SkeletonError(f"{where} is not a supported control root")

    if root == "if":
        binding: tuple[str, ...] = ()
        kind = "if"
        if start + 1 < opening and tokens[start + 1] == "let":
            equals = _top_level_token(tokens, start + 2, opening, {"="})
            if equals is None:
                raise SkeletonError(f"{where} has malformed if-let syntax")
            kind = "if_let"
            binding = tokens[start + 2 : equals]
        slots: list[tuple[str, int, int]] = [("then", opening + 1, closing)]
        layout: list[str] = [kind]
        end = closing + 1
        if end < limit and tokens[end] == "else":
            end += 1
            if end >= limit:
                raise SkeletonError(f"{where} has malformed else syntax")
            if tokens[end] == "if":
                nested_end, nested_layout, nested_binding, nested_slots = (
                    _control_details(tokens, end, limit, where)
                )
                layout.extend(("else_if", *nested_layout))
                binding += ("|else-if|", *nested_binding)
                slots.extend(
                    (f"else.{name}", slot_start, slot_end)
                    for name, slot_start, slot_end in nested_slots
                )
                end = nested_end
            elif tokens[end] == "{":
                else_close = _matching_delimiter(tokens, end, limit, where)
                layout.append("else")
                slots.append(("else", end + 1, else_close))
                end = else_close + 1
            else:
                raise SkeletonError(f"{where} has unsupported else syntax")
        if end < limit and tokens[end] == ";":
            end += 1
        return end, kind, tuple(layout) + binding, tuple(slots)

    if root in {"while", "for"}:
        kind = root
        binding = ()
        if root == "while" and start + 1 < opening and tokens[start + 1] == "let":
            equals = _top_level_token(tokens, start + 2, opening, {"="})
            if equals is None:
                raise SkeletonError(f"{where} has malformed while-let syntax")
            kind = "while_let"
            binding = tokens[start + 2 : equals]
        elif root == "for":
            in_token = _top_level_token(tokens, start + 1, opening, {"in"})
            if in_token is None:
                raise SkeletonError(f"{where} has malformed for syntax")
            binding = tokens[start + 1 : in_token]
        end = closing + 1
        if end < limit and tokens[end] == ";":
            end += 1
        return end, kind, (kind, *binding), (("body", opening + 1, closing),)

    if root == "match":
        arms: list[tuple[str, int, int]] = []
        patterns: list[str] = []
        cursor = opening + 1
        arm_index = 0
        while cursor < closing:
            while cursor < closing and tokens[cursor] == ",":
                cursor += 1
            if cursor >= closing:
                break
            arrow = _top_level_token(tokens, cursor, closing, {"=>"})
            if arrow is None:
                raise SkeletonError(f"{where} has malformed match arms")
            patterns.extend(("|arm|", *tokens[cursor:arrow]))
            body = arrow + 1
            if body >= closing or tokens[body] != "{":
                raise SkeletonError(f"{where} has a non-block match arm")
            arm_close = _matching_delimiter(tokens, body, closing + 1, where)
            arms.append((f"arm.{arm_index}", body + 1, arm_close))
            arm_index += 1
            cursor = arm_close + 1
            if cursor < closing and tokens[cursor] == ",":
                cursor += 1
        end = closing + 1
        if end < limit and tokens[end] == ";":
            end += 1
        return end, "match", ("match", *patterns), tuple(arms)

    end = closing + 1
    if end < limit and tokens[end] == ";":
        end += 1
    kind = "block" if root == "{" else root
    return end, kind, (kind,), (("body", opening + 1, closing),)


def _statement_end(
    tokens: tuple[str, ...], start: int, limit: int, where: str
) -> tuple[int, str, tuple[str, ...], tuple[tuple[str, int, int], ...]]:
    root = tokens[start]
    if root == "{" or root in {"if", "while", "for", "loop", "match"}:
        return _control_details(tokens, start, limit, where)
    macro = _top_level_token(tokens, start, limit, {"!"})
    if (
        macro is not None
        and macro + 1 < limit
        and tokens[macro + 1] == "{"
        and all(
            token == "::"
            or re.fullmatch(r"(?:r#)?[A-Za-z_][A-Za-z0-9_]*", token) is not None
            for token in tokens[start:macro]
        )
    ):
        closing = _matching_delimiter(tokens, macro + 1, limit, where)
        end = closing + 1
        if end < limit and tokens[end] == ";":
            end += 1
        return end, "macro_brace", (), ()
    semicolon = _top_level_token(tokens, start, limit, {";"})
    end = limit if semicolon is None else semicolon + 1
    if root != "let":
        statement_role = "tail" if semicolon is None else "semicolon"
        expression_role = (
            root if root in {"return", "break", "continue", "yield"} else "expression"
        )
        return end, f"{statement_role}_{expression_role}", (), ()
    equals = _top_level_token(tokens, start + 1, end, {"="})
    if equals is None:
        declaration_end = end - 1 if tokens[end - 1] == ";" else end
        declaration = tokens[start + 1 : declaration_end]
        if not declaration:
            raise SkeletonError(f"{where} has a malformed local declaration")
        return end, "let_uninitialized", declaration, ()
    declaration = tokens[start + 1 : equals]
    else_token = _top_level_token(tokens, equals + 1, end, {"else"})
    if else_token is None:
        return end, "let", declaration, ()
    else_open = else_token + 1
    if else_open >= end or tokens[else_open] != "{":
        raise SkeletonError(f"{where} has malformed let-else syntax")
    else_close = _matching_delimiter(tokens, else_open, end, where)
    return (
        end,
        "let_else",
        declaration,
        (("else", else_open + 1, else_close),),
    )


def _proctor_marker(
    tokens: tuple[str, ...], index: int, where: str
) -> tuple[int, int] | None:
    if tokens[index : index + 4] != ("#", "[", "proctor", "("):
        return None
    if index + 7 > len(tokens) or tokens[index + 5 : index + 7] != (")", "]"):
        raise SkeletonError(f"{where} has a malformed proctor label")
    raw = tokens[index + 4]
    if not raw.isdecimal():
        raise SkeletonError(f"{where} has a non-integer proctor label")
    label = int(raw)
    if label > U32_MAX:
        raise SkeletonError(f"{where} has an out-of-range proctor label")
    return label, index + 7


def _enclosing_block_limit(
    tokens: tuple[str, ...],
    body_open: int,
    marker_index: int,
    body_close: int,
    where: str,
) -> int:
    stack = [body_open]
    for index in range(body_open + 1, marker_index):
        if tokens[index] == "{":
            stack.append(index)
        elif tokens[index] == "}":
            if len(stack) == 1:
                raise SkeletonError(f"{where} has unbalanced block delimiters")
            stack.pop()
    return (
        body_close
        if len(stack) == 1
        else _matching_delimiter(tokens, stack[-1], body_close + 1, where)
    )


def _skeleton_shape(skeleton: str, record_id: int, view_name: str) -> _SkeletonShape:
    where = f"record {record_id} {view_name}.skeleton"
    tokens = _rust_tokens(skeleton, where)
    if not tokens:
        raise SkeletonError(f"{where} must contain one function")
    body_open = _top_level_token(tokens, 0, len(tokens), {"{"})
    if body_open is None:
        raise SkeletonError(f"{where} must contain a function body")
    body_close = _matching_delimiter(tokens, body_open, len(tokens), where)
    if body_close != len(tokens) - 1 or "fn" not in tokens[:body_open]:
        raise SkeletonError(f"{where} must contain exactly one complete function")

    markers: list[tuple[int, int, int]] = []
    index = body_open + 1
    while index < body_close:
        if (
            tokens[index] == "!"
            and index + 1 < body_close
            and tokens[index + 1] in {"(", "[", "{"}
        ):
            index = _matching_delimiter(tokens, index + 1, body_close, where) + 1
            continue
        marker = _proctor_marker(tokens, index, where)
        if marker is None:
            index += 1
            continue
        label, start = marker
        markers.append((label, index, start))
        index = start
    if len({label for label, _, _ in markers}) != len(markers):
        raise SkeletonError(f"{where} has duplicate proctor labels")

    intervals: list[
        tuple[
            int,
            int,
            int,
            int,
            str,
            tuple[str, ...],
            tuple[tuple[str, int, int], ...],
        ]
    ] = []
    for label, marker_index, start in markers:
        if start >= body_close:
            raise SkeletonError(f"{where} label {label} has no statement")
        statement_limit = _enclosing_block_limit(
            tokens, body_open, marker_index, body_close, where
        )
        end, root, declaration, slots = _statement_end(
            tokens, start, statement_limit, f"{where} label {label}"
        )
        intervals.append((label, marker_index, start, end, root, declaration, slots))

    parent_by_label: dict[int, int | None] = {}
    for label, marker_index, _, _, _, _, _ in intervals:
        containers = [
            (other_label, other_marker)
            for other_label, other_marker, _, other_end, _, _, _ in intervals
            if other_marker < marker_index < other_end
        ]
        parent_by_label[label] = (
            max(containers, key=lambda value: value[1])[0] if containers else None
        )

    statements: list[_SkeletonStatementShape] = []
    for label, marker_index, start, end, root, declaration, slots in intervals:
        parent = parent_by_label[label]
        direct_children = [
            (child_label, child_marker)
            for child_label, child_marker, _, _, _, _, _ in intervals
            if parent_by_label[child_label] == label
        ]
        child_slots: list[tuple[int, str]] = []
        for child_label, child_marker in direct_children:
            matching_slots = [
                name
                for name, slot_start, slot_end in slots
                if slot_start <= child_marker < slot_end
            ]
            slot = matching_slots[-1] if matching_slots else "payload"
            child_slots.append((child_label, slot))
        control = (root, *(name for name, _, _ in slots))
        if root in {
            "if",
            "if_let",
            "while",
            "while_let",
            "for",
            "loop",
            "match",
            "block",
        }:
            control = declaration
        statements.append(
            _SkeletonStatementShape(
                label=label,
                parent=parent,
                root=root,
                declaration=declaration,
                control=control,
                child_slots=tuple(child_slots),
                payload=tokens[start:end],
            )
        )
    return _SkeletonShape(signature=tokens[:body_open], statements=tuple(statements))


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
    baseline_shape = _skeleton_shape(baseline.skeleton, record_id, "baseline")
    applied_shape = _skeleton_shape(applied.skeleton, record_id, "applied")

    def disposition_topology(
        nodes: tuple[StatementDisposition, ...], parent: int | None = None
    ) -> tuple[tuple[int, int | None], ...]:
        result: list[tuple[int, int | None]] = []
        for node in nodes:
            result.append((node.label, parent))
            result.extend(disposition_topology(node.children, node.label))
        return tuple(result)

    expected_topology = disposition_topology(baseline.statement_dispositions)
    baseline_topology = tuple(
        (statement.label, statement.parent) for statement in baseline_shape.statements
    )
    if baseline_topology != expected_topology:
        raise SkeletonError(
            f"record {record_id} baseline skeleton labels do not match its disposition topology"
        )
    applied_topology = tuple(
        (statement.label, statement.parent) for statement in applied_shape.statements
    )
    if applied_topology != expected_topology:
        raise SkeletonError(
            f"record {record_id} applied skeleton labels do not match its disposition topology"
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
    if baseline_shape.signature != applied_shape.signature:
        raise SkeletonError(f"record {record_id} baseline/applied signatures differ")
    for before_shape, after_shape in zip(
        baseline_shape.statements, applied_shape.statements, strict=True
    ):
        disposition = next(
            node.disposition
            for node in baseline_nodes
            if node.label == before_shape.label
        )
        if disposition == "mechanical":
            if before_shape.payload != after_shape.payload:
                raise SkeletonError(
                    f"record {record_id} mechanical label {before_shape.label} "
                    "differs between skeleton views"
                )
            payload = before_shape.payload
            if (
                len(payload) != 9
                or payload[:6] != ("::", "std", "::", "print", "!", "(")
                or not payload[6].startswith('"')
                or payload[7:] != (")", ";")
            ):
                raise SkeletonError(
                    f"record {record_id} mechanical label {before_shape.label} "
                    "is not one canonical zero-argument print statement"
                )
            baseline_metadata = next(
                metadata
                for metadata in baseline.statement_pair_metadata
                if metadata.label == before_shape.label
            )
            applied_metadata = next(
                metadata
                for metadata in applied.statement_pair_metadata
                if metadata.label == before_shape.label
            )
            if baseline_metadata != applied_metadata:
                raise SkeletonError(
                    f"record {record_id} mechanical label {before_shape.label} "
                    "has different report metadata between skeleton views"
                )
        if (
            before_shape.root != after_shape.root
            or before_shape.control != after_shape.control
        ):
            raise SkeletonError(
                f"record {record_id} baseline/applied control topology differs "
                f"at label {before_shape.label}"
            )
        if before_shape.declaration != after_shape.declaration:
            raise SkeletonError(
                f"record {record_id} baseline/applied declaration topology differs "
                f"at label {before_shape.label}"
            )
        if before_shape.child_slots != after_shape.child_slots:
            raise SkeletonError(
                f"record {record_id} baseline/applied control child slots differ "
                f"at label {before_shape.label}"
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
            "foreign_function_names",
            "signature_dependencies",
            "dependencies",
        }
        if set(data) != required:
            raise SkeletonError(
                f"record {record_id} must contain exactly {sorted(required)}"
            )
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
            foreign_function_names=_foreign_function_names(data, record_id),
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


def leaf_schedule(graph: dict[int, set[int]]) -> tuple[tuple[int, ...], ...]:
    components = strongly_connected_components(graph)
    owner = {
        member: component_index
        for component_index, component in enumerate(components)
        for member in component
    }
    outgoing = {
        index: {
            owner[successor]
            for member in component
            for successor in graph[member]
            if owner[successor] != index
        }
        for index, component in enumerate(components)
    }
    unprocessed = set(range(len(components)))
    schedule: list[tuple[int, ...]] = []
    while unprocessed:
        leaves = [index for index in unprocessed if not (outgoing[index] & unprocessed)]
        chosen = min(leaves, key=lambda index: components[index][0])
        schedule.append(components[chosen])
        unprocessed.remove(chosen)
    return tuple(schedule)


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
