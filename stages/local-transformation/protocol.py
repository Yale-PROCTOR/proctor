from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from model import ItemRecord

from proctor.llm.types import Message, Request, RequestMetadata
from proctor.prompts.library import PromptLibrary, RenderedPrompt

NO_FENCE_DIAGNOSTIC = (
    "The LLM response contained no triple-backtick fenced code block; return "
    "exactly one triple-backtick fenced Rust code block."
)

_OPENING = re.compile(r"(?m)^```(?!`)([A-Za-z0-9_+-]+)?(?:\r\n|\n)")
_CLOSING = re.compile(r"(?m)^```(?!`)(?:\r\n|\n|$)")


@dataclass(frozen=True)
class Extraction:
    candidate: str | None
    failed_text: str | None
    diagnostics: str | None


@dataclass(frozen=True)
class PromptRenderInput:
    dependency_context: str
    transformation_targets: str
    failed_transformation: str | None = None
    diagnostics: str | None = None


def extract_code_block(response: str) -> Extraction:
    blocks: list[str] = []
    cursor = 0
    while True:
        opening = _OPENING.search(response, cursor)
        if opening is None:
            break
        closing = _CLOSING.search(response, opening.end())
        if closing is None:
            cursor = opening.end()
            continue
        content = response[opening.end() : closing.start()]
        if content.endswith("\r\n"):
            content = content[:-2]
        elif content.endswith("\n"):
            content = content[:-1]
        blocks.append(content)
        cursor = closing.end()
    if not blocks:
        return Extraction(None, response, NO_FENCE_DIAGNOSTIC)
    candidate = max(enumerate(blocks), key=lambda pair: (len(pair[1]), -pair[0]))[1]
    return Extraction(candidate, None, None)


def validation_request(
    members: tuple[int, ...],
    records_by_id: dict[int, ItemRecord],
    transformation: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "expected_functions": [
            {
                "id": records_by_id[item_id].id,
                "name": records_by_id[item_id].name,
                "skeleton": records_by_id[item_id].annotated_skeleton,
                "needs_transformation": records_by_id[item_id].needs_transformation,
                "statements_requiring_transformation": list(
                    records_by_id[item_id].statements_requiring_transformation
                ),
            }
            for item_id in sorted(members)
        ],
        "transformation": transformation,
    }


def replacement_request(
    members: tuple[int, ...],
    records_by_id: dict[int, ItemRecord],
    transformation: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "items": [
            {
                "id": records_by_id[item_id].id,
                "path": records_by_id[item_id].path,
                "name": records_by_id[item_id].name,
                "skeleton": records_by_id[item_id].annotated_skeleton,
                "needs_transformation": records_by_id[item_id].needs_transformation,
                "statements_requiring_transformation": list(
                    records_by_id[item_id].statements_requiring_transformation
                ),
            }
            for item_id in sorted(members)
        ],
        "transformation": transformation,
    }


def repair_context(failed_transformation: str, diagnostics: str) -> str:
    return (
        "The previous transformation failed.\n\n"
        "Previous transformation:\n\n"
        f"```rust\n{failed_transformation}\n```\n\n"
        "Diagnostics:\n\n"
        f"```text\n{diagnostics}\n```\n\n"
        "Regenerate every function in Transformation Targets."
    )


def render_prompt(
    value: PromptRenderInput, prompt_dir: Path | None = None
) -> RenderedPrompt:
    directory = prompt_dir or Path(__file__).parent / "prompts"
    template = PromptLibrary(directory).get("local_transformation", version=1)
    repair = ""
    if value.failed_transformation is not None and value.diagnostics is not None:
        repair = repair_context(value.failed_transformation, value.diagnostics)
    return template.render(
        dependency_context=value.dependency_context,
        transformation_targets=value.transformation_targets,
        repair_context=repair,
    )


def llm_request(
    rendered: RenderedPrompt,
    *,
    run_id: str,
    members: tuple[int, ...],
) -> Request:
    return Request(
        messages=(Message(role="user", content=rendered.text),),
        metadata=RequestMetadata(
            run_id=run_id,
            stage="local_transformation",
            item=",".join(str(item_id) for item_id in sorted(members)),
            prompt_id=rendered.id,
            prompt_version=rendered.version,
            prompt_hash=rendered.content_hash,
        ),
    )


def make_skeleton_command(
    crat_tool: Path, current_project: Path, output: Path
) -> list[str]:
    return [
        str(crat_tool),
        "make-skeleton",
        "--output",
        str(output),
        str(current_project),
    ]


def normalize_safety_command(
    crat_tool: Path, library_source: Path, output: Path
) -> list[str]:
    return [
        str(crat_tool),
        "normalize-safety",
        "--output",
        str(output),
        str(library_source),
    ]


def validate_command(crat_tool: Path, request: Path, response: Path) -> list[str]:
    return [
        str(crat_tool),
        "validate",
        "--input",
        str(request),
        "--output",
        str(response),
    ]


def replace_command(
    crat_tool: Path,
    current_project: Path,
    request: Path,
    output: Path,
    statement_pairs_output: Path,
) -> list[str]:
    return [
        str(crat_tool),
        "replace",
        "--request",
        str(request),
        "--output",
        str(output),
        "--statement-pairs-output",
        str(statement_pairs_output),
        str(current_project),
    ]
