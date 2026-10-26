"""Per-call usage records, one JSONL line per LLM invocation.

Synchronous appends — microseconds against multi-second LLM calls, and
a killed stage loses nothing (plan §5.2).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from proctor.llm.types import RequestMetadata, Usage
from proctor.usage.pricing import PricingTable


class UsageTracker:
    _append_lock = Lock()

    def __init__(
        self,
        path: Path,
        *,
        run_id: str | None = None,
        stage: str | None = None,
        item: str | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        self._path = path
        self._run_id = run_id
        self._stage = stage
        self._item = item
        self._pricing = pricing
        self.record_count = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        metadata: RequestMetadata,
        provider: str,
        model: str,
        usage: Usage | None,
        latency_s: float,
        finish_reason: str,
        attempt: int = 1,
        error: str | None = None,
    ) -> None:
        cost: float | None = None
        if usage is not None and self._pricing is not None:
            cost = self._pricing.cost(provider, model, usage)
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": metadata.run_id or self._run_id,
            "stage": metadata.stage or self._stage,
            "item": metadata.item or self._item,
            "attempt": attempt,
            "provider": provider,
            "model": model,
            "prompt_id": metadata.prompt_id,
            "prompt_version": metadata.prompt_version,
            "prompt_hash": metadata.prompt_hash,
            "input_tokens": usage.input_tokens if usage else 0,
            "cached_input_tokens": usage.cached_input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "reasoning_tokens": usage.reasoning_tokens if usage else None,
            "latency_s": round(latency_s, 3),
            "finish_reason": finish_reason,
            "cost_usd": cost,
            "error": error,
        }
        with self._append_lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            self.record_count += 1


def read_usage(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            if isinstance(data, dict):
                records.append(data)
    return records
