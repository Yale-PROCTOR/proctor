# © 2026 Massachusetts Institute of Technology
# MIT License

# ci/hooks.py
from pathlib import Path
from typing import Optional, Mapping

from ..build import configure_c_test_case, build_c_test_case, build_lib_harness
from ..cli import Hooks, BuildContext
from ..local_types import TestCase

def _configure(tc: TestCase, ctx: BuildContext) -> None:
    if not ctx.cmake:
        raise FileNotFoundError
    configure_c_test_case(ctx.cmake, tc.test_root, ctx.timeout, ctx.verbose, ctx.fuzz, ctx.asan)

def _build(tc: TestCase, ctx: BuildContext) -> None:
    if not ctx.cmake:
        raise FileNotFoundError
    build_c_test_case(
        ctx.cmake,
        tc.test_root,
        tc.target_dir,
        ctx.jobs,
        ctx.timeout,
        ctx.verbose,
    )

def _build_runner(tc: TestCase, ctx: BuildContext) -> None:
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
    if context.asan:
        env = {}
        env['RUSTFLAGS'] = '-Z sanitizer=address'
        env['ASAN_OPTIONS'] = 'abort_on_error=1:detect_leaks=0'
        return env
    else:
        return None

def make_ci_hooks() -> Hooks:
    """
    C/CMake mode:
      - configure + build use CMake
      - library cases also build the Cando runner using cargo unless --skip-lib-tests is provided
    """

    return Hooks(configure=_configure,
                 build=_build,
                 build_runner=_build_runner,
                 generate_build_environment=_generate_build_environment,
                 generate_run_environment=_generate_run_environment)
