from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any

from final_validation import _copy_project
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


def _build_edits(
    project: Path,
    edits: dict[Path, tuple[bytes, bytes]],
    tools: Any,
    build: Callable[[Path, Any], CommandResult],
) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    try:
        for path, (_, candidate) in edits.items():
            path.write_bytes(candidate)
        result = build(project, tools)
        outcome["build_returncode"] = result.returncode
        outcome["build_diagnostics"] = (result.stdout + result.stderr)[-12000:]
        if result.returncode != 0:
            outcome.update(status="reverted", reason="build_failed")
        elif any(
            path.read_bytes() != candidate for path, (_, candidate) in edits.items()
        ):
            outcome.update(status="reverted", reason="build_changed_source")
        else:
            outcome.update(status="applied", reason="build_succeeded")
    except Exception as exc:
        outcome.update(status="reverted", reason="build_error", error=str(exc))
    finally:
        if outcome.get("status") != "applied":
            for path, (before, _) in edits.items():
                path.write_bytes(before)
    return outcome


def _apply_bounds(
    project: Path,
    path: Path,
    before: bytes,
    directory: Path,
    entry: dict[str, Any],
    tools: Any,
    build: Callable[[Path, Any], CommandResult],
) -> None:
    for key in ("build_returncode", "build_diagnostics", "error"):
        entry.pop(key, None)
    try:
        candidate = _syntax(
            tools,
            directory,
            before.decode("utf-8"),
            wrapper=entry["wrapper"],
            expressions=entry["proposed_bounds"],
        )["candidate"]
        if not isinstance(candidate, str):
            raise ValueError("wrapper bound replacement did not return source")
        candidate_bytes = candidate.encode("utf-8")
    except (StageFailure, ValueError, OSError) as exc:
        entry.update(status="unchanged", reason="invalid_replacement", error=str(exc))
        return
    if candidate_bytes == before:
        entry.update(status="unchanged", reason="original_bounds_returned")
        return
    entry.update(_build_edits(project, {path: (before, candidate_bytes)}, tools, build))


def infer_wrapper_bounds(
    project: Path,
    *,
    workdir: Path,
    stage_dir: Path,
    tools: Any,
    make_complete: Callable[[], Callable[[Request], Response]],
    build: Callable[[Path, Any], CommandResult],
    run_id: str,
    workers: int = 1,
    dump_exchanges: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "no_wrappers", "wrappers": [], "errors": []}
    scratch = workdir / "wrapper-bounds"
    template = None
    jobs: list[tuple[Path, range]] = []
    entries: list[dict[str, Any]] = report["wrappers"]
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
        if wrappers:
            start = len(entries)
            entries.extend(
                {"file": relative, "wrapper": target["path"]} for target in wrappers
            )
            jobs.append((Path(relative), range(start, len(entries))))

    if not jobs:
        if report["errors"]:
            report["status"] = "detection_failed"
        return report
    assert template is not None
    report["status"] = "completed"

    def infer_file(
        current_project: Path,
        relative: Path,
        indices: range,
        file_tools: Any,
        complete: Callable[[Request], Response],
    ) -> None:
        path = current_project / relative
        for index in indices:
            directory = scratch / "wrappers" / f"{index:06d}"
            entry = entries[index]
            before = path.read_bytes()
            try:
                current = _syntax(
                    file_tools, directory / "current", before.decode("utf-8")
                )
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
            except (StageFailure, ValueError, OSError) as exc:
                entry.update(
                    status="unchanged", reason="invalid_replacement", error=str(exc)
                )
                continue
            _apply_bounds(
                current_project,
                path,
                before,
                directory / "replacement",
                entry,
                file_tools,
                build,
            )

    count = min(workers, len(jobs))
    if count == 1:
        complete = make_complete()
        for relative, indices in jobs:
            infer_file(project, relative, indices, tools, complete)
        return report

    pending = iter(jobs)
    dispatch = Lock()
    stopped = False
    accepted = scratch / "accepted"
    logs = scratch / "workers"
    logs.mkdir()

    def worker(index: int, projects: Path) -> None:
        nonlocal stopped
        current_project = projects / f"{index:02d}"
        try:
            _copy_project(project, current_project)
            file_tools = tools.fork(
                logs / f"worker-{index:02d}.log", current_project / "target"
            )
            complete = make_complete()
            while True:
                with dispatch:
                    job = None if stopped else next(pending, None)
                if job is None:
                    return
                relative, indices = job
                infer_file(current_project, relative, indices, file_tools, complete)
                if any(entries[index]["status"] == "applied" for index in indices):
                    path = current_project / relative
                    candidate = accepted / relative
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, candidate)
                    shutil.copyfile(project / relative, path)
        except Exception:
            with dispatch:
                stopped = True
            raise

    with (
        TemporaryDirectory(dir=scratch, prefix="projects-") as projects,
        ThreadPoolExecutor(
            max_workers=count, thread_name_prefix="wrapper-bounds"
        ) as pool,
    ):
        futures = [pool.submit(worker, index, Path(projects)) for index in range(count)]
        for future in futures:
            future.result()

    edits = {}
    for relative, _ in jobs:
        candidate = accepted / relative
        if candidate.is_file():
            path = project / relative
            edits[path] = path.read_bytes(), candidate.read_bytes()
    if not edits:
        return report
    merged = _build_edits(project, edits, tools, build)
    report["merged_build"] = merged
    if merged["status"] == "applied":
        return report

    # The failed merged build restored the baseline. Recheck saved proposals
    # against the evolving project in discovery order.
    for relative, indices in jobs:
        path = project / relative
        for index in indices:
            entry = entries[index]
            if entry["status"] in {"applied", "reverted"}:
                directory = scratch / "wrappers" / f"{index:06d}" / "replay"
                _apply_bounds(
                    project, path, path.read_bytes(), directory, entry, tools, build
                )
    return report
