from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

U64_MAX = 2**64 - 1
VALUE_KINDS = frozenset({"Fn", "Static", "Const"})
TYPE_KINDS = frozenset({"TyAlias", "Enum", "Struct", "Union"})
ALL_KINDS = VALUE_KINDS | TYPE_KINDS


class SkeletonError(ValueError):
    pass


class ContextOverflow(ValueError):
    pass


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
        return ItemRecord(
            id=record_id,
            path=path,
            kind=kind,
            name=_string(data, "name", record_id),
            annotated_source=_string(data, "annotated_source", record_id),
            annotated_skeleton=_string(data, "annotated_skeleton", record_id),
            source_signature=_string(data, "source_signature", record_id),
            target_signature=_string(data, "target_signature", record_id),
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
    if record.kind == "Fn":
        return (
            f"### Function {record.id}: {record.path}\n"
            "Source signature:\n"
            f"```rust\n{record.source_signature}\n```\n"
            "Target signature:\n"
            f"```rust\n{record.target_signature}\n```"
        )
    text = (
        record.declaration if record.kind in {"Static", "Const"} else record.definition
    )
    return f"### {record.kind} {record.id}: {record.path}\n```rust\n{text}\n```"


def render_transformation_targets(
    members: tuple[int, ...], records_by_id: dict[int, ItemRecord]
) -> str:
    entries = []
    for item_id in sorted(members):
        record = records_by_id[item_id]
        entries.append(
            f"### Function {record.id}: {record.path}\n"
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
