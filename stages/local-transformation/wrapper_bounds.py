from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from proctor.llm.types import LlmError, Message, Request, RequestMetadata, Response
from proctor.prompts.library import PromptLibrary
from tooling import CommandResult, StageFailure, write_json


def _sources(project: Path) -> Iterator[Path]:
    for directory, directories, names in os.walk(project, followlinks=False):
        root = Path(directory)
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".")
            and not (root / name).is_symlink()
            and not (root == project and name in {"target", "tests", "runner"})
        )
        for name in sorted(names):
            path = root / name
            if path.suffix == ".rs" and name != "build.rs" and not path.is_symlink():
                yield path


def _syntax(
    tools: Any, directory: Path, source: str, **replacement: Any
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    request = directory / "request.json"
    output = directory / "response.json"
    write_json(request, {"source": source, **replacement})
    result = tools.wrapper_bounds(request, output)
    if (
        not isinstance(result, dict)
        or set(result) != {"wrappers", "candidate"}
        or not isinstance(result["wrappers"], list)
    ):
        raise ValueError("invalid wrapper bounds tool response")
    return result


def infer_wrapper_bounds(
    project: Path,
    *,
    workdir: Path,
    stage_dir: Path,
    tools: Any,
    complete: Callable[[Request], Response],
    build: Callable[[Path], CommandResult],
    run_id: str,
    dump_exchanges: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "no_wrappers", "wrappers": [], "errors": []}
    scratch = workdir / "wrapper-bounds"
    template = None
    for file_index, path in enumerate(_sources(project)):
        relative = str(path.relative_to(project))
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            report["errors"].append({"file": relative, "error": str(exc)})
            continue
        if "from_raw_parts" not in source or "1000000" not in source.replace("_", ""):
            continue
        if template is None:
            if scratch.exists():
                shutil.rmtree(scratch)
            template = PromptLibrary(stage_dir / "prompts").get(
                "wrapper_bound_inference", 1
            )
        discovery = scratch / "files" / f"{file_index:06d}"
        try:
            wrappers = _syntax(tools, discovery, source)["wrappers"]
        except (StageFailure, ValueError, OSError) as exc:
            report["errors"].append({"file": relative, "error": str(exc)})
            continue
        for target in wrappers:
            directory = scratch / "wrappers" / f"{len(report['wrappers']):06d}"
            entry: dict[str, Any] = {"file": relative, "wrapper": target["path"]}
            report["wrappers"].append(entry)
            before = path.read_bytes()
            try:
                current = _syntax(tools, directory / "current", before.decode("utf-8"))
                target = next(
                    (
                        item
                        for item in current["wrappers"]
                        if item["path"] == entry["wrapper"]
                    ),
                    None,
                )
                if target is None:
                    entry.update(status="unchanged", reason="no_remaining_bounds")
                    continue
                originals = [site["text"] for site in target["sites"]]
                entry["original_bounds"] = originals
                rendered = template.render(
                    wrapper=target["wrapper"],
                    internal=target["internal"],
                    bounds=originals,
                )
                entry["prompt_hash"] = rendered.content_hash
                request = Request(
                    messages=(Message(role="user", content=rendered.text),),
                    metadata=RequestMetadata(
                        run_id=run_id,
                        stage="local_transformation",
                        item=f"{relative}:{entry['wrapper']}",
                        prompt_id=rendered.id,
                        prompt_version=rendered.version,
                        prompt_hash=rendered.content_hash,
                    ),
                )
                if dump_exchanges:
                    (directory / "prompt.md").write_text(
                        rendered.text, encoding="utf-8"
                    )
                try:
                    response = complete(request)
                except (LlmError, ValueError, OSError) as exc:
                    entry.update(status="unchanged", reason="llm_error", error=str(exc))
                    continue
                entry.update(provider=response.provider, model=response.model)
                if dump_exchanges:
                    (directory / "response.txt").write_text(
                        response.text, encoding="utf-8"
                    )
                if response.finish_reason != "stop":
                    entry.update(status="unchanged", reason="incomplete_response")
                    continue
                try:
                    expressions = json.loads(response.text)
                except ValueError:
                    expressions = None
                if (
                    not isinstance(expressions, list)
                    or len(expressions) != len(originals)
                    or not all(
                        isinstance(value, str) and value.strip()
                        for value in expressions
                    )
                ):
                    entry.update(status="unchanged", reason="invalid_response")
                    continue
                expressions = [value.strip() for value in expressions]
                entry["proposed_bounds"] = expressions
                if expressions == originals:
                    entry.update(status="unchanged", reason="original_bounds_returned")
                    continue
                candidate = _syntax(
                    tools,
                    directory / "replacement",
                    before.decode("utf-8"),
                    wrapper=entry["wrapper"],
                    expressions=expressions,
                )["candidate"]
                if not isinstance(candidate, str):
                    raise ValueError("wrapper bound replacement did not return source")
                candidate_bytes = candidate.encode("utf-8")
                if candidate_bytes == before:
                    entry.update(status="unchanged", reason="original_bounds_returned")
                    continue
            except (StageFailure, ValueError, OSError) as exc:
                entry.update(
                    status="unchanged", reason="invalid_replacement", error=str(exc)
                )
                continue
            try:
                path.write_bytes(candidate_bytes)
                result = build(project)
                entry["build_returncode"] = result.returncode
                entry["build_diagnostics"] = (result.stdout + result.stderr)[-12000:]
                if result.returncode != 0:
                    entry.update(status="reverted", reason="build_failed")
                elif path.read_bytes() != candidate_bytes:
                    entry.update(status="reverted", reason="build_changed_source")
                else:
                    entry.update(status="applied", reason="build_succeeded")
            except Exception as exc:
                entry.update(status="reverted", reason="build_error", error=str(exc))
            finally:
                if entry.get("status") != "applied":
                    path.write_bytes(before)
    if report["wrappers"]:
        report["status"] = "completed"
    elif report["errors"]:
        report["status"] = "detection_failed"
    return report
