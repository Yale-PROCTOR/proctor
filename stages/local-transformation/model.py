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


@dataclass(frozen=True)
class ObservationDocument:
    observations: tuple[dict[str, Any], ...]


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ANON_ID = re.compile(
    r"<(id|fn|struct|enum|union|field|variant|const|static|method)([0-9]+)>\Z"
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


_PRIMITIVES = {
    "bool",
    "char",
    "str",
    "never",
    "i8",
    "i16",
    "i32",
    "i64",
    "i128",
    "isize",
    "u8",
    "u16",
    "u32",
    "u64",
    "u128",
    "usize",
    "f16",
    "f32",
    "f64",
    "f128",
}
_BINARY = {
    "add",
    "subtract",
    "multiply",
    "divide",
    "remainder",
    "and",
    "or",
    "bit_xor",
    "bit_and",
    "bit_or",
    "shift_left",
    "shift_right",
    "equal",
    "not_equal",
    "less",
    "less_equal",
    "greater",
    "greater_equal",
}


def _enum(value: Any, allowed: set[str], where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ObservationError(f"{where} has unknown value {value!r}")
    return value


def _anon(value: Any, prefixes: set[str], where: str) -> str:
    if not isinstance(value, str):
        raise ObservationError(f"{where} must be an anonymized ID")
    match = _ANON_ID.fullmatch(value)
    if match is None or match.group(1) not in prefixes:
        raise ObservationError(f"{where} must be a {sorted(prefixes)} anonymized ID")
    return value


def _external_identity(value: Any, where: str) -> None:
    value = _exact_object(value, {"kind", "crate", "path"}, where)
    if value["kind"] != "external":
        raise ObservationError(f"{where}.kind must be 'external'")
    if not isinstance(value["crate"], str) or not value["crate"]:
        raise ObservationError(f"{where}.crate must be a nonempty string")
    if (
        not isinstance(value["path"], list)
        or not value["path"]
        or any(not isinstance(part, str) or not part for part in value["path"])
    ):
        raise ObservationError(f"{where}.path must be a nonempty string array")


def _adt_identity(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise ObservationError(f"{where} must be an object")
    if value.get("kind") == "external":
        _external_identity(value, where)
    elif value.get("kind") == "local":
        _exact_object(value, {"kind", "id"}, where)
        _anon(value["id"], {"struct", "enum", "union"}, f"{where}.id")
    else:
        raise ObservationError(f"{where}.kind is unknown")


def _member_identity(value: Any, prefix: str, where: str) -> None:
    if not isinstance(value, dict):
        raise ObservationError(f"{where} must be an object")
    if value.get("kind") == "external":
        _external_identity(value, where)
    elif value.get("kind") == "local":
        _exact_object(value, {"kind", "owner", "id"}, where)
        _adt_identity(value["owner"], f"{where}.owner")
        _anon(value["id"], {prefix}, f"{where}.id")
    else:
        raise ObservationError(f"{where}.kind is unknown")


def _type_tree(value: Any, where: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise ObservationError(f"{where} must be a tagged object")
    kind = value["kind"]
    if kind == "primitive":
        _exact_object(value, {"kind", "name"}, where)
        _enum(value["name"], _PRIMITIVES, f"{where}.name")
    elif kind == "slice":
        _exact_object(value, {"kind", "element"}, where)
        _type_tree(value["element"], f"{where}.element")
    elif kind == "array":
        _exact_object(value, {"kind", "element", "length"}, where)
        _type_tree(value["element"], f"{where}.element")
        _wire_integer(value["length"], f"{where}.length")
    elif kind in {"raw_pointer", "reference"}:
        _exact_object(value, {"kind", "mutability", "pointee"}, where)
        _enum(
            value["mutability"],
            {"const", "mut"} if kind == "raw_pointer" else {"shared", "mutable"},
            f"{where}.mutability",
        )
        _type_tree(value["pointee"], f"{where}.pointee")
    elif kind == "tuple":
        _exact_object(value, {"kind", "elements"}, where)
        if not isinstance(value["elements"], list):
            raise ObservationError(f"{where}.elements must be an array")
        for index, element in enumerate(value["elements"]):
            _type_tree(element, f"{where}.elements[{index}]")
    elif kind == "adt":
        _exact_object(value, {"kind", "adt_kind", "identity", "arguments"}, where)
        _enum(value["adt_kind"], {"struct", "enum", "union"}, f"{where}.adt_kind")
        _adt_identity(value["identity"], f"{where}.identity")
        if not isinstance(value["arguments"], list):
            raise ObservationError(f"{where}.arguments must be an array")
        for index, argument in enumerate(value["arguments"]):
            _type_tree(argument, f"{where}.arguments[{index}]")
    else:
        raise ObservationError(f"{where}.kind is unknown: {kind!r}")


def _value_identity(value: Any, where: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise ObservationError(f"{where} must be a tagged object")
    kind = value["kind"]
    if kind in {"binding", "function", "constant", "static", "method"}:
        _exact_object(value, {"kind", "id"}, where)
        prefixes = {
            "binding": {"id"},
            "function": {"fn"},
            "constant": {"const"},
            "static": {"static"},
            "method": {"method"},
        }[kind]
        _anon(value["id"], prefixes, f"{where}.id")
    elif kind == "external":
        _external_identity(value, where)
    elif kind in {"foreign_function", "foreign_static"}:
        _exact_object(value, {"kind", "symbol"}, where)
        if not isinstance(value["symbol"], str) or not value["symbol"]:
            raise ObservationError(f"{where}.symbol must be nonempty")
    elif kind == "constructor":
        _exact_object(value, {"kind", "adt", "variant"}, where)
        _adt_identity(value["adt"], f"{where}.adt")
        if value["variant"] is not None:
            _member_identity(value["variant"], "variant", f"{where}.variant")
    else:
        raise ObservationError(f"{where}.kind is unknown: {kind!r}")


def _expression(value: Any, where: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise ObservationError(f"{where} must be a tagged expression")
    kind = value["kind"]
    if kind in {"array", "tuple"}:
        _exact_object(value, {"kind", "elements"}, where)
        if not isinstance(value["elements"], list):
            raise ObservationError(f"{where}.elements must be an array")
        for index, child in enumerate(value["elements"]):
            _expression(child, f"{where}.elements[{index}]")
    elif kind == "call":
        _exact_object(value, {"kind", "callee", "arguments"}, where)
        _expression(value["callee"], f"{where}.callee")
        if not isinstance(value["arguments"], list):
            raise ObservationError(f"{where}.arguments must be an array")
        for index, child in enumerate(value["arguments"]):
            _expression(child, f"{where}.arguments[{index}]")
    elif kind == "method_call":
        _exact_object(value, {"kind", "receiver", "method", "arguments"}, where)
        _expression(value["receiver"], f"{where}.receiver")
        _value_identity(value["method"], f"{where}.method")
        if not isinstance(value["arguments"], list):
            raise ObservationError(f"{where}.arguments must be an array")
        for index, child in enumerate(value["arguments"]):
            _expression(child, f"{where}.arguments[{index}]")
    elif kind in {"binary", "assign_op"}:
        _exact_object(value, {"kind", "operator", "left", "right"}, where)
        _enum(value["operator"], _BINARY, f"{where}.operator")
        _expression(value["left"], f"{where}.left")
        _expression(value["right"], f"{where}.right")
    elif kind == "unary":
        _exact_object(value, {"kind", "operator", "operand"}, where)
        _enum(value["operator"], {"deref", "not", "negate"}, f"{where}.operator")
        _expression(value["operand"], f"{where}.operand")
    elif kind == "path":
        _exact_object(value, {"kind", "value"}, where)
        _value_identity(value["value"], f"{where}.value")
    elif kind == "cast":
        _exact_object(value, {"kind", "expression", "type"}, where)
        _expression(value["expression"], f"{where}.expression")
        _type_tree(value["type"], f"{where}.type")
    elif kind in {"assign", "index"}:
        keys = (
            {"kind", "left", "right"} if kind == "assign" else {"kind", "base", "index"}
        )
        _exact_object(value, keys, where)
        for key in keys - {"kind"}:
            _expression(value[key], f"{where}.{key}")
    elif kind == "field":
        _exact_object(value, {"kind", "base", "field"}, where)
        _expression(value["base"], f"{where}.base")
        _member_identity(value["field"], "field", f"{where}.field")
    elif kind == "range":
        _exact_object(value, {"kind", "start", "end", "limits"}, where)
        _enum(value["limits"], {"half_open", "closed"}, f"{where}.limits")
        for key in ("start", "end"):
            if value[key] is not None:
                _expression(value[key], f"{where}.{key}")
    elif kind == "if":
        _exact_object(value, {"kind", "condition", "then", "else"}, where)
        _expression(value["condition"], f"{where}.condition")
        _block(value["then"], f"{where}.then")
        if value["else"] is not None:
            _expression(value["else"], f"{where}.else")
    elif kind == "while":
        _exact_object(value, {"kind", "condition", "body"}, where)
        _expression(value["condition"], f"{where}.condition")
        _block(value["body"], f"{where}.body")
    elif kind == "loop":
        _exact_object(value, {"kind", "body"}, where)
        _block(value["body"], f"{where}.body")
    elif kind == "struct":
        _exact_object(value, {"kind", "adt", "variant", "fields", "rest"}, where)
        _adt_identity(value["adt"], f"{where}.adt")
        if value["variant"] is not None:
            _member_identity(value["variant"], "variant", f"{where}.variant")
        if not isinstance(value["fields"], list):
            raise ObservationError(f"{where}.fields must be an array")
        seen_fields: set[str] = set()
        for index, field in enumerate(value["fields"]):
            field_where = f"{where}.fields[{index}]"
            field = _exact_object(field, {"field", "value"}, field_where)
            _member_identity(field["field"], "field", f"{field_where}.field")
            field_key = json.dumps(field["field"], sort_keys=True)
            if field_key in seen_fields:
                raise ObservationError(f"{where}.fields contains a duplicate field")
            seen_fields.add(field_key)
            _expression(field["value"], f"{field_where}.value")
        if value["rest"] is not None:
            _expression(value["rest"], f"{where}.rest")
    elif kind == "literal":
        _exact_object(value, {"kind", "value"}, where)
        _literal(value["value"], f"{where}.value")
    elif kind == "address_of":
        _exact_object(value, {"kind", "borrow", "mutability", "expression"}, where)
        _enum(value["borrow"], {"reference", "raw"}, f"{where}.borrow")
        _enum(value["mutability"], {"const", "mut"}, f"{where}.mutability")
        _expression(value["expression"], f"{where}.expression")
    elif kind in {"return", "break"}:
        _exact_object(value, {"kind", "value"}, where)
        if value["value"] is not None:
            _expression(value["value"], f"{where}.value")
    elif kind == "continue":
        _exact_object(value, {"kind"}, where)
    elif kind == "repeat":
        _exact_object(value, {"kind", "value", "count"}, where)
        _expression(value["value"], f"{where}.value")
        _expression(value["count"], f"{where}.count")
    elif kind == "block":
        _exact_object(value, {"kind", "block"}, where)
        _block(value["block"], f"{where}.block")
    else:
        raise ObservationError(f"{where}.kind is unknown: {kind!r}")


def _literal(value: Any, where: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise ObservationError(f"{where} must be a tagged literal")
    kind = value["kind"]
    keys = {"kind", "value"}
    if kind in {"integer", "float"}:
        keys = {"kind", "value" if kind == "integer" else "bits", "type"}
    _exact_object(value, keys, where)
    if kind == "bool" and not isinstance(value["value"], bool):
        raise ObservationError(f"{where}.value must be a Boolean")
    elif kind in {"char", "string"} and not isinstance(value["value"], str):
        raise ObservationError(f"{where}.value must be a string")
    elif kind == "byte" and (
        isinstance(value["value"], bool)
        or not isinstance(value["value"], int)
        or not 0 <= value["value"] <= 255
    ):
        raise ObservationError(f"{where}.value must be a byte")
    elif kind in {"byte_string", "c_string"} and (
        not isinstance(value["value"], list)
        or any(
            isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255
            for item in value["value"]
        )
    ):
        raise ObservationError(f"{where}.value must be a byte array")
    elif kind == "integer":
        if not isinstance(value["value"], str) or not value["value"].isdigit():
            raise ObservationError(f"{where}.value must be an unsigned decimal string")
        _enum(
            value["type"],
            _PRIMITIVES - {"bool", "char", "str", "never", "f16", "f32", "f64", "f128"},
            f"{where}.type",
        )
    elif kind == "float":
        _enum(value["type"], {"f16", "f32", "f64", "f128"}, f"{where}.type")
        widths = {"f16": 4, "f32": 8, "f64": 16, "f128": 32}
        if (
            not isinstance(value["bits"], str)
            or re.fullmatch(rf"[0-9a-f]{{{widths[value['type']]}}}", value["bits"])
            is None
        ):
            raise ObservationError(
                f"{where}.bits must be exactly {widths[value['type']]} lowercase hexadecimal digits"
            )
    elif kind not in {
        "bool",
        "char",
        "byte",
        "string",
        "byte_string",
        "c_string",
        "integer",
        "float",
    }:
        raise ObservationError(f"{where}.kind is unknown: {kind!r}")


def _block(value: Any, where: str) -> None:
    value = _exact_object(value, {"statements"}, where)
    if not isinstance(value["statements"], list):
        raise ObservationError(f"{where}.statements must be an array")
    for index, statement in enumerate(value["statements"]):
        statement_where = f"{where}.statements[{index}]"
        if not isinstance(statement, dict):
            raise ObservationError(f"{statement_where} must be an object")
        if statement.get("kind") == "expression":
            _exact_object(
                statement, {"kind", "expression", "semicolon"}, statement_where
            )
            if not isinstance(statement["semicolon"], bool):
                raise ObservationError(f"{statement_where}.semicolon must be a Boolean")
            _expression(statement["expression"], f"{statement_where}.expression")
        elif statement.get("kind") == "let":
            _exact_object(
                statement, {"kind", "pattern", "type", "initializer"}, statement_where
            )
            _pattern(statement["pattern"], f"{statement_where}.pattern")
            if statement["type"] is not None:
                _type_tree(statement["type"], f"{statement_where}.type")
            if statement["initializer"] is not None:
                _expression(statement["initializer"], f"{statement_where}.initializer")
        else:
            raise ObservationError(f"{statement_where}.kind is unknown")


def _pattern(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise ObservationError(f"{where} must be an object")
    if value.get("kind") == "wildcard":
        _exact_object(value, {"kind"}, where)
    elif value.get("kind") == "binding":
        _exact_object(value, {"kind", "id", "mutability", "by_ref"}, where)
        _anon(value["id"], {"id"}, f"{where}.id")
        _enum(value["mutability"], {"immutable", "mutable"}, f"{where}.mutability")
        _enum(value["by_ref"], {"no", "shared", "mutable"}, f"{where}.by_ref")
    else:
        raise ObservationError(f"{where}.kind is unknown")


def _anonymized_ids(value: Any) -> list[tuple[str, int, str]]:
    result: list[tuple[str, int, str]] = []

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            for child in current.values():
                visit(child)
        elif isinstance(current, list):
            for child in current:
                visit(child)
        elif isinstance(current, str):
            match = _ANON_ID.fullmatch(current)
            if match is not None:
                result.append((match.group(1), int(match.group(2)), current))

    visit(value)
    return result


def _validate_anonymization(observation: dict[str, Any], where: str) -> None:
    all_ids = _anonymized_ids(observation)
    for prefix in {prefix for prefix, _, _ in all_ids}:
        first_occurrences: list[int] = []
        for candidate, index, _ in all_ids:
            if candidate == prefix and index not in first_occurrences:
                first_occurrences.append(index)
        if first_occurrences != list(range(len(first_occurrences))):
            raise ObservationError(
                f"{where} anonymized {prefix} IDs must follow contiguous first-occurrence order"
            )

    source_ids = {
        text for _, _, text in _anonymized_ids(observation["source_expression"])
    }
    target_only = sorted(
        text
        for prefix, _, text in _anonymized_ids(observation["target_expression"])
        if prefix in {"id", "fn"} and text not in source_ids
    )
    if target_only:
        raise ObservationError(
            f"{where}.target_expression contains target-only anonymized ID {target_only[0]}"
        )

    source_binding_order: list[str] = []
    for prefix, _, text in _anonymized_ids(observation["source_expression"]):
        if prefix == "id" and text not in source_binding_order:
            source_binding_order.append(text)
    anchor_ids = [anchor["id"] for anchor in observation["pointer_anchors"]]
    source_positions = {
        value: index for index, value in enumerate(source_binding_order)
    }
    if any(value not in source_positions for value in anchor_ids) or any(
        source_positions[left] >= source_positions[right]
        for left, right in pairwise(anchor_ids)
    ):
        raise ObservationError(
            f"{where}.pointer_anchors do not preserve first source occurrence order"
        )


def load_observations(text: str) -> ObservationDocument:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ObservationError(f"observation JSON decode failure: {exc}") from exc
    value = _exact_object(
        value, {"schema_version", "observations"}, "observation document"
    )
    if isinstance(value["schema_version"], bool) or value["schema_version"] != 1:
        raise ObservationError(
            f"unsupported observation schema_version {value['schema_version']!r}"
        )
    if not isinstance(value["observations"], list):
        raise ObservationError("observation document observations must be an array")
    for index, observation in enumerate(value["observations"]):
        where = f"observations[{index}]"
        observation = _exact_object(
            observation,
            {
                "source_expression",
                "target_expression",
                "pointer_anchors",
                "source_type",
                "source_adjusted_type",
                "target_type",
                "target_adjusted_type",
            },
            where,
        )
        _expression(observation["source_expression"], f"{where}.source_expression")
        _expression(observation["target_expression"], f"{where}.target_expression")
        if not isinstance(observation["pointer_anchors"], list):
            raise ObservationError(f"{where}.pointer_anchors must be an array")
        if not observation["pointer_anchors"]:
            raise ObservationError(f"{where}.pointer_anchors must be nonempty")
        seen: set[str] = set()
        for anchor_index, anchor in enumerate(observation["pointer_anchors"]):
            anchor_where = f"{where}.pointer_anchors[{anchor_index}]"
            anchor = _exact_object(
                anchor, {"id", "source_type", "target_type"}, anchor_where
            )
            anchor_id = _anon(anchor["id"], {"id"}, f"{anchor_where}.id")
            if anchor_id in seen:
                raise ObservationError(
                    f"{where}.pointer_anchors has duplicate ID {anchor_id}"
                )
            seen.add(anchor_id)
            _type_tree(anchor["source_type"], f"{anchor_where}.source_type")
            _type_tree(anchor["target_type"], f"{anchor_where}.target_type")
            if anchor["source_type"].get("kind") != "raw_pointer":
                raise ObservationError(
                    f"{anchor_where}.source_type must have outer kind raw_pointer"
                )
        for key in (
            "source_type",
            "source_adjusted_type",
            "target_type",
            "target_adjusted_type",
        ):
            _type_tree(observation[key], f"{where}.{key}")
        _validate_anonymization(observation, where)
    return ObservationDocument(observations=tuple(value["observations"]))


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
class StatementPairMetadata:
    label: int
    before_statement: str
    pointer_variables_complete: bool
    pointer_variables: tuple[PointerVariableMetadata, ...]


@dataclass(frozen=True)
class ItemRecord:
    id: int
    path: str
    kind: str
    dependencies: tuple[int, ...]
    signature_dependencies: tuple[int, ...] = ()
    name: str | None = None
    annotated_source: str | None = None
    annotated_skeleton: str | None = None
    source_signature: str | None = None
    target_signature: str | None = None
    needs_transformation: bool | None = None
    statements_requiring_transformation: tuple[int, ...] = ()
    statement_pair_metadata: tuple[StatementPairMetadata, ...] = ()
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
) -> tuple[StatementPairMetadata, ...]:
    where = f"record {record_id} field 'statement_pair_metadata'"
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
                pointer_variables_complete=complete,
                pointer_variables=tuple(variables),
            )
        )
    labels = tuple(statement.label for statement in result)
    if labels != expected_labels:
        raise SkeletonError(
            f"{where} labels must exactly match "
            "'statements_requiring_transformation' in producer order"
        )
    return tuple(result)


def _load_record(data: Any, index: int) -> ItemRecord:
    if not isinstance(data, dict):
        raise SkeletonError(f"record {index} must be an object")
    record_id = _integer(data.get("id"), f"record {index} id")
    path = _string(data, "path", record_id)
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in ALL_KINDS:
        raise SkeletonError(f"record {record_id} has unknown kind {kind!r}")
    if kind == "Fn":
        required = (
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
        )
        missing = [key for key in required if key not in data]
        if missing:
            raise SkeletonError(
                f"record {record_id} is missing required fields: {', '.join(missing)}"
            )
        dependencies = _dependencies(data, "dependencies", record_id)
        signature_dependencies = _dependencies(
            data, "signature_dependencies", record_id
        )
        needs_transformation = data["needs_transformation"]
        if not isinstance(needs_transformation, bool):
            raise SkeletonError(
                f"record {record_id} field 'needs_transformation' must be a Boolean"
            )
        statements_requiring_transformation = _u32_labels(
            data["statements_requiring_transformation"],
            f"record {record_id} field 'statements_requiring_transformation'",
        )
        if needs_transformation != bool(statements_requiring_transformation):
            raise SkeletonError(
                f"record {record_id} preservation Boolean and label array are inconsistent"
            )
        statement_pair_metadata = _statement_pair_metadata(
            data["statement_pair_metadata"],
            record_id,
            statements_requiring_transformation,
        )
        return ItemRecord(
            id=record_id,
            path=path,
            kind=kind,
            name=_string(data, "name", record_id),
            annotated_source=_string(data, "annotated_source", record_id),
            annotated_skeleton=_string(data, "annotated_skeleton", record_id),
            source_signature=_string(data, "source_signature", record_id),
            target_signature=_string(data, "target_signature", record_id),
            needs_transformation=needs_transformation,
            statements_requiring_transformation=statements_requiring_transformation,
            statement_pair_metadata=statement_pair_metadata,
            foreign_function_names=_foreign_function_names(data, record_id),
            signature_dependencies=signature_dependencies,
            dependencies=dependencies,
        )
    if kind in {"Static", "Const"}:
        missing = [
            key
            for key in ("declaration", "signature_dependencies", "dependencies")
            if key not in data
        ]
        if missing:
            raise SkeletonError(
                f"record {record_id} is missing required fields: {', '.join(missing)}"
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
    missing = [key for key in ("definition", "dependencies") if key not in data]
    if missing:
        raise SkeletonError(
            f"record {record_id} is missing required fields: {', '.join(missing)}"
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
    members: tuple[int, ...], records_by_id: dict[int, ItemRecord]
) -> str:
    entries = []
    for item_id in sorted(members):
        record = records_by_id[item_id]
        foreign_references = ""
        if record.foreign_function_names:
            names = ", ".join(f"`{name}`" for name in record.foreign_function_names)
            foreign_references = f"Foreign function references: {names}\n\n"
        entries.append(
            f"### Function `{record.name}`\n\n"
            f"{foreign_references}"
            f"Source:\n```rust\n{record.annotated_source}\n```\n"
            f"Target skeleton:\n```rust\n{record.annotated_skeleton}\n```"
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
