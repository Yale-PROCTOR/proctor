# © 2026 Massachusetts Institute of Technology
# MIT License

from pathlib import Path
from .core import DiscoveryProfile
from ..build import preset_build_dir


def _is_case_dir_c(p: Path) -> bool:
    return (p / "test_case").exists() and (p / "test_vectors").exists()


def _resolve_case_paths_c(case_root: Path) -> tuple[Path, Path, Path, Path]:
    """
    Resolve the build directory and target directory from the test case's root.
    Return value is a tuple (build directory, target directory)
    """
    build_project_dir = case_root.resolve()
    preset_dir = preset_build_dir(build_project_dir, "test")
    target_dir = (preset_dir or (build_project_dir / "build-ninja")).resolve()
    runtime_bin_dir = target_dir
    runner_dir = build_project_dir / "runner"
    return (build_project_dir, target_dir, runtime_bin_dir, runner_dir)


C_PROFILE = DiscoveryProfile(
    name="c",
    is_case_dir=_is_case_dir_c,
    resolve_case_paths=_resolve_case_paths_c,
)
