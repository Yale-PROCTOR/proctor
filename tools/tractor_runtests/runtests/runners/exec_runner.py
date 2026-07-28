# © 2026 Massachusetts Institute of Technology
# MIT License

from typing import Optional, Mapping

from .base import TestCaseRunner
from ..local_types import TestCase, TestOutcome
from ..utils import (
    regex_match,
    run_command,
    CommandError,
    difference,
    generate_test_corpus,
)


class ExecRunner(TestCaseRunner):
    def run_tests(test_case: TestCase, verbose: bool, env: Optional[Mapping[str, str]], timeout: Optional[float] = None) -> list[TestOutcome]:
        """Execute all test vectors for a single test case and return per-test results.

        How it works
        ------------
        - Runs the built `${build_dir}/driver` as the test subject (CWD set to test_case_dir).
        - For each JSON file in ./test_vectors (defining the current test vector):
            * build argv: [driver, *args]
            * send `stdin` to stdin
            * capture stdout/stderr and compare to `stdout` / `stderr` if present
            * compare process return code to `rc` (default 0)

        Returns
        -------
        list[tuple[bool, bool, str, str]] (Actually, list of TestOutcomes which have this structure)
            For each test vector, (skipped, passed, name, message). The message contains a short summary,
            and in verbose mode includes a unified diff for stdout (and optionally stderr).
        """
        test_case_dir = test_case.test_root
        test_dir = test_case_dir / "test_vectors"
        bin_dir = test_case.runtime_bin_dir

        if not test_dir.is_dir():
            return []

        corpus = generate_test_corpus(test_dir)

        out: list[TestOutcome] = []
        for test_name, spec in corpus.items():
            # No input / output pairs are tested for programs that exhibit undefined behavior
            if "has_ub" in spec:
                out.append(
                    TestOutcome(
                        skipped=True,
                        ok=True,
                        name=test_name,
                        message=f"[test] {test_dir}/{test_name}: Skipped",
                    )
                )
                continue

            cmd = [str(bin_dir / "driver"), *spec.get("argv", [])]
            stdin = spec.get("stdin", None)

            if verbose:
                print(f"[test] Testing {cmd}:{test_name}")

            try:
                res = run_command(
                    cmd=cmd,
                    cwd=test_case_dir,
                    verbose=verbose,
                    stdin=stdin,
                    capture_output=True,
                    merge_stderr=False,
                    env=env,
                    timeout=timeout,
                )
            except CommandError as e:
                # Percolate failures to run the driver at all (spawn, permission, etc.)
                raise e

            # Build expectations and compare
            expected_rc = spec.get("rc", 0)
            test_stdout = res.stdout or ""
            test_stderr = res.stderr or ""

            # stdout/stderr exact or regex
            exp_out = spec.get("stdout", {"pattern": "", "is_regex": False})
            exp_err = spec.get("stderr", {"pattern": "", "is_regex": False})

            rc_ok = res.returncode == expected_rc
            out_ok = (
                regex_match(exp_out["pattern"], test_stdout)
                if exp_out.get("is_regex", False)
                else (test_stdout == exp_out["pattern"])
            )
            err_ok = (
                regex_match(exp_err["pattern"], test_stderr)
                if exp_err.get("is_regex", False)
                else (test_stderr == exp_err["pattern"])
            )

            if rc_ok and out_ok and err_ok:
                out.append(
                    TestOutcome(
                        skipped=False,
                        ok=True,
                        name=test_name,
                        message=f"[test] {test_dir}/{test_name}: Passed",
                    )
                )
            else:
                reasons = []
                if not out_ok:
                    reasons.append("stdout mismatch")
                if not err_ok:
                    reasons.append("stderr mismatch")
                if not rc_ok:
                    reasons.append("return code mismatch")
                msg = f"{test_name}: " + ", ".join(reasons)

                if verbose:
                    msg += "\n" + difference("stdout", exp_out["pattern"], test_stdout)
                    msg += "\n" + difference("stderr", exp_err["pattern"], test_stderr)
                    msg += f"\nexpected rc={expected_rc}, actual rc={res.returncode}\n"
                out.append(TestOutcome(False, False, test_name, msg))

        return out
