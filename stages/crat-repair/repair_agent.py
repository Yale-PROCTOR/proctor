from __future__ import annotations

import argparse
import json
from pathlib import Path

from dr_agent.agents.registry import get_agent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    workspace = Path(request["workspace"])
    agent = get_agent(request["agent"], model=request["model"])
    # The parent enforces the timeout on this process and all agent children.
    result = agent.run(request["prompt"], workspace, request["timeout_s"] + 60)
    args.result.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
