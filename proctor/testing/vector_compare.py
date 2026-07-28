"""Run the vector harness on a run's per-stage outputs and compare.

Every pipeline stage that produces a Rust project leaves it at
``<run_dir>/stages/NN-<id>/out/rust``. We verify each against the case's
vectors and assemble a per-stage comparison — the crat vs.
abstraction-recovery vs. discipline-repair correctness delta.
"""

from __future__ import annotations

from pathlib import Path

from proctor.testing.vector_harness import (
    StageVectorResult,
    VectorComparison,
    VectorHarnessError,
    run_vectors,
    run_vectors_in_place,
)


def stage_rust_outputs(run_dir: Path) -> list[tuple[str, Path]]:
    """(stage_id, translated_rust_dir) for each stage that produced a Rust
    project, in pipeline order."""
    stages_dir = run_dir / "stages"
    if not stages_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for entry in sorted(stages_dir.iterdir(), key=lambda p: p.name):
        rust = entry / "out" / "rust"
        if (rust / "Cargo.toml").is_file():
            # entry name is "NN-<stage_id>"
            stage_id = entry.name.split("-", 1)[1] if "-" in entry.name else entry.name
            out.append((stage_id, rust))
    return out


def compare_stages(
    run_dir: Path,
    case_dir: Path,
    *,
    workdir: Path,
    stages: list[str] | None = None,
    timeout_s: int = 900,
) -> VectorComparison:
    """Verify each stage's Rust output against the case vectors.

    ``stages``: restrict to these stage ids (default: all Rust-producing
    stages). Early stages whose output isn't a runnable artifact (e.g. a
    lib-only c2rust output) surface as a build failure — informative,
    showing where a runnable translation first appears.
    """
    comparison = VectorComparison(case=case_dir.name)
    for stage_id, rust in stage_rust_outputs(run_dir):
        if stages is not None and stage_id not in stages:
            continue
        stage_wd = workdir / stage_id
        stage_wd.mkdir(parents=True, exist_ok=True)
        try:
            report = run_vectors(rust, case_dir, workdir=stage_wd, timeout_s=timeout_s)
            comparison.stages.append(
                StageVectorResult(stage_id=stage_id, report=report)
            )
        except (VectorHarnessError, OSError) as exc:
            comparison.stages.append(
                StageVectorResult(stage_id=stage_id, report=None, error=str(exc))
            )
    return comparison


def verify_final(
    run_dir: Path,
    case_dir: Path,
    *,
    workdir: Path,
    timeout_s: int = 900,
) -> StageVectorResult | None:
    """Verify only the last Rust-producing stage's output."""
    outputs = stage_rust_outputs(run_dir)
    if not outputs:
        return None
    stage_id, rust = outputs[-1]
    try:
        report = run_vectors(rust, case_dir, workdir=workdir, timeout_s=timeout_s)
        return StageVectorResult(stage_id=stage_id, report=report)
    except (VectorHarnessError, OSError) as exc:
        return StageVectorResult(stage_id=stage_id, report=None, error=str(exc))


# --- in-place variants (verify inside a writable corpus workspace) ---------
#
# ``case_dir`` here is the case directory *inside the workspace copy*, and
# ``workspace_root`` is that copy's root. Each stage's output is cycled
# through the case's ``translated_rust`` slot; library (cando) cases can
# only be verified this way. See vector_harness.run_vectors_in_place.


def compare_stages_in_place(
    run_dir: Path,
    case_dir: Path,
    *,
    workspace_root: Path,
    workdir: Path,
    stages: list[str] | None = None,
    timeout_s: int = 900,
) -> VectorComparison:
    """Per-stage verification in place: each stage's Rust output is
    dropped into the case's slot and verified against the workspace."""
    comparison = VectorComparison(case=case_dir.name)
    for stage_id, rust in stage_rust_outputs(run_dir):
        if stages is not None and stage_id not in stages:
            continue
        junit = workdir / f"{stage_id}.xml"
        try:
            report = run_vectors_in_place(
                rust,
                case_dir,
                workspace_root=workspace_root,
                junit_out=junit,
                timeout_s=timeout_s,
            )
            comparison.stages.append(
                StageVectorResult(stage_id=stage_id, report=report)
            )
        except (VectorHarnessError, OSError) as exc:
            comparison.stages.append(
                StageVectorResult(stage_id=stage_id, report=None, error=str(exc))
            )
    return comparison


def verify_final_in_place(
    run_dir: Path,
    case_dir: Path,
    *,
    workspace_root: Path,
    workdir: Path,
    timeout_s: int = 900,
) -> StageVectorResult | None:
    """Verify only the last Rust-producing stage's output, in place."""
    outputs = stage_rust_outputs(run_dir)
    if not outputs:
        return None
    stage_id, rust = outputs[-1]
    junit = workdir / f"{stage_id}.xml"
    try:
        report = run_vectors_in_place(
            rust,
            case_dir,
            workspace_root=workspace_root,
            junit_out=junit,
            timeout_s=timeout_s,
        )
        return StageVectorResult(stage_id=stage_id, report=report)
    except (VectorHarnessError, OSError) as exc:
        return StageVectorResult(stage_id=stage_id, report=None, error=str(exc))
