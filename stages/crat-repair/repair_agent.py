from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from dr_agent.agents.registry import get_agent


def terminate(signum: int, _frame: object) -> None:
    # uv can forward the signal the worker already received with its group.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if (args.request is None) != (args.result is None):
        parser.error("--request and --result must be used together")
    request = (
        json.loads(args.request.read_text(encoding="utf-8"))
        if args.request is not None
        else json.load(sys.stdin)
    )
    workspace = Path(request["workspace"])
    agent = get_agent(request["agent"], model=request["model"])
    timeout_s = request["timeout_s"]
    if args.result is not None:
        # Crat enforces its own timeout on the driver and its children.
        result = agent.run(request["prompt"], workspace, timeout_s + 60)
        args.result.write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        return 0

    # Let the agent driver reap its process group on stage cancellation.
    signal.signal(signal.SIGTERM, terminate)
    generated_config = workspace / "opencode.json"
    cleanup_config = request["agent"] == "opencode" and not any(
        (workspace / name).exists() for name in ("opencode.json", ".opencode.json")
    )
    try:
        result = agent.run(request["prompt"], workspace, timeout_s)
    finally:
        if cleanup_config and not workspace.is_symlink():
            generated_config.unlink(missing_ok=True)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
