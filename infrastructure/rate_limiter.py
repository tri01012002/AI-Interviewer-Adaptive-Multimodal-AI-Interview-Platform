"""Rate limiting primitives.

Two families live here:

* ``InMemoryRateLimiter`` / ``RedisRateLimiter`` keep the original ``allow(key)``
  API (raise ``RateLimitExceeded`` when over the limit).
* ``InMemoryFixedWindowCounter`` / ``RedisFixedWindowCounter`` implement a fixed
  window counter returning a ``RateLimitDecision`` (with ``retry_after_seconds``)
  and back the HTTP endpoint limits in ``apps.api.middleware.rate_limit``.

Redis counters are atomic: each window gets its own key
(``<namespace>:<key>:<window index>``) and ``INCR`` + ``EXPIRE`` are sent in a
single ``MULTI``/``EXEC`` transaction, so a counter can never be left behind
without a TTL and concurrent callers on any number of API instances share one
count. No Redis 7-only command (e.g. ``EXPIRE NX``) is required.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class RateLimitExceeded(RuntimeError):
    """Raised when a caller exceeds the configured per-window limit."""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    count: int
    retry_after_seconds: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)


def _window_position(now: float, window_seconds: int) -> tuple[int, int]:
    """Return (window index, seconds until the window resets, at least 1)."""
    window_index = int(now // window_seconds)
    reset_at = (window_index + 1) * window_seconds
    return window_index, max(1, math.ceil(reset_at - now))


class InMemoryFixedWindowCounter:
    """Process-local fixed window counter.

    Only suitable for a single API process (development, tests, or deployments
    without Redis): every process keeps its own counts.
    """

    _PRUNE_THRESHOLD = 10_000

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[int, int]] = {}

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        window_index, retry_after = _window_position(self._clock(), window_seconds)
        with self._lock:
            stored_window, count = self._counts.get(key, (window_index, 0))
            if stored_window != window_index:
                count = 0
            count += 1
            self._counts[key] = (window_index, count)
            if len(self._counts) > self._PRUNE_THRESHOLD:
                self._prune(window_index)
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            count=count,
            retry_after_seconds=retry_after,
        )

    def _prune(self, current_window: int) -> None:
        stale = [key for key, (window, _) in self._counts.items() if window < current_window]
        for key in stale:
            del self._counts[key]


class RedisFixedWindowCounter:
    """Fixed window counter shared through Redis (atomic INCR + EXPIRE)."""

    def __init__(
        self,
        redis_client: Any,
        namespace: str = "ai:ratelimit",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis = _raw_redis_client(redis_client)
        self.namespace = namespace
        self._clock = clock

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        window_index, retry_after = _window_position(self._clock(), window_seconds)
        redis_key = f"{self.namespace}:{key}:{window_index}"
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.incr(redis_key)
        # The key is unique to this window, so refreshing the TTL on every hit is
        # harmless; the extra second covers clock skew between API instances.
        pipeline.expire(redis_key, window_seconds + 1)
        count, _ = pipeline.execute()
        count = int(count)
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            count=count,
            retry_after_seconds=retry_after,
        )


def _raw_redis_client(redis_client: Any) -> Any:
    """Accept either a redis-py client or ``infrastructure.redis_store.RedisClient``."""
    from infrastructure.redis_store import RedisClient

    if isinstance(redis_client, RedisClient):
        return redis_client.client
    return redis_client


class RateLimiter:
    def allow(self, key: str) -> bool:
        raise NotImplementedError


class InMemoryRateLimiter(RateLimiter):
    def __init__(self, limit: int = 10, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets[key]
        while bucket and (now - bucket[0]) > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.limit:
            raise RateLimitExceeded(f"Rate limit exceeded for {key}")
        bucket.append(now)
        return True


class RedisRateLimiter(RateLimiter):
    """Fixed window limiter backed by Redis; INCR and EXPIRE are atomic."""

    def __init__(self, redis_client, limit: int = 10, window_seconds: int = 60, namespace: str = "ratelimit") -> None:
        self.redis = redis_client
        self.limit = limit
        self.window_seconds = window_seconds
        self.namespace = namespace
        self._counter = RedisFixedWindowCounter(redis_client, namespace=namespace)

    def _key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def allow(self, key: str) -> bool:
        decision = self._counter.hit(key, self.limit, self.window_seconds)
        if not decision.allowed:
            raise RateLimitExceeded(f"Rate limit exceeded for {key}")
        return True
