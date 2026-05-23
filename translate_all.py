#!/usr/bin/env python3
import concurrent.futures
import glob
import os
import signal
import subprocess
import sys
import threading

MAX_WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else os.cpu_count() or 1
directories = sorted(glob.glob("Hidden-Tests/*/*/") + glob.glob("Public-Tests/*/*/"))
successes = []
failures = []
process_lock = threading.Lock()
active_processes = set()

excludes = []

def finish():
    global successes, failures, directories

    successes = sorted(successes)
    if successes:
        print(f"Successes:")
        for x in successes:
            print(f"  {x}")

    failures = sorted(failures)
    if failures:
        print(f"Failures:")
        for x in failures:
            print(f"  {x}")

    remainings = sorted(set(directories) - set(successes) - set(failures))
    if remainings:
        print(f"Remainings:")
        for x in remainings:
            print(f"  {x}")

    if successes:
        print(f"Successes: {len(successes)}")
    if failures:
        print(f"Failures: {len(failures)}")
    if remainings:
        print(f"Remainings: {len(remainings)}")

    os._exit(1 if failures or remainings else 0)


def handle_interrupt(sig, frame):
    with process_lock:
        for p in active_processes:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                p.terminate()
    finish()


def run_translate(directory):
    if any(exclude in directory for exclude in excludes):
        return directory, True

    print(directory)
    command = ["./translate.sh", directory]

    try:
        with process_lock:
            p = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            active_processes.add(p)

        p.wait()

        with process_lock:
            active_processes.remove(p)

        return directory, (p.returncode == 0)
    except Exception:
        return directory, False

signal.signal(signal.SIGINT, handle_interrupt)

with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    future_to_dir = {executor.submit(run_translate, d): d for d in directories}

    try:
        for future in concurrent.futures.as_completed(future_to_dir):
            directory, success = future.result()
            if success:
                successes.append(directory)
            else:
                failures.append(directory)
    except KeyboardInterrupt:
        handle_interrupt(None, None)

finish()
