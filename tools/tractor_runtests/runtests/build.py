# © 2026 Massachusetts Institute of Technology
# MIT License

from pathlib import Path
from typing import Optional
import glob, json, os, shutil
from .utils import run_command, find_asan_library

def preset_build_dir(test_case_dir: Path, preset_name: str = "test") -> Optional[Path]:
    """Try to read CMakePresets.json and return the binaryDir for the given configure preset."""
    presets = test_case_dir / "CMakePresets.json"
    if not presets.exists():
        return None
    data = json.loads(presets.read_text(encoding="utf-8"))
    for cfg in data.get("configurePresets", []):
        if cfg.get("name") == preset_name:
            bd = (cfg.get("binaryDir") or "").replace(
                "${sourceDir}", str(test_case_dir)
            )
            return Path(bd).resolve() if bd else None
    return None

def configure_c_test_case(
    cmake: str, test_case_dir: Path, timeout: Optional[float], verbose: bool, fuzz: bool, asan: bool
):
    use_preset = (test_case_dir / "CMakePresets.json").exists()

    cmd = [cmake]
    if use_preset:
        cmd += ["-S", f"{test_case_dir}", "--preset", "test"]
    else:
        os.makedirs(test_case_dir / "build-ninja", exist_ok=True)
        cmd += ["-S", "./test_case", "-B", "./build-ninja", "-G", "Ninja"]
    
    cflags = []
    exe_ldflags = []
    sh_ldflags  = []

    if asan:
        base_dbg = ["-g", "-fPIC", "-fno-omit-frame-pointer"]
        san = ["-fsanitize=address", "-fsanitize=undefined", "-fno-sanitize-recover=undefined"]
        cflags      += base_dbg
        cflags      += san
        exe_ldflags += san
        sh_ldflags  += san
    
    if fuzz:
        cflags += ["-fsanitize=fuzzer-no-link"]
        exe_ldflags += ["-fsanitize=fuzzer"]
        sh_ldflags  += ["-fsanitize=fuzzer"]

    if cflags:
        cmd += [f"-DCMAKE_C_FLAGS={' '.join(cflags)}"]
    if exe_ldflags:
        cmd += [f"-DCMAKE_EXE_LINKER_FLAGS={' '.join(exe_ldflags)}"]
    if sh_ldflags:
        cmd += [f"-DCMAKE_SHARED_LINKER_FLAGS={' '.join(sh_ldflags)}",
                f"-DCMAKE_MODULE_LINKER_FLAGS={' '.join(sh_ldflags)}"]
    
    # require clang to build
    cmd += ["-DCMAKE_C_COMPILER=clang"]

    return run_command(
        cmd,
        cwd=test_case_dir,
        verbose=verbose,
        capture_output=True,
        check=True,
        timeout=timeout,
    )


def build_c_test_case(
    cmake: str,
    test_case_dir: Path,
    target_dir: Path,
    jobs: Optional[int],
    timeout: Optional[float],
    verbose: bool,
):
    """
    Build the test case. If configured with a preset, build using that preset from the test case root
    otherwise, build from the explicit binary dir.
    """
    use_preset = (test_case_dir / "CMakePresets.json").exists()
    
    cmd = [cmake, "--build"] + (["--preset", "test"] if use_preset else ["./"])
    cwd = test_case_dir if use_preset else target_dir
    if jobs is not None:
        cmd += ["--parallel"]
        if jobs != 0:
            cmd += [str(jobs)]
    return run_command(
        cmd, cwd=cwd, verbose=verbose, capture_output=True, check=True, timeout=timeout
    )


def build_lib_harness(
    cargo: str,
    runner_dir: Path,
    runner_name: str,
    timeout: Optional[float],
    verbose: bool,
    asan: bool
):
    cmd = [cargo, "rustc", "-p", runner_name, "--release"]
    if asan:
        asan_lib = find_asan_library()
        if asan_lib is None:
            raise FileNotFoundError
        cmd += ["--", "-Zsanitizer=address", f"-Clink-args={asan_lib}"] 
    cwd = runner_dir
    return run_command(
        cmd=cmd,
        cwd=cwd,
        verbose=verbose,
        capture_output=True,
        check=True,
        timeout=timeout,
    )


def build_rust_test_case(
    cargo: str,
    build_dir: Path,
    target_dir: Path,
    timeout: Optional[float],
    verbose: bool,
):
    """
    Build the translated rust test case
    The corresponding Cargo.toml should build a cdylib
    """
    cmd = [cargo]

    # HACK: Not sure exactly why but on my system I need to do +nightly for aarno
    if "aarno" in str(build_dir):
        cmd.append("+nightly")

    cmd += ["build", "--release", "--target-dir", target_dir]

    cwd = build_dir
    result = run_command(
        cmd, cwd=cwd, verbose=verbose, capture_output=False, check=True, timeout=timeout
    )
    # TODO:
    # Some translation tools are placing libraries in the dependencies folder `deps`.
    # To allow our tools to find these translated libraries, we're moving them into release. 
    if result.returncode == 0:
        release_dir = target_dir / "release"
        files = glob.glob(os.path.join(release_dir / "deps", "*.so"), recursive=False)
        for f in files:
            if not (release_dir / Path(f).name).exists():
                shutil.copy(f, release_dir)
    return result 
