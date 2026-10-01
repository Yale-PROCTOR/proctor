"""Drive TRACTOR's authoritative test-vector harness against a stage's
translated Rust output.

We do NOT reimplement vector comparison — we invoke the vendored
`runtests.rust` runner (``tools/tractor_runtests``, the pre-Falco direct
harness) and parse its JUnit output. Binary vectors run the translated
``driver`` directly; library vectors run the case's real cando ``runner``.
Neither needs Docker/Falco/root — only cargo/cmake/ninja on PATH.

The newer corpus's own orchestrator (``tools/test_runner``) is also driven
Falco-free at host level (nix + docker), covering the newer-corpus library
cases and B03; only file-change vectors remain Falco-only. The pipeline/bench
path for that is ``no_falco_bench.run_suite_no_falco`` (suite-batched); the
per-case ``run_vectors_no_falco`` below is a thin variant over the same
``tools/test_runner --no-falco`` invocation. See
``plan_docs/falco_integration_notes.md``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_HARNESS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tools" / "tractor_runtests"
)

VectorStatus = Literal["pass", "fail", "skip", "error"]

#: files copied from the corpus case (besides translated_rust) into the
#: temporary corpus the harness runs against.
_CASE_INPUTS = ("test_case", "test_vectors", "runner")


class VectorHarnessError(ValueError):
    """The harness could not be run (setup/invocation problem, not a
    vector failure)."""


@dataclass(frozen=True)
class VectorResult:
    name: str
    status: VectorStatus
    message: str = ""


@dataclass(frozen=True)
class VectorReport:
    case: str
    results: tuple[VectorResult, ...] = ()
    raw_junit: str = ""

    @property
    def build_ok(self) -> bool:
        build = next((r for r in self.results if r.name == "build"), None)
        return build is None or build.status == "pass"

    def _vectors(self) -> list[VectorResult]:
        return [r for r in self.results if r.name != "build"]

    @property
    def passed(self) -> int:
        return sum(1 for r in self._vectors() if r.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for r in self._vectors() if r.status in ("fail", "error"))

    @property
    def skipped(self) -> int:
        return sum(1 for r in self._vectors() if r.status == "skip")

    @property
    def total(self) -> int:
        return len(self._vectors())

    @property
    def ok(self) -> bool:
        """Build succeeded and no vector failed."""
        return self.build_ok and self.failed == 0

    def summary(self) -> dict[str, object]:
        return {
            "case": self.case,
            "build_ok": self.build_ok,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "vectors": {r.name: r.status for r in self._vectors()},
        }


def parse_junit(xml_text: str, case: str = "") -> VectorReport:
    """Parse the harness's JUnit XML into a VectorReport.

    A ``<testcase>`` with no child element is a pass; a child
    ``<failure>``/``<error>``/``<skipped>`` sets the status. The
    ``build`` testcase carries the build outcome.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise VectorHarnessError(f"harness produced invalid JUnit: {exc}") from exc

    results: list[VectorResult] = []
    for testcase in root.iter("testcase"):
        name = testcase.get("name", "")
        if not case:
            case = testcase.get("classname", "")
        status: VectorStatus = "pass"
        message = ""
        for child in testcase:
            tag = child.tag.lower()
            if tag == "failure":
                status = "fail"
            elif tag == "error":
                status = "error"
            elif tag == "skipped":
                status = "skip"
            else:
                continue
            message = child.get("message", "") or (child.text or "").strip()
            break
        results.append(VectorResult(name=name, status=status, message=message))

    return VectorReport(case=case, results=tuple(results), raw_junit=xml_text)


def _assemble_corpus(translated_rust: Path, case_dir: Path, dest_root: Path) -> str:
    """Build a minimal corpus the harness can discover: one case dir with
    translated_rust plus the corpus inputs. Returns the case name."""
    if not (translated_rust / "Cargo.toml").is_file():
        raise VectorHarnessError(
            f"{translated_rust} is not a Cargo project (no Cargo.toml)"
        )
    if not (case_dir / "test_vectors").is_dir():
        raise VectorHarnessError(f"{case_dir} has no test_vectors/")

    case = case_dir.name
    dest = dest_root / case
    dest.mkdir(parents=True)
    shutil.copytree(translated_rust, dest / "translated_rust")
    for name in _CASE_INPUTS:
        src = case_dir / name
        if src.is_dir():
            shutil.copytree(src, dest / name)
    return case


def run_vectors(
    translated_rust: Path,
    case_dir: Path,
    *,
    workdir: Path,
    timeout_s: int = 900,
) -> VectorReport:
    """Verify a translated Rust project against a corpus case's vectors.

    ``translated_rust``: a stage's ``out/rust`` (Cargo project the harness
    builds). ``case_dir``: the source corpus case (provides
    ``test_vectors``, ``test_case``, and ``runner`` for library cases).
    ``workdir``: scratch dir for the assembled corpus and JUnit output.
    """
    corpus = workdir / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    case = _assemble_corpus(translated_rust, case_dir, corpus)
    junit = workdir / "junit.xml"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "runtests.rust",
            "--root",
            str(corpus),
            "--subset",
            case,
            "--junit-xml",
            str(junit),
            "--keep-going",
        ],
        cwd=str(_HARNESS_DIR),
        env={**_harness_env()},
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if not junit.is_file():
        raise VectorHarnessError(
            f"harness produced no JUnit (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout)[-2000:]}"
        )
    return parse_junit(junit.read_text(encoding="utf-8"), case=case)


def _harness_env() -> dict[str, str]:
    """Environment for the harness: PYTHONPATH to the vendored runner,
    plus the caller's PATH (must contain cargo/cmake/ninja)."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_HARNESS_DIR}:{existing}" if existing else str(_HARNESS_DIR)
    return env


# --- in-place verification (required for library / cando cases) -----------
#
# A library case's cando ``runner`` is a member of the corpus Cargo
# workspace and depends on ``tools/cando2`` by a relative path, so it
# only builds with the *whole* workspace present. We therefore verify
# such cases in place inside a writable copy of the corpus: drop the
# stage's Rust into the case's ``translated_rust`` slot and run the
# harness scoped to that case. Binary cases work here too.


def is_library_case(case_dir: Path) -> bool:
    """TRACTOR marks library (cando) cases with a ``_lib`` suffix; they
    ship a ``runner/`` package the harness builds and runs. Matches the
    harness's own ``_is_library`` test."""
    return case_dir.name.endswith("_lib")


def find_workspace_root(case_dir: Path) -> Path | None:
    """The corpus Cargo-workspace root above ``case_dir``: the nearest
    ancestor with a ``[workspace]`` Cargo.toml and a ``tools/cando*``
    crate. Library cases build their cando runner from here, so
    verifying them needs the whole workspace in place. Returns ``None``
    for a corpus without that workspace (e.g. a binary-only subset)."""
    resolved = case_dir.resolve()
    for parent in (resolved, *resolved.parents):
        cargo = parent / "Cargo.toml"
        if (
            cargo.is_file()
            and "[workspace]" in cargo.read_text(encoding="utf-8", errors="ignore")
            and any(parent.glob("tools/cando*"))
        ):
            return parent
    return None


_WS_COPY_IGNORE = shutil.ignore_patterns(".git", "target", "translated_rust")


def copy_workspace(workspace_root: Path, dest: Path) -> Path:
    """Make one writable copy of the corpus workspace (call once per
    bench, before the parallel case loop). Skips ``.git``, build
    ``target`` dirs, and any pre-existing ``translated_rust`` slots. A
    no-op if ``dest`` already exists."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(workspace_root, dest, ignore=_WS_COPY_IGNORE, symlinks=True)
    return dest


def run_vectors_in_place(
    translated_rust: Path,
    case_dir: Path,
    *,
    workspace_root: Path,
    junit_out: Path,
    timeout_s: int = 900,
) -> VectorReport:
    """Verify by dropping ``translated_rust`` into the case's slot inside
    a real (writable) corpus workspace and running the harness there.

    ``case_dir`` is the case directory *inside the writable copy* and
    must live under ``workspace_root`` (also the copy). This is the path
    library cases require; binary cases work here too. Concurrent calls
    on different cases are safe — each writes only its own slot; the
    shared workspace ``target`` used for cando-runner builds is
    serialized by Cargo's own lock.
    """
    case_dir = case_dir.resolve()
    workspace_root = workspace_root.resolve()
    try:
        case_rel = case_dir.relative_to(workspace_root)
    except ValueError as exc:
        raise VectorHarnessError(
            f"{case_dir} is not under workspace root {workspace_root}"
        ) from exc
    if not (translated_rust / "Cargo.toml").is_file():
        raise VectorHarnessError(
            f"{translated_rust} is not a Cargo project (no Cargo.toml)"
        )
    if not (case_dir / "test_vectors").is_dir():
        raise VectorHarnessError(f"{case_dir} has no test_vectors/")

    slot = case_dir / "translated_rust"
    if slot.exists():
        shutil.rmtree(slot)
    shutil.copytree(translated_rust, slot)

    junit_out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "runtests.rust",
            "--root",
            str(workspace_root),
            "--subset",
            str(case_rel),
            "--junit-xml",
            str(junit_out),
            "--keep-going",
        ],
        cwd=str(_HARNESS_DIR),
        env=_harness_env(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if not junit_out.is_file():
        raise VectorHarnessError(
            f"harness produced no JUnit (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout)[-2000:]}"
        )
    return parse_junit(junit_out.read_text(encoding="utf-8"), case=str(case_rel))


# --- newer-harness engine (Falco-free via tools/test_runner --no-falco) ---
#
# The newer TRACTOR corpus ships ``tools/test_runner``, a Docker
# orchestrator that natively matches the newer corpus era: cando2
# (``lib_fn!``, rustc 1.94.1), the ``_cando_librunner`` naming, and B03.
# Yale's ``no-falco`` branch adds ``--no-falco`` to it, so it runs
# state/stdout/lib-state vectors without Falco (file-change vectors are
# skipped). Unlike ``run_vectors`` / ``run_vectors_in_place`` (pure-Python,
# run anywhere), this orchestrator spawns a container per vector, so it runs
# at HOST level and needs ``nix`` + ``docker`` on PATH — it cannot run nested
# inside the framework container. Fetch the corpus with
# ``./fetch_corpus.sh --no-falco``; see ``plan_docs/falco_integration_notes.md``.

#: phase pseudo-tests the newer orchestrator emits per case alongside the
#: real vectors; folded into the report's build outcome, not counted.
_NEWER_PHASE_NAMES = frozenset({"config", "build", "build-runners"})


def _fold_phase_entries(report: VectorReport) -> VectorReport:
    """Collapse the newer harness's ``config``/``build``/``build-runners``
    phase entries into a single ``build`` result (failed if any phase
    failed), so ``passed``/``failed``/``total`` count only real vectors
    while ``build_ok`` still reflects the build."""
    phase_fail = any(
        r.status in ("fail", "error")
        for r in report.results
        if r.name in _NEWER_PHASE_NAMES
    )
    vectors = tuple(r for r in report.results if r.name not in _NEWER_PHASE_NAMES)
    build = VectorResult(name="build", status="fail" if phase_fail else "pass")
    return VectorReport(
        case=report.case, results=(build, *vectors), raw_junit=report.raw_junit
    )


def run_vectors_no_falco(
    translated_rust: Path,
    case_dir: Path,
    *,
    corpus_root: Path,
    junit_out: Path,
    timeout_s: int = 1800,
    env: dict[str, str] | None = None,
) -> VectorReport:
    """Verify a translation with the newer harness (``tools/test_runner
    --no-falco``), which runs at host level.

    Drops ``translated_rust`` into the case's ``translated_rust`` slot in
    place inside the newer corpus workspace at ``corpus_root``, then runs
    ``nix run <corpus_root>/tools/test_runner -- --rust --no-falco
    --subset <case_rel>``. ``case_dir`` must live under ``corpus_root``.

    Requires ``nix`` and ``docker`` reachable via ``env`` (default:
    ``os.environ``). File-change vectors come back skipped; the build/config
    phase entries are folded into the report's build outcome.
    """
    corpus_root = corpus_root.resolve()
    case_dir = case_dir.resolve()
    try:
        case_rel = case_dir.relative_to(corpus_root)
    except ValueError as exc:
        raise VectorHarnessError(
            f"{case_dir} is not under corpus root {corpus_root}"
        ) from exc
    if not (translated_rust / "Cargo.toml").is_file():
        raise VectorHarnessError(
            f"{translated_rust} is not a Cargo project (no Cargo.toml)"
        )
    if not (case_dir / "test_vectors").is_dir():
        raise VectorHarnessError(f"{case_dir} has no test_vectors/")
    runner = corpus_root / "tools" / "test_runner"
    if not (runner / "flake.nix").is_file():
        raise VectorHarnessError(
            f"{runner} is not the newer tools/test_runner (no flake.nix); "
            "fetch it with ./fetch_corpus.sh --no-falco"
        )

    slot = case_dir / "translated_rust"
    if slot.exists():
        shutil.rmtree(slot)
    shutil.copytree(translated_rust, slot)

    junit_out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "nix",
            "run",
            "--extra-experimental-features",
            "nix-command flakes",
            str(runner),
            "--",
            "--rust",
            "--no-falco",
            "--keep-going",
            "--root",
            str(corpus_root),
            "--subset",
            str(case_rel),
            "--junit-xml",
            str(junit_out),
        ],
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if not junit_out.is_file():
        raise VectorHarnessError(
            f"newer harness produced no JUnit (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout)[-2000:]}"
        )
    report = parse_junit(junit_out.read_text(encoding="utf-8"), case=str(case_rel))
    return _fold_phase_entries(report)


@dataclass(frozen=True)
class StageVectorResult:
    stage_id: str
    report: VectorReport | None
    error: str | None = None


@dataclass
class VectorComparison:
    """Per-stage vector results for one case, for comparing how each
    transformation stage affected correctness."""

    case: str
    stages: list[StageVectorResult] = field(default_factory=list)

    def delta_table(self) -> list[dict[str, object]]:
        rows = []
        for sv in self.stages:
            if sv.report is not None:
                rows.append(
                    {
                        "stage": sv.stage_id,
                        "passed": sv.report.passed,
                        "total": sv.report.total,
                        "ok": sv.report.ok,
                    }
                )
            else:
                rows.append({"stage": sv.stage_id, "error": sv.error})
        return rows
