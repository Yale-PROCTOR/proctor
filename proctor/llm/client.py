"""The vendor-agnostic client: retry, rate limiting, context-overflow
handling, and usage tracking around any provider.

Every attempt — including failures — emits a usage record before the
call returns (plan §5.2).
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import replace
from threading import Lock
from typing import Any, Callable

from proctor.llm.providers.base import Provider, create_provider
from proctor.llm.types import (
    AuthError,
    ContextLimitExceeded,
    LlmError,
    ProviderError,
    RateLimited,
    Request,
    Response,
)
from proctor.usage.tracker import UsageTracker

_RETRYABLE = (ProviderError, RateLimited)


def _is_client_error(exc: LlmError) -> bool:
    """A 4xx other than 429 is a deterministic request problem —
    retrying only wastes wall-clock and rate budget."""
    return (
        isinstance(exc, ProviderError)
        and exc.status is not None
        and 400 <= exc.status < 500
        and exc.status != 429
    )


class RateLimiter:
    """Sliding-window requests-per-minute limiter."""

    def __init__(self, requests_per_minute: int | None) -> None:
        self._rpm = requests_per_minute
        self._window: deque[float] = deque()
        self._lock = Lock()

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> None:
        if not self._rpm:
            return
        with self._lock:
            now = time.monotonic()
            while self._window and now - self._window[0] >= 60:
                self._window.popleft()
            if len(self._window) >= self._rpm:
                wait = 60 - (now - self._window[0])
                if wait > 0:
                    sleep(wait)
                self._window.popleft()
            self._window.append(time.monotonic())


def _truncate(request: Request, strategy: str) -> Request | None:
    """One truncation step; None when nothing further can be dropped."""
    messages = list(request.messages)
    if len(messages) > 1:
        if strategy == "truncate_head":
            messages = messages[1:]
        else:  # truncate_middle: drop from the middle, keep first and last
            del messages[len(messages) // 2]
        return replace(request, messages=tuple(messages))
    if messages and len(messages[0].content) > 2000:
        half = len(messages[0].content) // 2
        content = messages[0].content
        if strategy == "truncate_head":
            clipped = content[-half:]
        else:
            quarter = half // 2
            clipped = content[:quarter] + "\n...[truncated]...\n" + content[-quarter:]
        return replace(request, messages=(replace(messages[0], content=clipped),))
    return None


class LlmClient:
    """Configured from the ``[llm]`` settings dict a stage receives in
    its envelope (``framework.llm``)."""

    def __init__(
        self,
        settings: dict[str, Any],
        tracker: UsageTracker | None = None,
        provider: Provider | None = None,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider if provider is not None else create_provider(settings)
        self.tracker = tracker
        self._sleep = sleep
        self._max_retries = int(settings.get("max_retries", 5))
        self._overflow = str(settings.get("context_overflow", "error"))
        rate = settings.get("rate_limit", {})
        rpm = rate.get("requests_per_minute") if isinstance(rate, dict) else None
        self._limiter = (
            limiter if limiter is not None else RateLimiter(int(rpm) if rpm else None)
        )

    def complete(self, request: Request) -> Response:
        current = request
        truncations = 0
        attempt = 0
        while True:
            attempt += 1
            self._limiter.acquire(self._sleep)
            started = time.monotonic()
            try:
                response = self.provider.complete(current)
            except ContextLimitExceeded as exc:
                self._track_error(current, attempt, started, exc)
                if self._overflow == "error":
                    raise
                truncations += 1
                if truncations > 5:
                    raise
                smaller = _truncate(current, self._overflow)
                if smaller is None:
                    raise
                current = smaller
                continue
            except AuthError as exc:
                self._track_error(current, attempt, started, exc)
                raise
            except _RETRYABLE as exc:
                self._track_error(current, attempt, started, exc)
                if _is_client_error(exc):  # 4xx (except 429): deterministic, no retry
                    raise
                if attempt > self._max_retries:
                    raise
                self._sleep(self._backoff(attempt, exc))
                continue
            if self.tracker is not None:
                self.tracker.record(
                    metadata=current.metadata,
                    provider=response.provider,
                    model=response.model,
                    usage=response.usage,
                    latency_s=response.latency_s,
                    finish_reason=response.finish_reason,
                    attempt=attempt,
                )
            return response

    def _backoff(self, attempt: int, exc: LlmError) -> float:
        if isinstance(exc, RateLimited) and exc.retry_after_s is not None:
            return exc.retry_after_s
        return min(60.0, (2.0**attempt) * (0.5 + random.random()))

    def _track_error(
        self, request: Request, attempt: int, started: float, exc: LlmError
    ) -> None:
        if self.tracker is not None:
            self.tracker.record(
                metadata=request.metadata,
                provider=getattr(self.provider, "name", "unknown"),
                model=request.model or str(self.settings.get("model", "")),
                usage=None,
                latency_s=time.monotonic() - started,
                finish_reason="error",
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
