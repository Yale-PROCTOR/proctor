#!/usr/bin/env python3
"""Run cloc on each test_case dir and summarize C code lines."""

import concurrent.futures
import glob
import json
import os
import signal
import subprocess
import sys
import threading

MAX_WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else os.cpu_count() or 1

process_lock = threading.Lock()
active_processes = set()
results = []
results_lock = threading.Lock()
failed_dirs = 0
failed_lock = threading.Lock()
completed = 0
completed_lock = threading.Lock()


def run_cloc(d, total):
    global failed_dirs, completed

    try:
        with process_lock:
            p = subprocess.Popen(
                ["cloc", "--json", "--include-lang=C,C/C++ Header", d],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                start_new_session=True,
            )
            active_processes.add(p)

        stdout, _ = p.communicate(timeout=120)

        with process_lock:
            active_processes.discard(p)
    except subprocess.TimeoutExpired:
        p.kill()
        with process_lock:
            active_processes.discard(p)
        print(f"  TIMEOUT: {d}", file=sys.stderr)
        with failed_lock:
            failed_dirs += 1
        return
    except Exception as e:
        print(f"  ERROR: {d}: {e}", file=sys.stderr)
        with failed_lock:
            failed_dirs += 1
        return

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        with failed_lock:
            failed_dirs += 1
        return

    n_files = 0
    n_lines = 0
    for lang in ("C", "C/C++ Header"):
        if lang in data:
            n_files += data[lang]["nFiles"]
            n_lines += data[lang]["code"]

    with results_lock:
        results.append((d, n_files, n_lines))

    with completed_lock:
        completed += 1
        print(f"[{completed}/{total}] {d}", file=sys.stderr)


def handle_interrupt(sig, frame):
    with process_lock:
        for p in active_processes:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                p.terminate()
    os._exit(1)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path> [workers]", file=sys.stderr)
        sys.exit(1)

    pattern = f"{sys.argv[1]}/*/*/*/test_case"
    dirs = sorted(glob.glob(pattern))
    if not dirs:
        print(f"No directories matched: {pattern}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(dirs)} directories to check (workers={MAX_WORKERS}).", file=sys.stderr)

    signal.signal(signal.SIGINT, handle_interrupt)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(run_cloc, d, len(dirs)): d for d in dirs}
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        except KeyboardInterrupt:
            handle_interrupt(None, None)

    print(f"\nProcessed {len(dirs)} dirs ({failed_dirs} failed)\n", file=sys.stderr)

    results.sort()
    print("\t".join(["dir", "files", "lines"]))
    for d, n_files, n_lines in results:
        print("\t".join([d, str(n_files), str(n_lines)]))


if __name__ == "__main__":
    main()
