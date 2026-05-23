#!/usr/bin/env python3
import os, sys, subprocess, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


def run_cratfinder(dir_path: Path, out_dir: Path):
    translated_dir = dir_path / "translated_rust"
    if not translated_dir.exists() or not translated_dir.is_dir():
        return None

    outer0 = dir_path.parent.parent.name
    outer = dir_path.parent.name
    inner = dir_path.name
    out_file = out_dir / f"{outer0}-{outer}-{inner}.txt"

    try:
        result = subprocess.run(
            ["/home/ubuntu/local/bin/crat-finder", "unsafe", str(translated_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        out_file.write_text(result.stdout)
        return out_file
    except Exception as e:
        print(f"{dir_path}: {e}")
        pass


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} [path] [path2]")
        sys.exit(1)

    path = Path(sys.argv[1]).resolve()
    path2 = Path(sys.argv[2]).resolve()

    out_dir = path2 / path.name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    dirs = list(path.glob("Hidden-Tests/*/*/")) + list(path.glob("Public-Tests/*/*/"))
    dirs = [d for d in dirs if (d / "translated_rust").exists()]
    max_workers = os.cpu_count() or 1

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_cratfinder, d, out_dir): d for d in dirs}
        for future in as_completed(futures):
            result = future.result()
            if result:
                print(result)


if __name__ == "__main__":
    main()
