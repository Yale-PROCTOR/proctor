"""Hash-chain checkpoints for resume (plan §4).

Each stage's key is::

    hash(stage id, stage fingerprint, resolved stage config hash,
         framework schema_version, upstream stage's checkpoint key)

No artifact-tree hashing: run-dir intermediates are assumed untouched
(``proctor resume --from <stage>`` is the escape hatch when they're not).

The stage fingerprint is the stage directory's git commit SHA; when the
directory has uncommitted changes, a digest of the dirty file contents
is appended so edits during development still invalidate correctly.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from proctor.contracts.stage_io import SCHEMA_VERSION

CHECKPOINT_NAME = ".checkpoint"


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _hash_files(base: Path, rel_paths: list[str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(rel_paths):
        file = base / rel
        digest.update(rel.encode())
        if file.is_file():
            digest.update(file.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _hash_tree(base: Path) -> str:
    skip = {"target", ".venv", "__pycache__", ".git"}
    rel_paths = [
        str(p.relative_to(base))
        for p in base.rglob("*")
        if p.is_file() and not (set(p.relative_to(base).parts) & skip)
    ]
    return _hash_files(base, rel_paths)


def stage_fingerprint(stage_dir: Path) -> str:
    """Identify the stage's code state: commit SHA, plus a digest of any
    uncommitted changes under the stage directory. Falls back to a full
    content hash outside git."""
    head = _run_git(["rev-parse", "HEAD"], stage_dir)
    if head is None:
        return f"tree:{_hash_tree(stage_dir)}"
    fingerprint = f"git:{head.strip()}"
    porcelain = _run_git(["status", "--porcelain", "--", "."], stage_dir)
    if porcelain:
        dirty_files = [line[3:].strip() for line in porcelain.splitlines() if line]
        fingerprint += f"+dirty:{_hash_files(stage_dir, dirty_files)}"
    return fingerprint


def config_hash(
    config: dict[str, Any],
    llm: dict[str, Any],
    timeout_s: int | None,
    *,
    no_fail: bool = False,
) -> str:
    canonical = json.dumps(
        {"config": config, "llm": llm, "timeout_s": timeout_s, "no_fail": no_fail},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def chain_key(
    stage_id: str,
    fingerprint: str,
    cfg_hash: str,
    upstream_key: str | None,
) -> str:
    canonical = json.dumps(
        {
            "stage_id": stage_id,
            "fingerprint": fingerprint,
            "config": cfg_hash,
            "schema_version": SCHEMA_VERSION,
            "upstream": upstream_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_checkpoint(stage_run_dir: Path, key: str, status: str) -> None:
    (stage_run_dir / CHECKPOINT_NAME).write_text(
        json.dumps({"key": key, "status": status}, indent=2) + "\n",
        encoding="utf-8",
    )


def read_checkpoint(stage_run_dir: Path) -> tuple[str, str] | None:
    """Returns (key, status) or None when absent/corrupt."""
    file = stage_run_dir / CHECKPOINT_NAME
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    key = data.get("key")
    status = data.get("status")
    if not isinstance(key, str) or not isinstance(status, str):
        return None
    return key, status
