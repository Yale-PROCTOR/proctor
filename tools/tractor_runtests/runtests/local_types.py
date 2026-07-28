# © 2026 Massachusetts Institute of Technology
# MIT License

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Duplicate definition to maintain parallel commits
def _fmt_cmd(cmd: list[str]) -> str:
    """Return a shell-escaped preview string for an argv-style command list.""" 
    return " ".join([shlex.quote(str(arg)) for arg in cmd])

@dataclass
class CommandResult:
    """Lightweight record of a completed subprocess.

    Attributes
    ----------
    cmd : list[str]
        Command (argv list) that was executed.
    cwd : str|None
        Working directory used for the subprocess.
    returncode : int
        Process exit status.
    stdout : str|None
        Captured stdout if capture_output=True, else None.
    stderr : str|None
        Captured stderr if capture_output=True and merge_stderr=False, else None.
    """

    cmd: list[str]
    cwd: Optional[str]
    returncode: int
    stdout: Optional[str]
    stderr: Optional[str]


class CommandError(RuntimeError):
    """Simple wrapper error for command failures"""

    def __init__(self, cmd, message: Optional[str] = None):
        msg = message or f"Command failed: {_fmt_cmd(cmd)}"
        super().__init__(msg)


@dataclass
class JUnitCase:
    name: str
    ok: bool
    message: str = ""
    skipped: bool = False
    error: bool = False


@dataclass
class TestOutcome:
    skipped: bool
    ok: bool
    name: str
    message: str


@dataclass
class TestCase:
    test_root: Path # Case root (contains test_vectors/, runner/, etc.)
    repo_root: Path
    rel_name: str
    is_library: bool

    # Build context for the test case
    build_dir: Path # Where to run cmake/cargo for the test case
    target_dir: Path # cmake binary dir OR cargo --target-dir (parent of release)

    # Runtime context for exec runner
    runtime_bin_dir: Path # Directory that contains 'driver'

    # Lib harness context
    runner_dir: Path #case_root / runner (Cando)
