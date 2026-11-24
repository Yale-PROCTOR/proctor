#!/usr/bin/env python3

import argparse
from decimal import Decimal
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sum event durations across PROCTOR run directories."
    )
    parser.add_argument(
        "--prefix", default="", help="collect runs with this name prefix"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=ROOT / "runs",
        help="directory containing runs (default: %(default)s)",
    )
    args = parser.parse_args()

    total = Decimal(0)
    for run in sorted(args.dir.resolve().glob("*")):
        if not run.is_dir() or not run.name.startswith(args.prefix):
            continue
        events = run / "events.jsonl"
        if not events.is_file():
            continue
        with events.open(encoding="utf-8") as log:
            for line in log:
                if line.strip():
                    event = json.loads(line, parse_float=Decimal)
                    if isinstance(event, dict):
                        total += event.get("duration_s", 0)

    print(total)


if __name__ == "__main__":
    main()
