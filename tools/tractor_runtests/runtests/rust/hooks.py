# © 2026 Massachusetts Institute of Technology
# MIT License

# rust/hooks.py
from pathlib import Path
from typing import Optional, Mapping

from ..build import build_rust_test_case, build_lib_harness
from ..cli import Hooks, BuildContext
from ..local_types import TestCase

def _build(tc: TestCase, ctx: BuildContext) -> None:
    """Module-level build function (picklable)."""
    if not ctx.cargo:
        raise FileNotFoundError
    build_rust_test_case(
        ctx.cargo,
        tc.build_dir,
        tc.target_dir,
        ctx.timeout,
        ctx.verbose,
    )

def _build_runner(tc: TestCase, ctx: BuildContext) -> None:
    """Module-level library harness builder (picklable)."""
    if ctx.skip_lib_tests or not tc.is_library:
        return
    runner_name = f"_{Path(tc.rel_name).name}_runner"
    if not ctx.cargo:
        raise FileNotFoundError
    build_lib_harness(
        ctx.cargo,
        tc.repo_root,
        runner_name,
        ctx.timeout,
        ctx.verbose,
        ctx.asan,
    )

def _generate_build_environment(context: BuildContext) -> Optional[Mapping[str, str]]:
    return None

def _generate_run_environment(context: BuildContext) -> Optional[Mapping[str, str]]:
    return {"RUST_ARTIFACTS":""}

def make_rust_hooks() -> Hooks:
    """
    Rust dynamic mode:
      - no configure step
      - build translated with Rust with Cargo
      - library cases also build the Cando runner using cargo unless --skip-lib-tests is provided
      - environment overrides artifacts directory to look for cargo-specific placement
    """
    return Hooks(configure=None,
                 build=_build,
                 build_runner=_build_runner,
                 generate_build_environment=_generate_build_environment,
                 generate_run_environment=_generate_run_environment)
