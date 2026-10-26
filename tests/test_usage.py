"""Pricing and report aggregation."""

import json
from pathlib import Path

import pytest

from proctor.cli import main
from proctor.llm.types import Usage
from proctor.usage.pricing import PricingTable
from proctor.usage.report import aggregate, collect, render_table


def test_pricing_cost() -> None:
    table = PricingTable.from_config(
        {
            "pricing": {
                "anthropic/claude-opus-4-8": {
                    "input": 10.0,
                    "cached_input": 1.0,
                    "output": 50.0,
                }
            }
        }
    )
    usage = Usage(
        input_tokens=1_000_000, cached_input_tokens=500_000, output_tokens=100_000
    )
    # 500k uncached * $10/M + 500k cached * $1/M + 100k out * $50/M
    assert table.cost("anthropic", "claude-opus-4-8", usage) == 5.0 + 0.5 + 5.0


def test_pricing_unknown_model_is_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    table = PricingTable.from_config({})
    assert table.cost("x", "y", Usage(input_tokens=5)) is None
    table.cost("x", "y", Usage(input_tokens=5))  # second call: no repeat warning
    assert capsys.readouterr().err.count("no pricing") == 1


def _write_usage(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _sample_run(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "r1"
    _write_usage(
        run / "stages" / "00-a" / "usage.jsonl",
        [
            {
                "stage": "a",
                "model": "m1",
                "input_tokens": 100,
                "output_tokens": 10,
                "latency_s": 1.0,
                "cost_usd": 0.5,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            },
            {
                "stage": "a",
                "model": "m1",
                "input_tokens": 200,
                "output_tokens": 20,
                "latency_s": 2.0,
                "cost_usd": 1.0,
                "cached_input_tokens": 50,
                "reasoning_tokens": 0,
            },
        ],
    )
    _write_usage(
        run / "stages" / "01-b" / "usage.jsonl",
        [
            {
                "stage": "b",
                "model": "m2",
                "input_tokens": 1000,
                "output_tokens": 100,
                "latency_s": 5.0,
                "cost_usd": 3.0,
                "cached_input_tokens": 0,
                "reasoning_tokens": 7,
            },
        ],
    )
    return run


def test_collect_and_aggregate(tmp_path: Path) -> None:
    run = _sample_run(tmp_path)
    records = collect([run])
    assert len(records) == 3
    rows = aggregate(records, ["stage", "model"])
    by_stage = {row["stage"]: row for row in rows}
    assert by_stage["a"]["calls"] == 2
    assert by_stage["a"]["input_tokens"] == 300
    assert by_stage["a"]["cost_usd"] == 1.5
    assert by_stage["b"]["reasoning_tokens"] == 7
    assert rows[0]["stage"] == "b"  # sorted by cost desc


def test_aggregate_unknown_cost_propagates(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "r2"
    _write_usage(
        run / "usage.jsonl",
        [
            {
                "stage": "a",
                "model": "m",
                "input_tokens": 10,
                "output_tokens": 1,
                "latency_s": 1.0,
                "cost_usd": 0.5,
            },
            {
                "stage": "a",
                "model": "m",
                "input_tokens": 10,
                "output_tokens": 1,
                "latency_s": 1.0,
                "cost_usd": None,
            },
        ],
    )
    rows = aggregate(collect([run]), ["stage"])
    assert rows[0]["cost_usd"] is None  # partial pricing must not fake a total


def test_report_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _sample_run(tmp_path)
    code = main(["report", str(run), "--group-by", "stage", "--format", "csv"])
    out = capsys.readouterr().out
    assert code == 0
    assert "stage" in out.splitlines()[0]
    assert any(line.startswith("a,") for line in out.splitlines())


def test_render_table_empty() -> None:
    assert "no usage records" in render_table([])


def test_concurrent_trackers_append_complete_records_with_private_counts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from proctor.llm.types import RequestMetadata, Usage
    from proctor.usage.tracker import UsageTracker, read_usage

    path = tmp_path / "usage.jsonl"
    trackers = [UsageTracker(path) for _ in range(8)]

    def record(worker):
        for attempt in range(25):
            trackers[worker].record(
                metadata=RequestMetadata(item=str(worker)),
                provider="fixture",
                model="model",
                usage=Usage(input_tokens=1, output_tokens=2),
                latency_s=0,
                finish_reason="stop",
                attempt=attempt + 1,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(8)))
    records = read_usage(path)
    assert len(records) == 200
    assert all(tracker.record_count == 25 for tracker in trackers)
    assert sum(item["input_tokens"] for item in records) == 200
    assert sorted((int(item["item"]), item["attempt"]) for item in records) == [
        (worker, attempt) for worker in range(8) for attempt in range(1, 26)
    ]
