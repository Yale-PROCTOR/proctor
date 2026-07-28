# © 2026 Massachusetts Institute of Technology
# MIT License

from contextlib import contextmanager
import argparse, os, sys, time
from argparse import ArgumentParser
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Callable, Optional, Mapping, Protocol

from .runners.exec_runner import ExecRunner
from .runners.lib_runner import LibRunner
from .evaluation import perf_metrics, valgrind_metrics
from .reporters.console import print_step, print_summary, print_failures, Status
from .reporters.junit_xml import write_junit_xml
from .local_types import JUnitCase, TestCase, TestOutcome
from .utils import run_command


@dataclass
class PhaseResult:
    """Result of running a single phase on a test case."""
    test_case: TestCase
    success: bool
    error_msg: Optional[str] = None
    outcomes: Optional[list[TestOutcome]] = None

@dataclass
class RunReport:
    totals: dict
    junit_suites: dict[str, list["JUnitCase"]]
    failures: list[tuple[str, str]]  # (case_name, message)

    @property
    def exit_code(self) -> int:
        return (0 if len(self.totals["failed_cases"]) == 0 else 1)

@dataclass
class BuildContext:
    """Context needed for building - must be picklable."""
    cmake: Optional[str]
    cargo: Optional[str]
    jobs: Optional[int]
    timeout: Optional[float]
    verbose: bool
    skip_lib_tests: bool
    fuzz: bool
    asan: bool

@dataclass
class Hooks:
    configure: Optional[Callable[[TestCase, BuildContext], None]] = None
    build: Optional[Callable[[TestCase, BuildContext], None]] = None
    build_runner: Optional[Callable[[TestCase, BuildContext], None]] = None
    generate_build_environment: Optional[Callable[[BuildContext], Optional[Mapping[str,str]]]] = None
    generate_run_environment: Optional[Callable[[BuildContext], Optional[Mapping[str,str]]]] = None

PROJECT_DIRS = ["Backup-Tests", "Hidden-Tests", "Public-Tests"]


class ParsedArguments(Protocol):
    root: Path  # Path to root
    jobs: Optional[int]  # Parallel build and configure jobs (default = use all cores)
    match_regex: list[str]  # Regex to select test cases by their relative path; can be repeated
    subset: list[Path]  # directories to search for tests; can be repeated.  Relative to --root unless absolute.
    build_timeout: Optional[float]  # Set a timeout (in seconds) for building tests
    test_timeout: float  # Set a timeout (in seconds) for running tests
    junit_xml: Path  # Write a JUnit XML report to this path
    clean: bool  # Delete temporary build artifacts
    keep_going: bool  # Continue building and running other testcases if one fails
    list: bool  # List selected test cases and exit
    no_color: bool  # Don't use escape codes for coloring output
    skip_lib_tests: bool  # Don't run the tests for libraries
    verbose: bool  # Print all command output")
    only: Optional[str]  # Select a single pass to perform of `config`, `build`, `build-runner`, `test`, `perf-metrics`"
    config_fuzz: bool  # Configure test cases to build with fuzzing support
    asan: bool  # Enable building with asan and ubsan
    perf_metrics: Optional[Path] # Run perf metrics collection and output to specified path
    valgrind_metrics: Optional[Path] # Run valgrind massif metrics collection and output to specified path


def build_argparser() -> ArgumentParser:
    ap = ArgumentParser(
        description="CI testing", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=os.getcwd(), help="Path to root")
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="Parallel build and configure jobs (default = use all cores)",
    )
    ap.add_argument(
        "-m",
        "--match-regex",
        action="append",
        default=[],
        help="Regex to select test cases by their relative path; can be repeated",
    )
    ap.add_argument(
        "-s",
        "--subset",
        type=Path,
        action="append",
        default=[],
        help="Explicit directories to search for tests; can be repeated. "
        "Relative to --root unless absolute.",
    )
    ap.add_argument(
        "--build-timeout",
        type=float,
        default=None,
        help="Set a timeout (in seconds) for building tests",
    )
    ap.add_argument(
        "--test-timeout",
        type=float,
        default=120,
        help="Set a timeout (in seconds) for running tests",
    )
    ap.add_argument(
        "-x",
        "--junit-xml",
        type=Path,
        default=None,
        help="Write a JUnit XML report to this path",
    )
    ap.add_argument(
        "--clean", 
        action="store_true",
        help="Delete temporary build artifacts"
    )
    ap.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue building and running other testcases if one fails",
    )
    ap.add_argument(
        "--list", action="store_true", help="List selected test cases and exit"
    )
    ap.add_argument(
        "--no-color",
        action="store_true",
        help="Don't use escape codes for coloring output",
    )
    ap.add_argument(
        "--skip-lib-tests",
        action="store_true",
        help="Don't run the tests for libraries",
    )
    ap.add_argument("--verbose", action="store_true", help="Print all command output")
    ap.add_argument(
        "--only",
        type=str,
        default=None,
        help="Select a single pass to perform of `config`, `build`, `build-runner`, `test`, `perf-metrics`"
    )
    ap.add_argument(
        "--config-fuzz",
        action="store_true",
        help="Configure test cases to build with fuzzing support"
    )
    ap.add_argument(
        "--asan",
        action="store_true",
        help="Enable building with asan and ubsan",
    )
    ap.add_argument(
        "--perf-metrics",
        type=Path,
        help="Calculate perf metrics. Outputs these metreics to specified file (overwrites if it exists)",
        default=None
    )
    ap.add_argument(
        "--valgrind-metrics",
        type=Path,
        help="Calculate valgrind massif metrics. Outputs these metreics to specified file (overwrites if it exists)",
        default=None
    )
    return ap

def _run_phase_worker(test_case: TestCase, phase_func: Callable, phase_name: str, context: BuildContext) -> PhaseResult:
    print(f"   {phase_name.capitalize()} {test_case.rel_name}", flush=True)
    try:
        phase_func(test_case, context)
        return PhaseResult(test_case, True)
    except Exception as e:
        return PhaseResult(test_case, False, f"{e!r}")

def _run_test_worker(test_case: TestCase, verbose: bool, env: Optional[Mapping[str, str]]=None, timeout: Optional[float]=None) -> PhaseResult:
    print(f"   Executing {test_case.rel_name}", flush=True)
    try:
        runner = LibRunner if test_case.is_library else ExecRunner
        outcomes = runner.run_tests(test_case, verbose, env, timeout)
        return PhaseResult(test_case, True, outcomes=outcomes)
    except Exception as e:
        return PhaseResult(test_case, False, f"{e!r}")

def _run_parallel_phase(
    phase_name: str,
    cases: list[TestCase],
    phase_func: Callable,
    context: BuildContext,
    n_jobs: Optional[int],
    args,
    totals: dict,
    failures: list[tuple[str, str]],
    junit_suites: dict,
    junit_name: Optional[str] = None,
) -> dict[str, TestCase]:
    """
    Run a phase (configure/build) in parallel across test cases.
    
    Returns a dict of test_case.rel_name -> TestCase for cases that succeeded.
    """
    junit_name = junit_name or phase_name
    still_active = {}
    
    with ProcessPoolExecutor(max_workers=n_jobs) as executor:
        future_to_case = {
            executor.submit(_run_phase_worker, tc, phase_func, phase_name, context): tc
            for tc in cases
        }
        
        for future in as_completed(future_to_case):
            tc = future_to_case[future]
            try:
                result = future.result()
            except Exception as e:
                result = PhaseResult(tc, False, f"Worker error: {e!r}")
            
            if result.success:
                junit_suites[tc.rel_name].append(JUnitCase(junit_name, ok=True))
                still_active[tc.rel_name] = tc
            else:
                msg = f"{phase_name.capitalize()} failed: {result.error_msg}"
                print_step(
                    Status.FAIL,
                    tc.rel_name,
                    msg,
                    colorize=(not args.no_color),
                    where=sys.stderr,
                )
                junit_suites[tc.rel_name].append(
                    JUnitCase(name=junit_name, ok=False, error=True, message=msg)
                )
                failures.append((tc.rel_name, msg))
                totals["failed_cases"].add(tc.rel_name)
                
                if not args.keep_going:
                    # Cancel remaining work
                    for f in future_to_case:
                        f.cancel()
                    break
    
    return still_active

@contextmanager
def profile_elapsed(msg: str):
    """Output how long code-surrounded-by-context-manager takes to run

    Args:
        msg (str): description message to log
    """
    start = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - start
        print(f"Profile: {msg} took {elapsed:.2f}s")

def _run_test_phase(
    cases: list[TestCase],
    n_jobs: Optional[int],
    args,
    totals: dict,
    failures: list[tuple[str, str]],
    junit_suites: dict,
    env: Optional[Mapping[str,str]]=None,
    timeout: Optional[float]=None
) -> dict[str, TestCase]:
    still_active = {}
    
    with ProcessPoolExecutor(max_workers=n_jobs) as executor:
        future_to_case = {
            executor.submit(_run_test_worker, tc, args.verbose, env, timeout): tc
            for tc in cases
        }
        
        for future in as_completed(future_to_case):
            tc = future_to_case[future]
            try:
                result = future.result()
            except Exception as e:
                result = PhaseResult(tc, False, f"Worker error: {e!r}")
            
            if not result.success:
                msg = f"Execution failed: {result.error_msg}"
                print_step(
                    Status.FAIL,
                    tc.rel_name,
                    msg,
                    colorize=(not args.no_color),
                    where=sys.stderr
                )
                junit_suites[tc.rel_name].append(
                    JUnitCase(name="execution", ok=False, error=True, message=msg)
                )
                failures.append((tc.rel_name, msg))
                totals["failed_cases"].add(tc.rel_name)
                
                if not args.keep_going:
                    for f in future_to_case:
                        f.cancel()
                    break
                continue
            
            outcomes = result.outcomes or []
            
            if not outcomes:
                totals["skipped_cases"] += 1
                msg = "No test vectors found"
                junit_suites[tc.rel_name].append(
                    JUnitCase(name=tc.rel_name, ok=False, skipped=True, message=msg)
                )
                print_step(
                    Status.SKIP,
                    tc.rel_name,
                    msg,
                    colorize=(not args.no_color),
                    where=sys.stderr,
                )
                continue
            
            totals["tested_cases"] += 1
            case_has_failure = False
            
            for o in outcomes:
                if o.skipped:
                    totals["skipped_vectors"] += 1
                    junit_suites[tc.rel_name].append(
                        JUnitCase(name=o.name, ok=True, skipped=True, message=o.message)
                    )
                elif o.ok:
                    totals["passed_vectors"] += 1
                    junit_suites[tc.rel_name].append(
                        JUnitCase(name=o.name, ok=True)
                    )
                else:
                    totals["failed_vectors"] += 1
                    junit_suites[tc.rel_name].append(
                        JUnitCase(name=o.name, ok=False, message=o.message)
                    )
                    msg = f"Test failed ({o.message})"
                    print_step(
                        Status.FAIL,
                        tc.rel_name,
                        msg,
                        colorize=(not args.no_color),
                        where=sys.stderr,
                    )
                    failures.append((tc.rel_name, msg))
                    case_has_failure = True
            
            if case_has_failure:
                totals["failed_cases"].add(tc.rel_name)
                if not args.keep_going:
                    for f in future_to_case:
                        f.cancel()
                    break
            else:
                still_active[tc.rel_name] = tc
    
    return still_active

def _finalize_results(totals: dict, failures: list[tuple[str, str]], junit_suites) -> RunReport:
    return RunReport(totals=totals, junit_suites=junit_suites, failures=failures)

def run_all_phases(args: ParsedArguments, cases: list[TestCase], hooks: Hooks, context: BuildContext):
    n_jobs = args.jobs if args.jobs and args.jobs > 0 else None
    totals = {
        "total_cases" : len(cases),
        "tested_cases" : 0,
        "skipped_cases" : 0,
        "failed_cases" : set(),
        "passed_vectors" : 0,
        "skipped_vectors" : 0,
        "failed_vectors" : 0,
    }

    failures: list[tuple[str, str]] = []
    junit_suites: dict[str, list[JUnitCase]] = {c.rel_name: [] for c in cases}

    # Track which test cases are still active (haven't failed yet)
    active_cases = {tc.rel_name: tc for tc in cases if (not tc.is_library or (tc.is_library and not context.skip_lib_tests))}

    # Phase 1: Configure
    if hooks.configure and (args.only is None or args.only == "config"):
        print("Configuring test cases...")
        start = time.time()
        active_cases = _run_parallel_phase(
            phase_name="configure",
            cases=list(active_cases.values()),
            phase_func=hooks.configure,
            context=context,
            n_jobs=n_jobs,
            args=args,
            totals=totals,
            failures=failures,
            junit_suites=junit_suites,
        )
        elapsed = time.time() - start
        print(f"Test cases configured in {elapsed:.2f}s")
        if not args.keep_going and not active_cases:
            return _finalize_results(totals, failures, junit_suites)

    # Phase 2: Build exec test cases
    if hooks.build and (args.only is None or args.only == "build"):
        print("Building test cases...")
        start = time.time()
        active_cases = _run_parallel_phase(
            phase_name="build",
            cases=list(active_cases.values()),
            phase_func=hooks.build,
            context=context,
            n_jobs=n_jobs,
            args=args,
            totals=totals,
            failures=failures,
            junit_suites=junit_suites,
        )
        elapsed = time.time() - start
        print(f"Test cases built in {elapsed:.2f}s")
        if not args.keep_going and not active_cases:
            return _finalize_results(totals, failures, junit_suites)
        
    # Phase 3: Build library harness (for library cases only)
    if hooks.build_runner and (args.only is None or args.only == "build-runner"):
        if args.asan:
            lib_cases: dict[str, TestCase] = {}
            bin_cases: dict[str, TestCase] = {}
            for (name, tc) in active_cases.items():
                # We could also pull in itertools for the partition method
                if tc.is_library:
                    lib_cases[name] = tc
                else:
                    bin_cases[name] = tc
            if lib_cases and not args.skip_lib_tests:
                print("Building lib test runners...")
                start = time.time()
                active_cases = _run_parallel_phase(
                    phase_name="build_lib_harness",
                    cases=list(lib_cases.values()),
                    phase_func=hooks.build_runner,
                    context=context,
                    n_jobs=n_jobs,
                    args=args,
                    totals=totals,
                    failures=failures,
                    junit_suites=junit_suites,
                    junit_name="build",  # Group with build in JUnit
                )
                elapsed = time.time() - start
                print(f"Lib test runners built in {elapsed:.2f}s")
                active_cases.update(bin_cases)
                if not args.keep_going and not active_cases:
                    return _finalize_results(totals, failures, junit_suites)
        else:
            lib_runner_cargo_targets = []
            for test_case in list(active_cases.values()):
                if test_case.is_library:
                    target_name = f"_{Path(test_case.rel_name).name}_runner"
                    lib_runner_cargo_targets.append(target_name)
            if len(lib_runner_cargo_targets) > 0:
                build_cmd: list[str | Path] = ["cargo", "build", "--release"]
                for target in lib_runner_cargo_targets:
                    build_cmd.append("-p")
                    build_cmd.append(target)
                print("Building lib test runners...")
                start = time.time()
                run_command(cmd=build_cmd, cwd=args.root, verbose=args.verbose,
                            check=True, timeout=args.build_timeout)
                elapsed = time.time() - start
                print(f"Lib test runners built in {elapsed:.2f}s")

    # Perf runtime performance metrics (don't run anything else as they might interfere)
    if (args.only is None or args.only == "perf-metrics") and args.perf_metrics:
        env = None
        if hooks.generate_run_environment is not None:
            updates = hooks.generate_run_environment(context)
            if updates is not None:
                env = environ.copy()
                env.update(updates)

        print("Running perf runtime performance metrics...")

        # TODO: This doesn't do any final reporting. We should probably add some
        with profile_elapsed("Perf runtime performance metrics"):
            for test_case in active_cases.values():
                perf_metrics.run(test_case, args.perf_metrics, args.verbose, env, args.test_timeout)
        return _finalize_results(totals, failures, junit_suites)
    
    if (args.only is None or args.only == "valgrind-metrics") and args.valgrind_metrics:
        env = None
        if hooks.generate_run_environment is not None:
            updates = hooks.generate_run_environment(context)
            if updates is not None:
                env = environ.copy()
                env.update(updates)

        print("Running valgrind memory metrics...")

        # TODO: This doesn't do any final reporting. We should probably add some
        with profile_elapsed("Valgrind memory metrics"):
            for test_case in active_cases.values():
                valgrind_metrics.run(test_case, args.valgrind_metrics, args.verbose, env, args.test_timeout)
        return _finalize_results(totals, failures, junit_suites)

            
    # Phase 4: Run tests
    if args.only is None or args.only == "test":
        env = None
        if hooks.generate_run_environment is not None:
            updates = hooks.generate_run_environment(context)
            if updates is not None:
                env = environ.copy()
                env.update(updates)
        print("Executing test cases...")
        with profile_elapsed("Test cases execution"):
            active_cases = _run_test_phase(
                cases=list(active_cases.values()),
                n_jobs=n_jobs,
                args=args,
                totals=totals,
                failures=failures,
                junit_suites=junit_suites,
                env=env,
                timeout=args.test_timeout
            )

    
    return _finalize_results(totals, failures, junit_suites)

def finalize_reporting(junit_path, report: RunReport, color=True):
    if junit_path:
        try:
            write_junit_xml(junit_path, report.junit_suites)
            print(f"\nWrote Junit report to {junit_path}")
        except Exception as e:
            print(f"warning: failed to write JUnit XML: {e}", file=sys.stderr)

    print_failures(report.failures)
    print_summary(
        report.totals["total_cases"],
        report.totals["skipped_cases"],
        report.totals["tested_cases"],
        len(report.totals["failed_cases"]),
        report.totals["passed_vectors"],
        report.totals["skipped_vectors"],
        report.totals["failed_vectors"],
        colorize=color,
    )
