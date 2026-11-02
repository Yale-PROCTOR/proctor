#!/usr/bin/env python3

import argparse
from collections import defaultdict
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize local-transformation test validation across PROCTOR runs."
    )
    parser.add_argument(
        "--prefix", default="", help="collect runs with this name prefix"
    )
    args = parser.parse_args()

    runs_by_category: defaultdict[tuple[str, int], list[Path]] = defaultdict(list)
    for run in sorted(ROOT.glob("runs/*")):
        if not run.is_dir() or not run.name.startswith(args.prefix):
            continue
        for path in sorted(
            (run / "stages").glob(
                "[0-9]*-local_transformation/out/artifacts/test-validation.json"
            )
        ):
            if not path.is_file():
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            runs_by_category[report["status"], report["repair_attempts"]].append(run)

    print(f"Collected runs: {sum(len(runs) for runs in runs_by_category.values())}")
    print("status\trepair_attempts\truns")
    for (status, repair_attempts), runs in sorted(runs_by_category.items()):
        print(f"{status}\t{repair_attempts}\t{len(runs)}")
        if (status, repair_attempts) != ("validation_succeeded", 0):
            for run in runs:
                print(f"  {run}")


if __name__ == "__main__":
    main()
