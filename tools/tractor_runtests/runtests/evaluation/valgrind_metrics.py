# © 2026 Massachusetts Institute of Technology
# MIT License

import csv
import re
from typing import Optional, Mapping
from pathlib import Path
from dataclasses import asdict, dataclass, fields
from tempfile import NamedTemporaryFile

from ..utils import run_command, CommandError, generate_test_corpus, LONG_TIMEOUT
from ..local_types import TestCase, TestOutcome

@dataclass
class ValgrindMetrics:
    max_mem_heap_B: int = 0
    max_mem_heap_extra_B: int = 0
    max_mem_stacks_B: int = 0


def run_valgrind(
    run_cmd: list[str | Path],
    cwd: Path, 
    verbose: bool, 
    massif_out_file: Path,
    timeout: Optional[float], 
    env: Optional[Mapping[str, str]],
    stdin: Optional[str] = None) -> None:
    """
    Runs valgrind memory profiler massif on an arbitrary command
    Doesn't return anything, but writes output of run to `MASSIF_FILE`
    for parsing later
    """
    valgrind_cmd: list[str | Path] = [
        "valgrind",
        "--tool=massif",
        "--stacks=yes",
        f"--massif-out-file={massif_out_file}",
    ]
    valgrind_cmd.extend(run_cmd)

    if verbose:
        print(f"Running valgrind command: {valgrind_cmd}")

    # Check if we're doing the long run based on the command
    timeout = LONG_TIMEOUT if "008_long_run" in str(run_cmd[0]) else timeout

    try:
        res = run_command(
            cmd=valgrind_cmd,
            stdin=stdin,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
    except CommandError as e:
        raise e

    if verbose:
        print(f"Stdout: {res.stdout}")
        print(f"Stderr: {res.stderr}")

    # Rc=1 means a partial test vector comparison so we'll leave it at that
    if res.returncode not in [0, 1]:
        raise RuntimeError(f"Unknown error/panic. Got returncode: {res.returncode}")

    # Just make sure massif file was created
    if not massif_out_file.exists():
        raise FileNotFoundError(f"Didn't generate massif output file: {massif_out_file}")


def parse_valgrind(massif_out_file: Path) -> ValgrindMetrics:
    """
    Parses output from run of valgrind massif
    Looks for output in `MASSIF_FILE`
    """
    with open(massif_out_file, "r") as f:
        lines = f.readlines()

    stats = ValgrindMetrics()

    for line in lines:
        heap_match = re.match(r"^mem_heap_B=(\d+)", line)
        heap_extra_match = re.match(r"^mem_heap_extra_B=(\d+)", line)
        stack_match = re.match(r"^mem_stacks_B=(\d+)", line)

        if heap_match:
            heap_usage = int(heap_match.group(1))
            stats.max_mem_heap_B = max(stats.max_mem_heap_B, heap_usage)
        elif heap_extra_match:
            heap_extra_usage = int(heap_extra_match.group(1))
            stats.max_mem_heap_extra_B = max(stats.max_mem_heap_extra_B, heap_extra_usage)
        elif stack_match:
            stack_usage = int(stack_match.group(1))
            stats.max_mem_stacks_B = max(stats.max_mem_stacks_B, stack_usage)

    return stats


def dump_metrics(
    metrics: ValgrindMetrics, 
    out_file: Path, 
    performer: Path, 
    test_case: str, 
    test_name: str) -> None:
    """
    Output valgrind metrics in CSV format to `out_file`
    Just using the current directory at the performer name for now
    """
    write_header = True if not out_file.exists() else False

    with open(out_file, "a", newline='') as csv_file:
        metrics_fields = [field.name for field in fields(ValgrindMetrics)]
        fieldnames = ["performer", "test_case", "test_name"] + metrics_fields

        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        combined = {
            "performer": performer,
            "test_case": test_case,
            "test_name": test_name,
            **asdict(metrics)
        }
        writer.writerow(combined)


def run(
    test_case: TestCase, 
    out_file: Path, 
    verbose: bool, 
    env: Optional[Mapping[str, str]], 
    timeout: Optional[float]) -> list[TestOutcome]:
    """
    Run `valgrind --tool=massif` -- a memory profiler to get three things:
        1. Max heap usage
        2. Max stack usage
        3. Max heap extra usage (metadata for allocations)
    """
    test_case_dir = test_case.test_root
    test_dir = test_case_dir / "test_vectors"

    if not test_dir.is_dir():
        return []

    corpus = generate_test_corpus(test_dir)

    out: list[TestOutcome] = []
    for test_name, spec in corpus.items():
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

        try:
            with NamedTemporaryFile() as massif_out_file:
                massif_out_file = Path(massif_out_file.name)
                run_valgrind(cmd, cwd, verbose, massif_out_file, timeout, env, stdin)
                metrics = parse_valgrind(massif_out_file)

            dump_metrics(metrics, out_file, test_dir, test_case.rel_name, test_name)
        except Exception as e:
            message = f"[FAIL] {test_dir}/{test_name}: Couldn't execute or parse valgrind: {e}"
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
            message=f"[test] {test_dir}/{test_name}: Got valgrind metrics"
        ))

    return out
