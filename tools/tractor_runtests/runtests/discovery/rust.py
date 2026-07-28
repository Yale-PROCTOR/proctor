# © 2026 Massachusetts Institute of Technology
# MIT License

from pathlib import Path
from .core import DiscoveryProfile


def _is_case_dir_rust(p: Path) -> bool:
    return (p / "translated_rust").exists() and (p / "test_vectors").exists()


def _resolve_case_paths_rust(case_root: Path) -> tuple[Path, Path, Path, Path]:
    """
    Resolve the build directory and target directory from the test case's root.
    Return value is a tuple (build directory, target directory, runtime binary directory, runner directory)
    """
    build_project_dir = (case_root / "translated_rust").resolve()
    target_dir = build_project_dir / "target"
    runtime_bin_dir = target_dir / "release"
    runner_dir = (case_root / "runner").resolve()
    return (build_project_dir, target_dir, runtime_bin_dir, runner_dir)


RUST_PROFILE = DiscoveryProfile(
    name="rust",
    is_case_dir=_is_case_dir_rust,
    resolve_case_paths=_resolve_case_paths_rust,
)
