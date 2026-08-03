from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from model import (
    ObservationDocument,
    RuleDocument,
    _RULE_KEY_ORDERS,
    load_rules,
    rules_to_json,
)

_CANONICAL_MAGNITUDE = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)
_LOCAL_ID = re.compile(
    r"<(id|fn|struct|enum|union|field|variant|const|static|method)[0-9]+>\Z"
)
_PREFIX_SORT = {
    "id": "binding",
    "fn": "function",
    "struct": "struct",
    "enum": "enum",
    "union": "union",
    "field": "field",
    "variant": "variant",
    "const": "constant",
    "static": "static",
    "method": "method",
}
_IDENTITY_SORTS = frozenset(_PREFIX_SORT.values())


class PairRejection(Enum):
    CONTEXT = "context"
    SOURCE = "source"
    DEGENERATE_SOURCE = "degenerate_source"
    TARGET_LOOKUP = "target_lookup"
    CARRIER = "carrier"


@dataclass(frozen=True)
class PairResult:
    rule: dict[str, Any] | None
    rejection: PairRejection | None
    substitutions: dict[tuple[str, int], tuple[Any, Any]] | None = None


@dataclass(frozen=True)
class _Walk:
    status: str
    value: Any = None


_OK = "ok"
_GENERALIZE = "generalize"
_IDENTITY_CONFLICT = "identity_conflict"
_REJECT = "reject"


def _ok(value: Any) -> _Walk:
    return _Walk(_OK, value)


def _variable(sort: str, index: int) -> dict[str, Any]:
    return {"kind": "variable", "sort": sort, "index": index}


def _object_order(value: dict[str, Any]) -> tuple[str, ...]:
    keys = frozenset(value)
    if keys == {
        "source_expression",
        "target_expression",
        "pointer_anchors",
        "source_type",
        "source_adjusted_type",
        "target_type",
        "target_adjusted_type",
    }:
        return (
            "source_expression",
            "target_expression",
            "pointer_anchors",
            "source_type",
            "source_adjusted_type",
            "target_type",
            "target_adjusted_type",
        )
    if keys == {"id", "source_type", "target_type"}:
        return ("id", "source_type", "target_type")
    return _RULE_KEY_ORDERS.get(keys, tuple(sorted(value)))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return (
            "object",
            tuple((key, _freeze(value[key])) for key in _object_order(value)),
        )
    if isinstance(value, list):
        return ("array", tuple(_freeze(child) for child in value))
    return ("scalar", type(value).__name__, value)


def _semantic_sort_key(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity_key(value: str) -> tuple[str, str] | None:
    match = _LOCAL_ID.fullmatch(value)
    if match is None:
        return None
    return (_PREFIX_SORT[match.group(1)], value)


@dataclass
class _State:
    counters: dict[str, int] = field(default_factory=dict)
    disagreements: dict[tuple[str, Any, Any], dict[str, Any]] = field(
        default_factory=dict
    )
    identities: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    substitutions: dict[tuple[str, int], tuple[Any, Any]] = field(default_factory=dict)
    context_forward: dict[tuple[str, str], str] = field(default_factory=dict)
    context_reverse: dict[tuple[str, str], str] = field(default_factory=dict)
    anchor_pairs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    left_anchors: set[str] = field(default_factory=set)
    right_anchors: set[str] = field(default_factory=set)

    def allocate(self, sort: str, left: Any, right: Any) -> dict[str, Any]:
        index = self.counters.get(sort, 0)
        self.counters[sort] = index + 1
        result = _variable(sort, index)
        self.substitutions[(sort, index)] = (copy.deepcopy(left), copy.deepcopy(right))
        return result

    def disagreement(
        self, sort: str, left: Any, right: Any, mode: str
    ) -> dict[str, Any] | None:
        key = (sort, _freeze(left), _freeze(right))
        existing = self.disagreements.get(key)
        if existing is not None:
            return existing
        if mode == "target":
            return None
        result = self.allocate(sort, left, right)
        self.disagreements[key] = result
        return result

    def context_identity(
        self, sort: str, left: str, right: str
    ) -> dict[str, Any] | None:
        left_key = (sort, left)
        right_key = (sort, right)
        if self.context_forward.get(left_key, right) != right:
            return None
        if self.context_reverse.get(right_key, left) != left:
            return None
        self.context_forward[left_key] = right
        self.context_reverse[right_key] = left
        return self.identity(sort, left, right, "context")

    def identity(
        self, sort: str, left: str, right: str, mode: str
    ) -> dict[str, Any] | None:
        if sort == "binding" and (
            left in self.left_anchors or right in self.right_anchors
        ):
            return self.anchor_pairs.get((left, right))
        key = (sort, left, right)
        existing = self.identities.get(key)
        if existing is not None:
            return existing
        if mode == "target":
            return None
        result = self.allocate(sort, left, right)
        self.identities[key] = result
        return result

    def snapshot(
        self,
    ) -> tuple[
        dict[str, int],
        dict[tuple[str, Any, Any], dict[str, Any]],
        dict[tuple[str, str, str], dict[str, Any]],
        dict[tuple[str, int], tuple[Any, Any]],
    ]:
        return (
            dict(self.counters),
            dict(self.disagreements),
            dict(self.identities),
            dict(self.substitutions),
        )

    def restore(
        self,
        snapshot: tuple[
            dict[str, int],
            dict[tuple[str, Any, Any], dict[str, Any]],
            dict[tuple[str, str, str], dict[str, Any]],
            dict[tuple[str, int], tuple[Any, Any]],
        ],
    ) -> None:
        (
            self.counters,
            self.disagreements,
            self.identities,
            self.substitutions,
        ) = snapshot


def _local_identity(
    left: str, right: str, expected_sort: str, state: _State, mode: str
) -> _Walk:
    left_key = _identity_key(left)
    right_key = _identity_key(right)
    if (
        left_key is None
        or right_key is None
        or left_key[0] != expected_sort
        or right_key[0] != expected_sort
    ):
        return _Walk(_IDENTITY_CONFLICT)
    result = state.identity(expected_sort, left, right, mode)
    if result is None:
        return _Walk(_REJECT if mode == "target" else _IDENTITY_CONFLICT)
    return _ok(result)


def _adt_identity(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if (
        isinstance(left, dict)
        and isinstance(right, dict)
        and left.get("kind") == right.get("kind") == "local"
    ):
        left_key = _identity_key(left["id"])
        right_key = _identity_key(right["id"])
        if (
            left_key is None
            or right_key is None
            or left_key[0]
            not in {
                "struct",
                "enum",
                "union",
            }
        ):
            return _Walk(_IDENTITY_CONFLICT)
        if left_key[0] != right_key[0]:
            return _Walk(_IDENTITY_CONFLICT)
        return _local_identity(left["id"], right["id"], left_key[0], state, mode)
    if left == right and isinstance(left, dict) and left.get("kind") == "external":
        return _ok(copy.deepcopy(left))
    return _Walk(_IDENTITY_CONFLICT)


def _member_identity(
    left: Any, right: Any, member_sort: str, state: _State, mode: str
) -> _Walk:
    if (
        isinstance(left, dict)
        and isinstance(right, dict)
        and left.get("kind") == right.get("kind") == "local"
    ):
        owner = _adt_identity(left["owner"], right["owner"], state, mode)
        if owner.status != _OK:
            return owner
        member = _local_identity(left["id"], right["id"], member_sort, state, mode)
        if member.status != _OK:
            return member
        return _ok({"kind": "local", "owner": owner.value, "id": member.value})
    if left == right and isinstance(left, dict) and left.get("kind") == "external":
        return _ok(copy.deepcopy(left))
    return _Walk(_IDENTITY_CONFLICT)


def _value_identity(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return _Walk(_IDENTITY_CONFLICT)
    left_kind = left.get("kind")
    right_kind = right.get("kind")
    local_sorts = {
        "binding": "binding",
        "function": "function",
        "constant": "constant",
        "static": "static",
        "method": "method",
    }
    if left_kind == right_kind and left_kind in local_sorts:
        return _local_identity(
            left["id"], right["id"], local_sorts[left_kind], state, mode
        )
    if left_kind == right_kind == "constructor":
        adt = _adt_identity(left["adt"], right["adt"], state, mode)
        if adt.status != _OK:
            return adt
        if left["variant"] is None and right["variant"] is None:
            variant = None
        elif left["variant"] is not None and right["variant"] is not None:
            result = _member_identity(
                left["variant"], right["variant"], "variant", state, mode
            )
            if result.status != _OK:
                return result
            variant = result.value
        else:
            return _Walk(_IDENTITY_CONFLICT)
        return _ok({"kind": "constructor", "adt": adt.value, "variant": variant})
    if left == right and left_kind in {
        "external",
        "foreign_function",
        "foreign_static",
    }:
        return _ok(copy.deepcopy(left))
    return _Walk(_IDENTITY_CONFLICT)


def _type_tree(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return _Walk(_GENERALIZE)
    kind = left.get("kind")
    if kind != right.get("kind"):
        return _Walk(_GENERALIZE)
    if kind == "primitive":
        return (
            _ok(copy.deepcopy(left))
            if left["name"] == right["name"]
            else _Walk(_GENERALIZE)
        )
    if kind == "slice":
        child = _type_tree(left["element"], right["element"], state, mode)
        return (
            _ok({"kind": "slice", "element": child.value})
            if child.status == _OK
            else child
        )
    if kind == "array":
        if left["length"] != right["length"]:
            return _Walk(_GENERALIZE)
        child = _type_tree(left["element"], right["element"], state, mode)
        return (
            _ok({"kind": "array", "element": child.value, "length": left["length"]})
            if child.status == _OK
            else child
        )
    if kind in {"raw_pointer", "reference"}:
        if left["mutability"] != right["mutability"]:
            return _Walk(_GENERALIZE)
        child = _type_tree(left["pointee"], right["pointee"], state, mode)
        return (
            _ok(
                {"kind": kind, "mutability": left["mutability"], "pointee": child.value}
            )
            if child.status == _OK
            else child
        )
    if kind == "tuple":
        if len(left["elements"]) != len(right["elements"]):
            return _Walk(_GENERALIZE)
        elements = []
        for left_child, right_child in zip(
            left["elements"], right["elements"], strict=True
        ):
            child = _type_tree(left_child, right_child, state, mode)
            if child.status != _OK:
                return child
            elements.append(child.value)
        return _ok({"kind": "tuple", "elements": elements})
    if kind == "adt":
        if left["adt_kind"] != right["adt_kind"] or len(left["arguments"]) != len(
            right["arguments"]
        ):
            return _Walk(_GENERALIZE)
        identity = _adt_identity(left["identity"], right["identity"], state, mode)
        if identity.status != _OK:
            return identity
        arguments = []
        for left_child, right_child in zip(
            left["arguments"], right["arguments"], strict=True
        ):
            child = _type_tree(left_child, right_child, state, mode)
            if child.status != _OK:
                return child
            arguments.append(child.value)
        return _ok(
            {
                "kind": "adt",
                "adt_kind": left["adt_kind"],
                "identity": identity.value,
                "arguments": arguments,
            }
        )
    return _Walk(_GENERALIZE)


def _context_type(left: Any, right: Any, state: _State) -> _Walk:
    before = dict(state.identities)
    result = _type_tree(left, right, state, "context")
    if result.status != _OK:
        return result
    for key in set(state.identities) - set(before):
        sort, left_id, right_id = key
        if state.context_forward.get((sort, left_id), right_id) != right_id:
            return _Walk(_REJECT)
        if state.context_reverse.get((sort, right_id), left_id) != left_id:
            return _Walk(_REJECT)
        state.context_forward[(sort, left_id)] = right_id
        state.context_reverse[(sort, right_id)] = left_id
    return result


def _generalize_expression(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    variable = state.disagreement("expression", left, right, mode)
    return _ok(variable) if variable is not None else _Walk(_REJECT)


def _expression_child(
    result: _Walk, left: Any, right: Any, state: _State, mode: str
) -> _Walk:
    if result.status == _OK or result.status == _REJECT:
        return result
    return _generalize_expression(left, right, state, mode)


def _optional_expression(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if left is None and right is None:
        return _ok(None)
    if left is None or right is None:
        return _Walk(_GENERALIZE)
    return _expression(left, right, state, mode)


def _expression_list(
    left: list[Any], right: list[Any], state: _State, mode: str
) -> _Walk:
    if len(left) != len(right):
        return _Walk(_GENERALIZE)
    result = []
    for left_child, right_child in zip(left, right, strict=True):
        child = _expression(left_child, right_child, state, mode)
        if child.status != _OK:
            return child
        result.append(child.value)
    return _ok(result)


def _block(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if len(left["statements"]) != len(right["statements"]):
        return _Walk(_GENERALIZE)
    statements = []
    for left_statement, right_statement in zip(
        left["statements"], right["statements"], strict=True
    ):
        kind = left_statement["kind"]
        if kind != right_statement["kind"]:
            return _Walk(_GENERALIZE)
        if kind == "expression":
            if left_statement["semicolon"] != right_statement["semicolon"]:
                return _Walk(_GENERALIZE)
            expression = _expression(
                left_statement["expression"], right_statement["expression"], state, mode
            )
            if expression.status != _OK:
                return expression
            statements.append(
                {
                    "kind": "expression",
                    "expression": expression.value,
                    "semicolon": left_statement["semicolon"],
                }
            )
        else:
            pattern = _pattern(
                left_statement["pattern"], right_statement["pattern"], state, mode
            )
            if pattern.status != _OK:
                return pattern
            if (left_statement["type"] is None) != (right_statement["type"] is None):
                return _Walk(_GENERALIZE)
            type_value = None
            if left_statement["type"] is not None:
                type_result = _type_tree(
                    left_statement["type"], right_statement["type"], state, mode
                )
                if type_result.status != _OK:
                    return type_result
                type_value = type_result.value
            initializer = _optional_expression(
                left_statement["initializer"],
                right_statement["initializer"],
                state,
                mode,
            )
            if initializer.status != _OK:
                return initializer
            statements.append(
                {
                    "kind": "let",
                    "pattern": pattern.value,
                    "type": type_value,
                    "initializer": initializer.value,
                }
            )
    return _ok({"statements": statements})


def _pattern(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if left["kind"] != right["kind"]:
        return _Walk(_GENERALIZE)
    if left["kind"] == "wildcard":
        return _ok({"kind": "wildcard"})
    if left["mutability"] != right["mutability"] or left["by_ref"] != right["by_ref"]:
        return _Walk(_GENERALIZE)
    identity = _local_identity(left["id"], right["id"], "binding", state, mode)
    if identity.status != _OK:
        return identity
    return _ok(
        {
            "kind": "binding",
            "id": identity.value,
            "mutability": left["mutability"],
            "by_ref": left["by_ref"],
        }
    )


def _expression(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if mode != "source":
        return _expression_inner(left, right, state, mode)
    snapshot = state.snapshot()
    result = _expression_inner(left, right, state, mode)
    if result.status != _OK:
        state.restore(snapshot)
        return result
    if (
        isinstance(result.value, dict)
        and result.value.get("kind") == "variable"
        and result.value.get("sort") == "expression"
        and (
            substitution := state.substitutions.get(
                ("expression", result.value["index"])
            )
        )
        is not None
        and _freeze(substitution[0]) == _freeze(left)
        and _freeze(substitution[1]) == _freeze(right)
    ):
        state.restore(snapshot)
        return _generalize_expression(left, right, state, mode)
    return result


def _expression_inner(left: Any, right: Any, state: _State, mode: str) -> _Walk:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return _generalize_expression(left, right, state, mode)
    kind = left.get("kind")
    if kind != right.get("kind"):
        return _generalize_expression(left, right, state, mode)

    def child(left_child: Any, right_child: Any) -> _Walk:
        return _expression(left_child, right_child, state, mode)

    if kind in {"array", "tuple"}:
        children = _expression_list(left["elements"], right["elements"], state, mode)
        if children.status != _OK:
            return _expression_child(children, left, right, state, mode)
        return _ok({"kind": kind, "elements": children.value})
    if kind == "call":
        callee = child(left["callee"], right["callee"])
        if callee.status != _OK:
            return _expression_child(callee, left, right, state, mode)
        arguments = _expression_list(left["arguments"], right["arguments"], state, mode)
        if arguments.status != _OK:
            return _expression_child(arguments, left, right, state, mode)
        return _ok(
            {"kind": "call", "callee": callee.value, "arguments": arguments.value}
        )
    if kind == "method_call":
        receiver = child(left["receiver"], right["receiver"])
        if receiver.status != _OK:
            return _expression_child(receiver, left, right, state, mode)
        method = _value_identity(left["method"], right["method"], state, mode)
        if method.status != _OK:
            return _expression_child(method, left, right, state, mode)
        arguments = _expression_list(left["arguments"], right["arguments"], state, mode)
        if arguments.status != _OK:
            return _expression_child(arguments, left, right, state, mode)
        return _ok(
            {
                "kind": "method_call",
                "receiver": receiver.value,
                "method": method.value,
                "arguments": arguments.value,
            }
        )
    if kind in {"binary", "assign_op"}:
        if left["operator"] != right["operator"]:
            return _generalize_expression(left, right, state, mode)
        first = child(left["left"], right["left"])
        if first.status != _OK:
            return _expression_child(first, left, right, state, mode)
        second = child(left["right"], right["right"])
        if second.status != _OK:
            return _expression_child(second, left, right, state, mode)
        return _ok(
            {
                "kind": kind,
                "operator": left["operator"],
                "left": first.value,
                "right": second.value,
            }
        )
    if kind == "unary":
        if left["operator"] != right["operator"]:
            return _generalize_expression(left, right, state, mode)
        operand = child(left["operand"], right["operand"])
        if operand.status != _OK:
            return _expression_child(operand, left, right, state, mode)
        return (
            _ok(
                {
                    "kind": "unary",
                    "operator": left["operator"],
                    "operand": operand.value,
                }
            )
            if operand.status == _OK
            else operand
        )
    if kind == "path":
        identity = _value_identity(left["value"], right["value"], state, mode)
        if identity.status != _OK:
            return identity
        return _ok({"kind": "path", "value": identity.value})
    if kind == "cast":
        expression = child(left["expression"], right["expression"])
        if expression.status != _OK:
            return _expression_child(expression, left, right, state, mode)
        type_result = _type_tree(left["type"], right["type"], state, mode)
        if type_result.status != _OK:
            return _expression_child(type_result, left, right, state, mode)
        return _ok(
            {"kind": "cast", "expression": expression.value, "type": type_result.value}
        )
    if kind in {"assign", "index"}:
        names = ("left", "right") if kind == "assign" else ("base", "index")
        first = child(left[names[0]], right[names[0]])
        if first.status != _OK:
            return _expression_child(first, left, right, state, mode)
        second = child(left[names[1]], right[names[1]])
        if second.status != _OK:
            return _expression_child(second, left, right, state, mode)
        return _ok({"kind": kind, names[0]: first.value, names[1]: second.value})
    if kind == "field":
        base = child(left["base"], right["base"])
        if base.status != _OK:
            return _expression_child(base, left, right, state, mode)
        member = _member_identity(left["field"], right["field"], "field", state, mode)
        if member.status != _OK:
            return _expression_child(member, left, right, state, mode)
        return _ok({"kind": "field", "base": base.value, "field": member.value})
    if kind == "range":
        if left["limits"] != right["limits"]:
            return _generalize_expression(left, right, state, mode)
        start = _optional_expression(left["start"], right["start"], state, mode)
        if start.status != _OK:
            return _expression_child(start, left, right, state, mode)
        end = _optional_expression(left["end"], right["end"], state, mode)
        if end.status != _OK:
            return _expression_child(end, left, right, state, mode)
        return _ok(
            {
                "kind": "range",
                "start": start.value,
                "end": end.value,
                "limits": left["limits"],
            }
        )
    if kind == "if":
        condition = child(left["condition"], right["condition"])
        if condition.status != _OK:
            return _expression_child(condition, left, right, state, mode)
        then = _block(left["then"], right["then"], state, mode)
        if then.status != _OK:
            return _expression_child(then, left, right, state, mode)
        otherwise = _optional_expression(left["else"], right["else"], state, mode)
        if otherwise.status != _OK:
            return _expression_child(otherwise, left, right, state, mode)
        return _ok(
            {
                "kind": "if",
                "condition": condition.value,
                "then": then.value,
                "else": otherwise.value,
            }
        )
    if kind == "while":
        condition = child(left["condition"], right["condition"])
        if condition.status != _OK:
            return _expression_child(condition, left, right, state, mode)
        body = _block(left["body"], right["body"], state, mode)
        if body.status != _OK:
            return _expression_child(body, left, right, state, mode)
        return _ok({"kind": "while", "condition": condition.value, "body": body.value})
    if kind == "loop":
        body = _block(left["body"], right["body"], state, mode)
        if body.status != _OK:
            return _expression_child(body, left, right, state, mode)
        return _ok({"kind": "loop", "body": body.value})
    if kind == "struct":
        if len(left["fields"]) != len(right["fields"]):
            return _generalize_expression(left, right, state, mode)
        adt = _adt_identity(left["adt"], right["adt"], state, mode)
        if adt.status != _OK:
            return _expression_child(adt, left, right, state, mode)
        if left["variant"] is None and right["variant"] is None:
            variant = None
        elif left["variant"] is not None and right["variant"] is not None:
            variant_result = _member_identity(
                left["variant"], right["variant"], "variant", state, mode
            )
            if variant_result.status != _OK:
                return _expression_child(variant_result, left, right, state, mode)
            variant = variant_result.value
        else:
            return _generalize_expression(left, right, state, mode)
        fields = []
        for left_field, right_field in zip(
            left["fields"], right["fields"], strict=True
        ):
            member = _member_identity(
                left_field["field"], right_field["field"], "field", state, mode
            )
            if member.status != _OK:
                return _expression_child(member, left, right, state, mode)
            value = child(left_field["value"], right_field["value"])
            if value.status != _OK:
                return _expression_child(value, left, right, state, mode)
            fields.append({"field": member.value, "value": value.value})
        rest = _optional_expression(left["rest"], right["rest"], state, mode)
        if rest.status != _OK:
            return _expression_child(rest, left, right, state, mode)
        return _ok(
            {
                "kind": "struct",
                "adt": adt.value,
                "variant": variant,
                "fields": fields,
                "rest": rest.value,
            }
        )
    if kind == "literal":
        left_literal = left["value"]
        right_literal = right["value"]
        if left_literal.get("kind") == right_literal.get("kind") == "integer":
            if left_literal["type"] != right_literal["type"]:
                return _generalize_expression(left, right, state, mode)
            left_value = left_literal["value"]
            right_value = right_literal["value"]
            if left_value == right_value:
                magnitude: Any = left_value
            elif _CANONICAL_MAGNITUDE.fullmatch(
                left_value
            ) and _CANONICAL_MAGNITUDE.fullmatch(right_value):
                magnitude = state.disagreement(
                    "integer_magnitude", left_value, right_value, mode
                )
                if magnitude is None:
                    return _Walk(_REJECT)
            else:
                return _generalize_expression(left, right, state, mode)
            return _ok(
                {
                    "kind": "literal",
                    "value": {
                        "kind": "integer",
                        "value": magnitude,
                        "type": left_literal["type"],
                    },
                }
            )
        if left_literal == right_literal:
            return _ok(copy.deepcopy(left))
        return _generalize_expression(left, right, state, mode)
    if kind == "address_of":
        if (
            left["borrow"] != right["borrow"]
            or left["mutability"] != right["mutability"]
        ):
            return _generalize_expression(left, right, state, mode)
        expression = child(left["expression"], right["expression"])
        if expression.status != _OK:
            return _expression_child(expression, left, right, state, mode)
        return _ok(
            {
                "kind": "address_of",
                "borrow": left["borrow"],
                "mutability": left["mutability"],
                "expression": expression.value,
            }
        )
    if kind in {"return", "break"}:
        value = _optional_expression(left["value"], right["value"], state, mode)
        if value.status != _OK:
            return _expression_child(value, left, right, state, mode)
        return _ok({"kind": kind, "value": value.value})
    if kind == "continue":
        return _ok({"kind": "continue"})
    if kind == "repeat":
        value = child(left["value"], right["value"])
        if value.status != _OK:
            return _expression_child(value, left, right, state, mode)
        count = child(left["count"], right["count"])
        if count.status != _OK:
            return _expression_child(count, left, right, state, mode)
        return _ok({"kind": "repeat", "value": value.value, "count": count.value})
    if kind == "block":
        block = _block(left["block"], right["block"], state, mode)
        if block.status != _OK:
            return _expression_child(block, left, right, state, mode)
        return _ok({"kind": "block", "block": block.value})
    return _generalize_expression(left, right, state, mode)


def _context(
    left: dict[str, Any], right: dict[str, Any], state: _State
) -> dict[str, Any] | None:
    if len(left["pointer_anchors"]) != len(right["pointer_anchors"]):
        return None
    anchors = []
    for left_anchor, right_anchor in zip(
        left["pointer_anchors"], right["pointer_anchors"], strict=True
    ):
        left_id = left_anchor["id"]
        right_id = right_anchor["id"]
        if left_id in state.left_anchors or right_id in state.right_anchors:
            return None
        variable = state.allocate("anchor", left_id, right_id)
        state.anchor_pairs[(left_id, right_id)] = variable
        state.left_anchors.add(left_id)
        state.right_anchors.add(right_id)
        source_type = _context_type(
            left_anchor["source_type"], right_anchor["source_type"], state
        )
        target_type = _context_type(
            left_anchor["target_type"], right_anchor["target_type"], state
        )
        if source_type.status != _OK or target_type.status != _OK:
            return None
        anchors.append(
            {
                "id": variable,
                "source_type": source_type.value,
                "target_type": target_type.value,
            }
        )
    result: dict[str, Any] = {"pointer_anchors": anchors}
    for key in (
        "source_type",
        "source_adjusted_type",
        "target_type",
        "target_adjusted_type",
    ):
        type_result = _context_type(left[key], right[key], state)
        if type_result.status != _OK:
            return None
        result[key] = type_result.value
    return result


def _collect_local_identities(value: Any) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            kind = current.get("kind")
            if kind == "literal" or kind in {
                "external",
                "foreign_function",
                "foreign_static",
                "variable",
            }:
                return
            if kind == "local":
                if "owner" in current:
                    visit(current["owner"])
                identity = _identity_key(current["id"])
                if identity is not None:
                    result.add(identity)
                return
            if kind in {"binding", "function", "constant", "static", "method"}:
                identity = _identity_key(current["id"])
                if identity is not None:
                    result.add(identity)
                return
            for child in current.values():
                visit(child)
        elif isinstance(current, list):
            for child in current:
                visit(child)

    visit(value)
    return result


def _carriers_valid(state: _State) -> bool:
    anchors = [
        {("binding", value) for value in state.left_anchors},
        {("binding", value) for value in state.right_anchors},
    ]
    for seed in (0, 1):
        carriers: dict[tuple[str, str], set[tuple[str, int]]] = {}
        for (sort, index), values in state.substitutions.items():
            if sort in _IDENTITY_SORTS:
                identity = _identity_key(values[seed])
                if identity is not None:
                    carriers.setdefault(identity, set()).add((sort, index))
            elif sort == "expression":
                identities = _collect_local_identities(values[seed])
                if identities & anchors[seed]:
                    return False
                for identity in identities:
                    carriers.setdefault(identity, set()).add((sort, index))
        if any(
            len(values) > 1
            for identity, values in carriers.items()
            if identity not in anchors[seed]
        ):
            return False
    return True


def _rewrite_variables(value: Any, mappings: dict[str, dict[int, int]]) -> Any:
    if isinstance(value, dict):
        if value.get("kind") == "variable":
            sort = value["sort"]
            old = value["index"]
            mapping = mappings.setdefault(sort, {})
            if old not in mapping:
                mapping[old] = len(mapping)
            return _variable(sort, mapping[old])
        return {
            key: _rewrite_variables(value[key], mappings)
            for key in _object_order(value)
        }
    if isinstance(value, list):
        return [_rewrite_variables(child, mappings) for child in value]
    return copy.deepcopy(value)


def canonicalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    mappings: dict[str, dict[int, int]] = {}
    context: dict[str, Any] = {}
    anchors = []
    for anchor in rule["pointer_anchors"]:
        anchors.append(
            {
                "id": _rewrite_variables(anchor["id"], mappings),
                "source_type": _rewrite_variables(anchor["source_type"], mappings),
                "target_type": _rewrite_variables(anchor["target_type"], mappings),
            }
        )
    context["pointer_anchors"] = anchors
    for key in (
        "source_type",
        "source_adjusted_type",
        "target_type",
        "target_adjusted_type",
    ):
        context[key] = _rewrite_variables(rule[key], mappings)
    source = _rewrite_variables(rule["source_pattern"], mappings)
    target = _rewrite_variables(rule["target_pattern"], mappings)
    return {
        "source_pattern": source,
        "target_pattern": target,
        "pointer_anchors": context["pointer_anchors"],
        "source_type": context["source_type"],
        "source_adjusted_type": context["source_adjusted_type"],
        "target_type": context["target_type"],
        "target_adjusted_type": context["target_adjusted_type"],
    }


def synthesize_pair(left: dict[str, Any], right: dict[str, Any]) -> PairResult:
    state = _State()
    context = _context(left, right, state)
    if context is None:
        return PairResult(None, PairRejection.CONTEXT)
    source = _expression(
        left["source_expression"], right["source_expression"], state, "source"
    )
    if source.status != _OK:
        return PairResult(None, PairRejection.SOURCE)
    if (
        isinstance(source.value, dict)
        and source.value.get("kind") == "variable"
        and source.value.get("sort") == "expression"
    ):
        return PairResult(None, PairRejection.DEGENERATE_SOURCE)
    target = _expression(
        left["target_expression"], right["target_expression"], state, "target"
    )
    if target.status != _OK:
        return PairResult(None, PairRejection.TARGET_LOOKUP)
    if not _carriers_valid(state):
        return PairResult(None, PairRejection.CARRIER)
    rule = {
        "source_pattern": source.value,
        "target_pattern": target.value,
        "pointer_anchors": context["pointer_anchors"],
        "source_type": context["source_type"],
        "source_adjusted_type": context["source_adjusted_type"],
        "target_type": context["target_type"],
        "target_adjusted_type": context["target_adjusted_type"],
    }
    canonical = canonicalize_rule(rule)
    return PairResult(canonical, None, copy.deepcopy(state.substitutions))


def synthesize_rules(documents: Sequence[ObservationDocument]) -> RuleDocument:
    unique: dict[Any, tuple[dict[str, Any], bool]] = {}
    for document in documents:
        for observation in document.observations:
            key = _freeze(observation)
            if key in unique:
                value, _ = unique[key]
                unique[key] = (value, True)
            else:
                unique[key] = (copy.deepcopy(observation), False)
    values = sorted(unique.values(), key=lambda entry: _semantic_sort_key(entry[0]))
    rules: dict[str, dict[str, Any]] = {}
    for index, (left, repeated) in enumerate(values):
        for right, _ in values[index + 1 :]:
            result = synthesize_pair(left, right)
            if result.rule is not None:
                key = json.dumps(
                    result.rule,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                rules[key] = result.rule
        if repeated:
            result = synthesize_pair(left, left)
            if result.rule is not None:
                key = json.dumps(
                    result.rule,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                rules[key] = result.rule
    result_document = RuleDocument(rules=tuple(rules[key] for key in sorted(rules)))
    return load_rules(rules_to_json(result_document))
