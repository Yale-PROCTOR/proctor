# © 2026 Massachusetts Institute of Technology
# MIT License

import csv
import json
from pathlib import Path
from typing import List, Mapping, Optional
from dataclasses import asdict, dataclass, fields
from tempfile import NamedTemporaryFile

from ..utils import run_command, CommandError, generate_test_corpus, LONG_TIMEOUT
from ..local_types import TestCase, TestOutcome

NUM_RUNS = 5 # number of times to run perf stat per test vector

# Events for perf stat to run
EVENTS = [
    "instructions",
    "cpu-cycles",
    "branch-instructions",
    "branch-misses",
    "cache-misses",
    "cache-references",
    "context-switches",
    "cpu-migrations",
    "page-faults",
    "duration_time",
    "user_time",
    "task-clock"
]


@dataclass
class PerfStatMetrics:
    performer: str
    test_case: str
    test_vector: str
    value: float
    unit: str
    event: str
    variance_pct: float
    metric_value: float
    metric_unit: str


def dump_metrics(metrics: List[PerfStatMetrics], file: Path) -> None:
    """
    Dumps a run of perf stat into CSV file

    Args:
        lines: output from `run_perf_stat`
        file: path to file to output results. Appends to `file` if it exists
    """
    # Only need to write the header if the file doesn't exist
    write_header = True if not file.exists() else False

    with open(file, "a", newline='') as csv_file:
        fieldnames = [field.name for field in fields(PerfStatMetrics)]
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows([asdict(metric) for metric in metrics])


def run_perf_stat(
    run_cmd: List[str | Path], 
    cwd: Path, 
    verbose: bool, 
    timeout: Optional[float], 
    env: Optional[Mapping[str, str]],
    stdin: Optional[str] = None, 
    num_runs: int = NUM_RUNS) -> List[str]:
    """
    Executes `perf stat`, with a default list of events

    Args:
        run_cmd: the command to benchmark with perf stat
        cwd: the directory to run the command
        stdin: input to pass to program 
        num_runs: the number of times to repeat the measurements

    Returns:
        List of strings that are the lines of the output from perf stat

    Exceptions:
        If the command fails, or stderr is empty 
    """
    events = ",".join(EVENTS) # convert to CSV as expected by perf stat
    perf_cmd: List[str | Path] = [
        "perf",
        "stat",
        "-e", events,
        "--repeat", str(num_runs),
        "-j", # make it output JSON
        "sh",
        "-c"
    ]

    # Quote everything in the command to make sure arguments are specified properly
    cmd = " ".join(f'"{str(item)}"' for item in run_cmd)

    if stdin is not None:
        # We need to manually handle stdin because perf stat doesn't like it
        # Use file redirection because it doesn't like echo either it seems
        with NamedTemporaryFile("w+") as stdin_file:
            stdin_file.write(stdin)
            cmd += f" < {stdin_file.name}"
    perf_cmd.append(cmd)

    if verbose:
        print(f"Running perf cmd: {perf_cmd}")


    # Check if we're doing the long run based on the command
    timeout = LONG_TIMEOUT if "008_long_run" in str(run_cmd[0]) else timeout

    try:
        res = run_command(
            perf_cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout
        )
    except CommandError as e:
        raise e

    if verbose:
        print(f"Stdout: {res.stdout}")
        print(f"Stderr: {res.stderr}")

    if res.stderr is None:
        raise RuntimeError("Couldn't get benchmarking information from perf stat")

    # Rc=1 means a partial test vector comparison so we'll leave it at that
    if res.returncode not in [0, 1]:
        raise RuntimeError(f"Unknown error/panic. Got returncode: {res.returncode}")

    return res.stderr.splitlines()


def parse_perf_stat(lines: List[str], performer: Path, test_case: str, test_vector: str) -> list[PerfStatMetrics]:
    """
    Parses the output from perf stat into format we want

    Args: 
        lines: lines in output from perf stat. Should be in JSON format
        performer: the name of the performer that ran this (just using the directory name for now)
        test_case: the name of the test case that this run was for
        test_vector: the test vector that generated this information

    Exceptions:
        Raises an exception of the input format is unexpected
    """
    out: list[PerfStatMetrics] = []

    for line in lines:
        try:
            json_line = json.loads(line)
        except json.JSONDecodeError as e:
            # Need to continue on if there's other output in stderr
            print(f"Got line in stderr: {line}. Error: {e}. Continuing on")
            continue

        value = float(json_line["counter-value"])
        unit = json_line["unit"]
        event = json_line["event"]
        variance_pct = json_line["variance"]
        metric_value = float(json_line["metric-value"])
        metric_unit = json_line["metric-unit"]

        # For these we need to manually add the unit nanoseconds for some reason
        if event == "duration_time" or event == "user_time":
            unit = "nsec"

        out.append(PerfStatMetrics(
            str(performer),
            test_case, 
            test_vector, 
            value, 
            unit, 
            event, 
            variance_pct, 
            metric_value, 
            metric_unit
        ))

    return out


def run(
    test_case: TestCase, 
    out_file: Path, 
    verbose: bool, 
    env: Optional[Mapping[str, str]], 
    timeout: Optional[float]) -> list[TestOutcome]:
    """
    Runs `perf stat` for all test vectors associated with `test_case`
    """
    test_case_dir = test_case.test_root
    test_dir = test_case_dir / "test_vectors"

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

        if test_case.is_library:
            runner_name = f"_{Path(test_case.rel_name).name}_runner"
            exec_path = test_case.repo_root / "target" / "release" / runner_name
            cmd = [
                exec_path,
                "lib",
                "-c",
                test_name + ".json",
            ]
            cwd = test_case.repo_root
        else:
            cmd = [str(test_case.runtime_bin_dir / "driver"), *spec.get("argv", [])]
            cwd = test_case_dir

        stdin = spec.get("stdin", None)

        # TODO: Better error handling is due here and other places
        try:
            # Actually run, parse, and dump perf stat output
            raw_output = run_perf_stat(cmd, cwd, verbose, timeout, env, stdin)
            # Using test_dir for performer name
            metrics = parse_perf_stat(raw_output, test_dir, test_case.rel_name, test_name)
            dump_metrics(metrics, out_file)
        except Exception as e:
            message = f"[FAIL] {test_dir}/{test_name}: Couldn't execute, parse, or write perf stat run: {e}"
            if verbose:
                print(message)
            out.append(TestOutcome(
                skipped=False,
                ok=False,
                name=test_name,
                message=message
            ))

        out.append(TestOutcome(
            skipped=False,
            ok=True,
            name=test_name,
            message=f"[test] {test_dir}/{test_name}: Got perf metrics"
        ))

    return out

