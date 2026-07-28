# © 2026 Massachusetts Institute of Technology
# MIT License

# rust/__main__.py
import shutil
import sys
from typing import cast

from runtests.discovery.core import collect_cases
from runtests.discovery.rust import RUST_PROFILE
from runtests.cli import ParsedArguments, build_argparser, run_all_phases, finalize_reporting, BuildContext
from .hooks import make_rust_hooks


def main():
    # NB: cast is safe as long as ParsedArguments actually matches build_argparser()
    args: ParsedArguments = cast(ParsedArguments, build_argparser().parse_args())

    root = args.root.resolve()
    if not args.subset:
        from runtests.cli import PROJECT_DIRS
        args.subset = [root / top for top in PROJECT_DIRS]

    test_cases = collect_cases(root, args.subset, args.match_regex, RUST_PROFILE)

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
        for test_case in test_cases:
            try:
                shutil.rmtree(test_case.target_dir)
                print(f"Deleted {test_case.target_dir}")
            except FileNotFoundError:
                pass
        return 0

    found_case_count = len(test_cases)
    print(f"Found {found_case_count} test case{'s' if found_case_count != 1 else ''}")
    
    cargo = shutil.which("cargo")
    if not cargo:
        print(f"error: required tool 'cargo' not found in PATH", file=sys.stderr)
        return 2

    context = BuildContext(
        cmake=None,
        cargo=cargo,
        jobs=args.jobs,
        timeout=args.build_timeout,
        verbose=args.verbose,
        skip_lib_tests=args.skip_lib_tests,
        fuzz=args.config_fuzz,
        asan=False,
    )

    hooks = make_rust_hooks()
    report = run_all_phases(args, test_cases, hooks, context)
    finalize_reporting(args.junit_xml, report, color=(not args.no_color))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
