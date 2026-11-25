"""Fixed-window rate limits for expensive or abuse-prone endpoints.

Attach with ``dependencies=[Depends(...)]`` on a route:

* ``interview_answer_rate_limit`` - POST /interview/{id}/answer (LLM cost),
  keyed by authenticated actor id, ``RATE_LIMIT_ANSWER_PER_MINUTE``.
* ``cv_upload_rate_limit`` - POST /recruitment-campaigns/{id}/applications/cv,
  keyed by authenticated actor id, ``RATE_LIMIT_CV_UPLOAD_PER_MINUTE``.
* ``login_rate_limit`` - POST /auth/login, keyed by client IP,
  ``RATE_LIMIT_LOGIN_PER_MINUTE``. The IP is ``request.client.host``; behind a
  reverse proxy run uvicorn with ``--proxy-headers --forwarded-allow-ips=<proxy>``
  so it is the real client address. ``X-Forwarded-For`` is never parsed here
  because it is client-controlled.

Each key is ``<scope>:<identity>`` so limits are per user (or IP) *and* per route.
Actor-keyed limits depend on ``tenant_context``, so authentication and tenant
checks run first and are unaffected (401/403 win over 429). Rejections return
429 with ``Retry-After`` (seconds until the window resets).

Backend: Redis (shared across API instances, atomic INCR+EXPIRE in one
MULTI/EXEC) whenever ``REDIS_URL`` is set; a process-local in-memory counter is
used ONLY when ``REDIS_URL`` is empty.

Failure mode: if Redis errors, the request is allowed (fail OPEN) and a warning
is logged; Redis is then not retried for ``RATE_LIMIT_BACKEND_RETRY_SECONDS`` so
an outage does not add a timeout to every request. Rate limiting is abuse/cost
protection, not a security boundary. Set ``RATE_LIMIT_FAIL_OPEN=false`` to
reject with 503 instead. ``RATE_LIMIT_ENABLED=false`` disables all limits.

Every decision is counted in ``rate_limit_decisions_total{scope,outcome}``
(outcome: allowed / limited / backend_error); identities are never labels.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Annotated, Any, Protocol

from fastapi import Depends, HTTPException, Request, status

from config import settings
from infrastructure.rate_limiter import (
    InMemoryFixedWindowCounter,
    RateLimitDecision,
    RedisFixedWindowCounter,
)
from services.tenant_authorization import TenantContext, tenant_context
from utils import metrics
from utils.logger import get_logger

RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_NAMESPACE = "ai:ratelimit"

logger = get_logger(__name__)


class FixedWindowBackend(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision: ...


class EndpointRateLimiter:
    """Selects the backend from settings and applies the failure policy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._override: FixedWindowBackend | None = None
        self._backend: FixedWindowBackend | None = None
        self._backend_url: str | None = None
        self._unavailable_until = 0.0

    def set_backend(self, backend: FixedWindowBackend | None) -> None:
        """Force a backend (tests, custom wiring); ``None`` restores settings-based selection."""
        with self._lock:
            self._override = backend
            self._unavailable_until = 0.0

    def _current_backend(self) -> FixedWindowBackend:
        if self._override is not None:
            return self._override
        url = settings.REDIS_URL or ""
        with self._lock:
            if self._backend is None or self._backend_url != url:
                self._backend = self._build_backend(url)
                self._backend_url = url
            return self._backend

    @staticmethod
    def _build_backend(url: str) -> FixedWindowBackend:
        if not url:
            logger.info("Rate limiting uses the in-memory backend (REDIS_URL is not set)")
            return InMemoryFixedWindowCounter()
        import redis

        client = redis.Redis.from_url(
            url,
            socket_timeout=settings.REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=settings.REDIS_TIMEOUT_SECONDS,
        )
        return RedisFixedWindowCounter(client, namespace=RATE_LIMIT_NAMESPACE)

    def check(self, scope_name: str, identity: str, limit: int) -> RateLimitDecision | None:
        """Count one hit. Returns ``None`` when the backend is unavailable and policy is fail-open."""
        if time.monotonic() < self._unavailable_until:
            return self._unavailable(scope_name)
        try:
            backend = self._current_backend()
            return backend.hit(f"{scope_name}:{identity}", limit, RATE_LIMIT_WINDOW_SECONDS)
        except Exception as exc:
            self._unavailable_until = time.monotonic() + settings.RATE_LIMIT_BACKEND_RETRY_SECONDS
            logger.warning(
                "Rate limit backend unavailable",
                extra={
                    "rate_limit_scope": scope_name,
                    "failure_type": type(exc).__name__,
                    "fail_open": settings.RATE_LIMIT_FAIL_OPEN,
                    "retry_backend_after_seconds": settings.RATE_LIMIT_BACKEND_RETRY_SECONDS,
                },
            )
            return self._unavailable(scope_name)

    @staticmethod
    def _unavailable(scope_name: str) -> None:
        if settings.RATE_LIMIT_FAIL_OPEN:
            return None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Request could not be admitted; retry later.",
            headers={"Retry-After": str(max(1, int(settings.RATE_LIMIT_BACKEND_RETRY_SECONDS)))},
        )


endpoint_rate_limiter = EndpointRateLimiter()


def enforce_rate_limit(scope_name: str, identity: str, limit: int) -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    try:
        decision = endpoint_rate_limiter.check(scope_name, identity, limit)
    except HTTPException:
        # Fail-closed backend outage (503).
        metrics.inc("rate_limit_decisions_total", {"scope": scope_name, "outcome": "backend_error"})
        raise
    if decision is None:
        metrics.inc("rate_limit_decisions_total", {"scope": scope_name, "outcome": "backend_error"})
        return
    if decision.allowed:
        metrics.inc("rate_limit_decisions_total", {"scope": scope_name, "outcome": "allowed"})
        return
    metrics.inc("rate_limit_decisions_total", {"scope": scope_name, "outcome": "limited"})
    logger.warning(
        "Rate limit exceeded",
        extra={
            "rate_limit_scope": scope_name,
            "rate_limit": decision.limit,
            "retry_after_seconds": decision.retry_after_seconds,
        },
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests; retry later.",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


def _actor_rate_limit(scope_name: str, limit_setting: str) -> Callable[..., Any]:
    # Sync on purpose: FastAPI runs it in the thread pool, keeping Redis I/O off the event loop.
    def dependency(context: Annotated[TenantContext, Depends(tenant_context)]) -> None:
        enforce_rate_limit(
            scope_name,
            f"user:{context.actor_user_id}",
            int(getattr(settings, limit_setting)),
        )

    dependency.__name__ = f"{scope_name}_rate_limit"
    return dependency


interview_answer_rate_limit = _actor_rate_limit("interview_answer", "RATE_LIMIT_ANSWER_PER_MINUTE")
cv_upload_rate_limit = _actor_rate_limit("cv_upload", "RATE_LIMIT_CV_UPLOAD_PER_MINUTE")


def login_rate_limit(request: Request) -> None:
    client_host = request.client.host if request.client else "unknown"
    enforce_rate_limit("login", f"ip:{client_host}", settings.RATE_LIMIT_LOGIN_PER_MINUTE)
