# © 2026 Massachusetts Institute of Technology
# MIT License

# ci/__main__.py
import shutil
import sys
from typing import cast

from runtests.discovery.core import collect_cases
from runtests.discovery.c import C_PROFILE
from runtests.cli import BuildContext, ParsedArguments, build_argparser, run_all_phases, finalize_reporting
from .hooks import make_ci_hooks


def main():
    ap = build_argparser()

    # NB: cast is safe as long as ParsedArguments actually matches build_argparser()
    args: ParsedArguments = cast(ParsedArguments, ap.parse_args())

    root = args.root.resolve()
    if not args.subset:
        from runtests.cli import PROJECT_DIRS
        args.subset = [root / top for top in PROJECT_DIRS]

    test_cases = collect_cases(root, args.subset, args.match_regex, C_PROFILE)
    
    # Process --list flag, ignoring other flags if present
    if args.list:
        for c in test_cases:
            print(c.rel_name)
        return 0
    if not test_cases:
        print("No test cases found", file=sys.stderr)
        return 1
    
    # Process --clean flag, ignoring other flags if present
    if args.clean:
        for c in test_cases:
            try:
                shutil.rmtree(c.target_dir)
                print(f"Deleted {c.target_dir}")
            except FileNotFoundError:
                pass
        return 0

    found_case_count = len(test_cases)
    print(f"Found {found_case_count} test case{'s' if found_case_count != 1 else ''}")

    cmake = shutil.which("cmake")
    ninja = shutil.which("ninja")
    need_cargo = any(tc.is_library for tc in test_cases) and not args.skip_lib_tests
    cargo = shutil.which("cargo") if need_cargo else None
    if not cmake or not ninja or (need_cargo and not cargo):
        tool = 'cmake' if not cmake else ('ninja' if not ninja else 'cargo')
        print(f"error: required tool '{tool}' not found in PATH", file=sys.stderr)
        return 2

    context = BuildContext(
        cmake=cmake,
        cargo=cargo,
        jobs=args.jobs,
        timeout=args.build_timeout,
        verbose=args.verbose,
        skip_lib_tests=args.skip_lib_tests,
        fuzz=args.config_fuzz,
        asan=args.asan,
    )

    hooks = make_ci_hooks()
    report = run_all_phases(args, test_cases, hooks, context)
    finalize_reporting(args.junit_xml, report, color=(not args.no_color))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
