#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from stage import run_stage
from tooling import CratTools

from proctor.contracts import StageInput


def warm_tools(stage_dir: Path, tools: Any | None = None) -> tuple[Path, Path]:
    log = stage_dir / "build.log"
    active_tools = tools or CratTools(log)
    return active_tools.build_tools((stage_dir / "../crat").resolve())


def main(argv: list[str] | None = None, *, tools: Any | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args(argv)
    stage_dir = Path(__file__).parent
    if args.build_only:
        try:
            crat, crat_tool = warm_tools(stage_dir, tools)
        except Exception as exc:  # noqa: BLE001 - CLI must report warmup failures
            print(f"tool warmup failed: {exc}", file=sys.stderr)
            return 1
        print(crat)
        print(crat_tool)
        return 0
    if args.input is None or args.output is None:
        parser.error("--input and --output are required unless --build-only is used")
    try:
        stage_input = StageInput.read(args.input)
        output = run_stage(stage_input, stage_dir=stage_dir, tools=tools)
    except Exception as exc:  # noqa: BLE001 - a stage must always write an envelope
        from proctor.contracts import StageOutput

        output = StageOutput(
            status="failure",
            stage_id="local_transformation",
            stage_version="0.1.0",
            error=f"{type(exc).__name__}: {exc}",
        )
    output.write(args.output)
    return 0 if output.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
