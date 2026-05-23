#!/usr/bin/env python3
"""Run cargo clippy on each translated_rust dir and summarize warnings by code."""

import concurrent.futures
import glob
import json
import os
import signal
import subprocess
import sys
import threading
from collections import Counter

BUCKETS = ["P01", "P00", "B01", "B02"]
MAX_WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else os.cpu_count() or 1

process_lock = threading.Lock()
active_processes = set()
bucket_warnings = {b: Counter() for b in BUCKETS}
warnings_lock = threading.Lock()
failed_dirs = 0
failed_lock = threading.Lock()
completed = 0
completed_lock = threading.Lock()


def classify_bucket(d):
    for part in d.split(os.sep):
        for b in BUCKETS:
            if part.startswith(b):
                return b
    return None


def run_clippy(d, total):
    global failed_dirs, completed
    manifest = f"{d}/Cargo.toml"

    try:
        with process_lock:
            p = subprocess.Popen(
                ["cargo", "clippy", "--message-format=json", f"--manifest-path={manifest}"],
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

    local_warnings = Counter()
    for line in stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-message":
            continue
        message = msg.get("message", {})
        if message.get("level") != "warning":
            continue
        code = (message.get("code") or {}).get("code") or "unknown"
        local_warnings[code] += 1

    bucket = classify_bucket(d)
    if bucket:
        with warnings_lock:
            bucket_warnings[bucket].update(local_warnings)

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
    global failed_dirs

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path> [workers]", file=sys.stderr)
        sys.exit(1)

    pattern = f"{sys.argv[1]}/*/*/*/translated_rust"
    dirs = sorted(glob.glob(pattern))
    if not dirs:
        print(f"No directories matched: {pattern}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(dirs)} directories to check (workers={MAX_WORKERS}).", file=sys.stderr)

    signal.signal(signal.SIGINT, handle_interrupt)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(run_clippy, d, len(dirs)): d for d in dirs}
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        except KeyboardInterrupt:
            handle_interrupt(None, None)

    print(f"\nProcessed {len(dirs)} dirs ({failed_dirs} failed)\n", file=sys.stderr)

    all_codes = set()
    for c in bucket_warnings.values():
        all_codes.update(c.keys())

    total = Counter()
    for c in bucket_warnings.values():
        total.update(c)

    print("\t".join(["warning_code"] + BUCKETS))
    for code, _ in total.most_common():
        row = [code] + [str(bucket_warnings[b].get(code, 0)) for b in BUCKETS]
        print("\t".join(row))


if __name__ == "__main__":
    main()
