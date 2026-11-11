"""Measure what a stage changed: files and lines, and the exported ABI.

Drives ``tools/diff_metrics`` (our ``syn`` before/after diff), the same way
``unsafe_eval`` drives ``measure_unsafety``: built on demand, shelled by path.
It's syntactic, so it needs no toolchain beyond ``cargo``/``rustfmt`` and works
even if a crate doesn't compile.

Line counts are rustfmt-normalized (a stage that only reformats shows zero
churn). The ABI diff compares exported symbols, their signatures, and the
layout of crate types reachable from them (see the tool's ``PROVENANCE.md``).
:func:`public_api` reads the C headers so the diff covers the real API: c2rust
exports every non-``static`` function, and a later stage internalizing the ones
the header doesn't declare is not a break; a layout change behind a pointer
breaks only if the header defines that type's body.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proctor.contracts.manifest import ManifestError, ProjectManifest

_TOOL_DIR = Path(__file__).resolve().parent.parent.parent / "tools" / "diff_metrics"
_BIN = _TOOL_DIR / "target" / "release" / "diff_metrics"


class DiffEvalError(RuntimeError):
    """diff_metrics could not be built or run (not a finding about the code)."""


@dataclass(frozen=True)
class PublicApi:
    """What the C headers make public: symbols (functions and globals) and the
    types whose body they define (C code may read those types' fields)."""

    symbols: frozenset[str]
    types: frozenset[str] = frozenset()

    def to_api_file(self) -> str:
        """The tool's ``--api`` format."""
        lines = [f"symbol {s}" for s in sorted(self.symbols)]
        lines += [f"type {t}" for t in sorted(self.types)]
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class DiffReport:
    """What changed between two crates. ``abi_*`` are lists of symbol names;
    ``abi_ok`` is the tool's verdict (see ``tools/diff_metrics``)."""

    files_changed: int
    lines_added: int
    lines_removed: int
    abi_ok: bool
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    abi_removed: list[str] = field(default_factory=list)
    abi_added: list[str] = field(default_factory=list)
    abi_changed: list[str] = field(default_factory=list)
    abi_removed_bin_internal: list[str] = field(default_factory=list)
    #: ``{name, kind, by_value, public_layout, breaking, before, after}``
    abi_types_changed: list[dict[str, Any]] = field(default_factory=list)
    api_restricted: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def abi_breaking_types(self) -> list[str]:
        return [t["name"] for t in self.abi_types_changed if t.get("breaking")]

    def summary(self) -> str:
        if self.abi_ok:
            abi = "ABI kept"
        else:
            parts = [
                f"{len(self.abi_removed)} removed" if self.abi_removed else "",
                f"{len(self.abi_changed)} changed" if self.abi_changed else "",
                f"{len(self.abi_breaking_types)} layout(s) changed"
                if self.abi_breaking_types
                else "",
            ]
            abi = "ABI: " + (", ".join(p for p in parts if p) or "see report")
        return (
            f"{self.files_changed} file(s) changed, +{self.lines_added} "
            f"-{self.lines_removed} lines; {abi}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "per_file": {
                rel: {
                    "status": f.get("status"),
                    "lines_added": f.get("lines_added", 0),
                    "lines_removed": f.get("lines_removed", 0),
                }
                for rel, f in self.files.items()
            },
            "abi_ok": self.abi_ok,
            "abi_removed": self.abi_removed,
            "abi_added": self.abi_added,
            "abi_changed": self.abi_changed,
            "abi_removed_bin_internal": self.abi_removed_bin_internal,
            "abi_types_changed": self.abi_types_changed,
            "api_restricted": self.api_restricted,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DiffReport:
        abi = d.get("abi", {})
        return cls(
            files_changed=int(d.get("files_changed", 0)),
            lines_added=int(d.get("lines_added", 0)),
            lines_removed=int(d.get("lines_removed", 0)),
            abi_ok=bool(abi.get("ok", False)),
            files=dict(d.get("files", {})),
            abi_removed=list(abi.get("removed", [])),
            abi_added=list(abi.get("added", [])),
            abi_changed=[c.get("name", "") for c in abi.get("changed", [])],
            abi_removed_bin_internal=list(abi.get("removed_bin_internal", [])),
            abi_types_changed=list(abi.get("types_changed", [])),
            api_restricted=bool(abi.get("api_restricted", False)),
            raw=d,
        )


def ensure_built(*, timeout_s: int = 900) -> Path:
    """Build diff_metrics on demand (stable rust)."""
    if _BIN.is_file():
        return _BIN
    proc = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=str(_TOOL_DIR),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0 or not _BIN.is_file():
        raise DiffEvalError(
            f"failed to build diff_metrics:\n{(proc.stderr or proc.stdout)[-1500:]}"
        )
    return _BIN


def is_program(crate: Path) -> bool:
    """Whether the crate is a program (``proctor.toml`` ``target_kind =
    "executable"``). CRAT emits a program as a library holding all the code plus
    a ``[[bin]]`` that calls it, so its exports look like an API, but nothing
    outside the crate calls them."""
    try:
        return ProjectManifest.load(crate).target_kind == "executable"
    except ManifestError:
        return False


def measure_diff(
    before: Path,
    after: Path,
    *,
    api: PublicApi | None = None,
    timeout_s: int = 300,
) -> DiffReport:
    """Diff two Cargo projects (e.g. consecutive stages' ``out/rust``). With
    ``api``, the ABI diff covers only those symbols, and layout changes behind
    a pointer break only for the types the headers define. For a program
    (:func:`is_program`) the API is empty: no export or type is public."""
    if is_program(before):
        api = PublicApi(frozenset())
    binary = ensure_built()
    cmd = [str(binary), "--before", str(before), "--after", str(after)]
    with tempfile.TemporaryDirectory() as td:
        if api is not None:
            api_file = Path(td) / "api.txt"
            api_file.write_text(api.to_api_file(), encoding="utf-8")
            cmd += ["--api", str(api_file)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise DiffEvalError(f"diff_metrics timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        raise DiffEvalError(
            f"diff_metrics failed: {(proc.stderr or proc.stdout)[-800:]}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DiffEvalError(f"diff_metrics produced non-JSON output: {exc}") from exc
    return DiffReport.from_json(data)


# A C function declaration/definition head in a header: `<type> name(`.
_DECL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_NOT_FUNCS = frozenset(
    {
        "if",
        "while",
        "for",
        "switch",
        "return",
        "sizeof",
        "defined",
        "typedef",
        "__attribute__",
        "__declspec",
        "_Static_assert",
        "static_assert",
    }
)


# A global declared in a header: `extern <type> name;` / `extern <type> name[N];`
_GLOBAL = re.compile(
    r"\bextern\b(?!\s*\")[^;{}()]*?\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;"
)
# A tagged type defined with a body: `struct tag {`
_TAGGED_BODY = re.compile(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\s*\{")
# The start of a typedef'd body: `typedef struct [tag] {`
_TYPEDEF_BODY = re.compile(r"\btypedef\s+(?:struct|union|enum)\b[^{;]*\{")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _typedef_body_names(text: str) -> set[str]:
    """Names given to typedef'd bodies: `typedef struct {..} Name;`."""
    names: set[str] = set()
    for m in _TYPEDEF_BODY.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        end = text.find(";", i)
        if end != -1:
            names.update(_IDENT.findall(text[i:end]))
    return names


def public_api(c_project: Path) -> PublicApi | None:
    """The C project's public API, read from its headers (``include/``, or every
    ``*.h`` if there is no ``include/``): function and global names, and the
    types whose body the headers define. Best-effort: strips comments,
    preprocessor lines, function-pointer declarators and ALL_CAPS macro names.

    Returns None — "don't restrict the ABI diff" — when no symbol is found, or
    when a header builds names with ``##`` token pasting (the real API is
    macro-generated and can't be read off the source; restricting to fragments
    would hide real ABI changes). An unreadable header raises ``OSError``
    rather than silently shrinking the API."""
    inc = c_project / "include"
    headers = (
        sorted(inc.rglob("*.h")) if inc.is_dir() else sorted(c_project.rglob("*.h"))
    )
    symbols: set[str] = set()
    types: set[str] = set()
    for h in headers:
        text = h.read_text(encoding="utf-8", errors="replace")
        if "##" in text:
            return None
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        text = re.sub(r"//[^\n]*", " ", text)
        text = "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
        )
        types.update(_TAGGED_BODY.findall(text))
        types.update(_typedef_body_names(text))
        symbols.update(_GLOBAL.findall(text))
        # function-pointer declarators with their parameter list: `(*cb)(...)`
        text = re.sub(r"\(\s*\*\s*\w*\s*\)\s*\([^()]*\)", " ", text)
        for m in _DECL.finditer(text):
            name = m.group(1)
            if name not in _NOT_FUNCS and not name.isupper():
                symbols.add(name)
    if not symbols:
        return None
    return PublicApi(frozenset(symbols), frozenset(types))
