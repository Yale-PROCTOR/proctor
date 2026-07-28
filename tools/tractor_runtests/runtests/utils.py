# © 2026 Massachusetts Institute of Technology
# MIT License

import re, json, subprocess, os, glob, shlex
from difflib import unified_diff
from pathlib import Path
from typing import Optional, Union, Mapping
from .local_types import CommandResult, CommandError

SNIPPET_LEN = 800
PERFORMERS = ["aarno", "galois", "harvest", "intel", "uwisc", "yale", "c2rust", "llm"]
LONG_TIMEOUT = 1800 # 30 minutes for 008_long_run


def _fmt_cmd(cmd: list[str]) -> str:
    """Return a shell-escaped preview string for an argv-style command list."""
    return " ".join([shlex.quote(str(arg)) for arg in cmd])

def find_asan_library():
    """Find libclang_rt.asan.so using multiple methods."""
    
    # Ask clang directly
    try:
        result = subprocess.run(
            ['clang', '-print-runtime-dir'],
            capture_output=True,
            text=True,
            check=True
        )
        runtime_dir = result.stdout.strip()
        asan_path = os.path.join(runtime_dir, 'libclang_rt.asan.so')
        if os.path.exists(asan_path):
            return asan_path
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    # Search ldconfig cache
    try:
        result = subprocess.run(
            ['ldconfig', '-p'],
            capture_output=True,
            text=True,
            check=True
        )
        for line in result.stdout.splitlines():
            if 'libclang_rt.asan.so' in line:
                path = line.split('=>')[-1].strip()
                if os.path.exists(path):
                    return path
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    # Search common installation paths
    search_patterns = [
        '/usr/lib/clang/*/lib/linux/libclang_rt.asan-*.so',
        '/usr/lib/llvm-*/lib/clang/*/lib/linux/libclang_rt.asan-*.so',
        '/usr/lib64/clang/*/lib/linux/libclang_rt.asan-*.so',
        '/usr/local/lib/clang/*/lib/linux/libclang_rt.asan-*.so',
    ]
    
    for pattern in search_patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    
    return None

def difference(label: str, expected: str, actual: str, n=1500) -> str:
    diff = "".join(
        unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"expected {label}",
            tofile=f"actual {label}",
        )
    )
    return diff[:n] + ("... [truncated]\n" if len(diff) > n else "")


def regex_match(pattern: str, text: str) -> bool:
    """Return True if the pattern fully matches the input text"""
    compiled = re.compile(pattern, flags=re.MULTILINE)
    return bool(compiled.fullmatch(text))


def generate_test_corpus(test_dir_path: Path):
    """Load all JSON test files in deterministic (sorted) order.

    File format (per test JSON)
    ---------------------------
    {
      "argv":  [ "...", ... ]  | None, # optional command-line args to pass to driver
      "stdin":  "..."          | None, # optional stdin input string
      "stdout": { "pattern",   | None, # optional expected stdout. pattern is the output string
                "is_regex" }   | None, # if is_regex is true, compile pattern as regex
      "stderr": { "pattern",   | None, # optional expected stderr. pattern is the error string
                "is_regex" }   | None, # if is_regex is true, compile pattern as regex
      "lib_state_in": {...}    | None, # The state of a program running a library function
      "lib_state_out": {...}   | None, # The state after library function execution
      "rc":     N              | None, # optional expected exit status (default 0)
      "has_ub": "..."          | None, # optional flag to indicate UB. Provides a text explanation
    }

    Returns
    -------
    dict[str, dict]
        test_name (filename stem) -> test specification dict.
    """
    test_vectors = {}
    entries = [
        e for e in os.scandir(test_dir_path) if e.is_file() and e.name.endswith(".json")
    ]
    entries.sort(key=lambda e: Path(e.name).stem)

    for dir_entry in entries:
        name = Path(dir_entry.name).stem
        with open(dir_entry.path, mode="r", encoding="utf-8") as test_file:
            test_data = json.load(test_file)
            test_vectors[name] = test_data
    return test_vectors


def run_command(
    cmd: list[Union[str, Path]],
    cwd: Optional[Union[str, Path]] = None,
    verbose: bool = False,
    stdin: Optional[Union[str, bytes]] = None,
    capture_output: bool = False,
    merge_stderr: bool = False,
    check: bool = False,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> CommandResult:
    """Run a subprocess with consistent logging, capture, and error handling.

    Parameters
    ----------
    cmd : list[str|Path]
        Program and arguments (argv style). Paths are stringified safely.
    cwd : str|Path|None
        Working directory for the child process.
    verbose : bool
        If True, prints a one-line `[run]` log (and capture previews).
    stdin : str|bytes|None
        Input to send to the process. Bytes are decoded as UTF-8 with 'replace'.
    capture_output : bool
        If True, capture stdout/stderr as text; else inherit parent's stdio.
    merge_stderr : bool
        If True and capturing, merge stderr into stdout; stderr will be None in the result.
    check : bool
        If True, raise CommandError on non-zero exit.
    timeout : float|None
        Kill the process if it runs longer than this many seconds.
    env : Mapping[str,str]|None
        Additional/overridden environment variables for the child.

    Returns
    -------
    CommandResult
        Completed process info including return code and any captured output.

    Raises
    ------
    CommandError
        On timeout, spawn error, or (if check=True) non-zero exit code.
    """
    cmd_list = [str(arg) for arg in cmd]
    cwd_str = str(cwd) if isinstance(cwd, Path) else cwd

    if capture_output:
        stdout_opt = subprocess.PIPE
        stderr_opt = subprocess.STDOUT if merge_stderr else subprocess.PIPE
    else:
        stdout_opt = None
        stderr_opt = None if not merge_stderr else subprocess.STDOUT

    if isinstance(stdin, bytes):
        try:
            stdin = stdin.decode("utf-8", "replace")
        except Exception:
            stdin = stdin.decode(errors="replace")

    if verbose:
        where = f" (cwd = {cwd_str})" if cwd_str else ""
        print(f"\n[run] {_fmt_cmd(cmd_list)}{where}")

    completed = None
    try:
        completed = subprocess.run(
            cmd_list,
            cwd=cwd_str,
            input=stdin,
            text=True,  # ensure stdout/stderr are str, not bytes
            stdout=stdout_opt,
            stderr=stderr_opt,
            env=env,
            timeout=timeout,
            check=False,  # we raise manually below if check=True
        )
    except subprocess.TimeoutExpired as te:
        raise CommandError(
            cmd_list, message=f"Timed out after {timeout}s: {_fmt_cmd(cmd_list)}"
        ) from te
    except Exception as e:
        # TODO: Format the error nicely
        raise CommandError(
            cmd_list, message=f"Command Failed {_fmt_cmd(cmd_list)} {repr(e)}"
        ) from e

    out = completed.stdout if capture_output else None
    err = None if merge_stderr else (completed.stderr if capture_output else None)

    # On verbose runs, show short previews of captured streams for easier debugging.
    if verbose and capture_output:

        def _preview(label: str, data: Optional[str]):
            if data is None:
                print(f"[run] {label}: <None>")
                return
            snippet = data[:SNIPPET_LEN] + (
                "... [truncated]\n" if len(data) > SNIPPET_LEN else ""
            )
            print(f"[run] {label} preview:\n{snippet.encode('utf-8')}")

        _preview("stdin", stdin)
        _preview("stdout", out)
        if not merge_stderr:
            _preview("stderr", err)

    if check and completed.returncode != 0:
        raise CommandError(
            cmd_list,
            # Note, this expects the following. message: str | None = None
            # Maintaining for commit parallelism
            message=[f"Command exited {completed.returncode}: {_fmt_cmd(cmd_list)}"], 
        )

    return CommandResult(
        cmd=cmd_list,
        cwd=cwd_str,
        returncode=completed.returncode,
        stdout=out,
        stderr=err,
    )
