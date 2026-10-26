"""LLM client behavior (fake provider, no network) and provider
payload/parse mapping (pure functions, no network)."""

from pathlib import Path
from typing import Any

import pytest

from proctor.llm.client import LlmClient, _truncate
from proctor.llm.providers import anthropic, openai
from proctor.llm.providers.replay import RecordingProvider, ReplayProvider
from proctor.llm.types import (
    AuthError,
    ContextLimitExceeded,
    Message,
    ProviderError,
    RateLimited,
    Request,
    Response,
    Usage,
)
from proctor.usage.tracker import UsageTracker, read_usage


class FakeProvider:
    name = "fake"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[Request] = []

    def complete(self, request: Request) -> Response:
        self.calls.append(request)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        assert isinstance(action, Response)
        return action


def _response(text: str = "hi") -> Response:
    return Response(
        text=text,
        finish_reason="stop",
        provider="fake",
        model="fake-1",
        latency_s=0.01,
        usage=Usage(input_tokens=10, cached_input_tokens=2, output_tokens=5),
    )


def _request(content: str = "hello") -> Request:
    return Request(messages=(Message(role="user", content=content),))


def _client(
    provider: FakeProvider, tmp_path: Path, **settings: Any
) -> tuple[LlmClient, Path]:
    log = tmp_path / "usage.jsonl"
    tracker = UsageTracker(log, run_id="r", stage="s")
    client = LlmClient(
        {"provider": "fake", "model": "fake-1", **settings},
        tracker=tracker,
        provider=provider,
        sleep=lambda _s: None,
    )
    return client, log


def test_retry_then_success(tmp_path: Path) -> None:
    provider = FakeProvider(
        [ProviderError("boom", status=500), RateLimited("slow"), _response()]
    )
    client, log = _client(provider, tmp_path)
    response = client.complete(_request())
    assert response.text == "hi"
    records = read_usage(log)
    assert len(records) == 3  # two failures + one success, all tracked
    assert [r["finish_reason"] for r in records] == ["error", "error", "stop"]
    assert records[2]["input_tokens"] == 10


def test_retries_exhausted(tmp_path: Path) -> None:
    provider = FakeProvider([ProviderError("boom")] * 3)
    client, _ = _client(provider, tmp_path, max_retries=2)
    with pytest.raises(ProviderError):
        client.complete(_request())
    assert len(provider.calls) == 3


def test_auth_error_not_retried(tmp_path: Path) -> None:
    provider = FakeProvider([AuthError("bad key"), _response()])
    client, log = _client(provider, tmp_path)
    with pytest.raises(AuthError):
        client.complete(_request())
    assert len(provider.calls) == 1
    assert read_usage(log)[0]["error"].startswith("AuthError")


def test_context_limit_error_mode(tmp_path: Path) -> None:
    provider = FakeProvider([ContextLimitExceeded("too long")])
    client, _ = _client(provider, tmp_path)
    with pytest.raises(ContextLimitExceeded):
        client.complete(_request())


def test_context_limit_truncate_mode(tmp_path: Path) -> None:
    provider = FakeProvider(
        [ContextLimitExceeded("too long"), ContextLimitExceeded("still"), _response()]
    )
    client, _ = _client(provider, tmp_path, context_overflow="truncate_middle")
    request = Request(
        messages=tuple(Message(role="user", content=f"m{i}" * 100) for i in range(4))
    )
    response = client.complete(request)
    assert response.text == "hi"
    # each retry sent fewer/smaller messages
    assert len(provider.calls[-1].messages) < len(request.messages)


def test_truncate_single_message_halves() -> None:
    request = _request("x" * 10000)
    smaller = _truncate(request, "truncate_middle")
    assert smaller is not None
    assert len(smaller.messages[0].content) < 6000
    assert "[truncated]" in smaller.messages[0].content


def test_anthropic_payload_and_parse() -> None:
    request = Request(
        messages=(Message(role="user", content="hi"),),
        system="be brief",
        temperature=0.5,
    )
    payload = anthropic.build_payload(request, "claude-opus-4-8")
    assert payload["model"] == "claude-opus-4-8"
    assert payload["system"] == "be brief"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]

    body = {
        "model": "claude-opus-4-8",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {
            "input_tokens": 100,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
            "output_tokens": 20,
        },
    }
    response = anthropic.parse_http(200, {}, body, 1.0)
    assert response.text == "hello"
    assert response.usage.input_tokens == 150  # total incl. cached
    assert response.usage.cached_input_tokens == 40
    assert response.usage.output_tokens == 20


def test_anthropic_error_mapping() -> None:
    with pytest.raises(AuthError):
        anthropic.parse_http(401, {}, {"error": {"message": "no"}}, 0.0)
    with pytest.raises(RateLimited) as rate:
        anthropic.parse_http(429, {"retry-after": "7"}, {"error": {}}, 0.0)
    assert rate.value.retry_after_s == 7.0
    with pytest.raises(ContextLimitExceeded):
        anthropic.parse_http(
            400, {}, {"error": {"message": "prompt is too long: 250000"}}, 0.0
        )
    with pytest.raises(ProviderError):
        anthropic.parse_http(529, {}, {"error": {"message": "overloaded"}}, 0.0)


def test_openai_payload_and_parse() -> None:
    request = Request(messages=(Message(role="user", content="hi"),), system="be brief")
    payload = openai.build_payload(request, "gpt-5")
    assert payload["messages"][0] == {"role": "system", "content": "be brief"}

    body = {
        "model": "gpt-5",
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 150,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
    }
    response = openai.parse_http(200, {}, body, 1.0)
    assert response.usage.input_tokens == 150
    assert response.usage.cached_input_tokens == 40
    assert response.usage.reasoning_tokens == 12


def test_openai_context_limit_mapping() -> None:
    body = {"error": {"message": "too long", "code": "context_length_exceeded"}}
    with pytest.raises(ContextLimitExceeded):
        openai.parse_http(400, {}, body, 0.0)


def test_record_then_replay(tmp_path: Path) -> None:
    inner = FakeProvider([_response("recorded!")])
    recording = RecordingProvider(inner, tmp_path / "cassettes", model="fake-1")
    request = _request("cassette me")
    live = recording.complete(request)

    replay = ReplayProvider(
        {"cassette_dir": str(tmp_path / "cassettes"), "model": "fake-1"}
    )
    replayed = replay.complete(request)
    assert replayed.text == live.text == "recorded!"
    assert replayed.usage.input_tokens == live.usage.input_tokens

    with pytest.raises(ProviderError, match="no cassette"):
        replay.complete(_request("never recorded"))


def test_extra_passthrough_anthropic() -> None:
    payload = anthropic.build_payload(
        _request(),
        "claude-opus-4-8",
        {"thinking": {"type": "enabled", "budget_tokens": 8000}},
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert anthropic.build_payload(_request(), "m").get("thinking") is None


def test_extra_passthrough_openai() -> None:
    payload = openai.build_payload(_request(), "gpt-5", {"reasoning_effort": "high"})
    assert payload["reasoning_effort"] == "high"
    assert "reasoning_effort" not in openai.build_payload(_request(), "gpt-5")


def test_client_error_4xx_not_retried(tmp_path: Path) -> None:
    provider = FakeProvider([ProviderError("bad request", status=400), _response()])
    client, log = _client(provider, tmp_path)
    with pytest.raises(ProviderError):
        client.complete(_request())
    assert len(provider.calls) == 1  # deterministic 4xx: exactly one attempt
    assert read_usage(log)[0]["error"].startswith("ProviderError")


def test_429_status_still_retried(tmp_path: Path) -> None:
    provider = FakeProvider([ProviderError("throttled", status=429), _response()])
    client, _ = _client(provider, tmp_path)
    assert client.complete(_request()).text == "hi"


def test_concurrent_clients_share_rate_limit_for_every_retry(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    import proctor.llm.client as client_module

    now = [0.0]
    waits = []

    def sleep(delay):
        waits.append(delay)
        now[0] += delay

    monkeypatch.setattr(
        client_module, "time", SimpleNamespace(monotonic=lambda: now[0])
    )
    limiter = client_module.RateLimiter(2)
    log = tmp_path / "shared.jsonl"
    trackers = [UsageTracker(log) for _ in range(4)]
    clients = [
        LlmClient(
            {"model": "fake-1", "max_retries": 1},
            tracker=tracker,
            provider=FakeProvider([RateLimited("retry", retry_after_s=0), _response()]),
            sleep=sleep,
            limiter=limiter,
        )
        for tracker in trackers
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda client: client.complete(_request()), clients))
    assert len(responses) == 4
    assert len(read_usage(log)) == 8
    assert all(tracker.record_count == 2 for tracker in trackers)
    assert sum(waits) == 180
