"""Request correlation IDs for HTTP and WebSocket traffic.

``RequestContextMiddleware`` is a pure ASGI middleware (so the context variable
is visible to every inner middleware, dependency, endpoint and thread-pool call):

* accepts an inbound ``X-Request-ID`` only when it is at most 128 characters of
  ``[A-Za-z0-9._-]``; anything else is replaced with a fresh ``uuid4``;
* binds it with ``utils.logger.bind_log_context(request_id=...)`` so every log
  record emitted while handling the request carries ``request_id``;
* exposes it as ``request.state.request_id`` and echoes it in the
  ``X-Request-ID`` response header;
* logs one ``request completed`` line per HTTP request with method, route
  template (never the raw path or query string), status code and duration;
* records ``http_requests_total`` / ``http_request_duration_seconds`` (labels:
  method, route template, status class) via ``utils.metrics``. ``/metrics``
  itself is excluded so scrapes do not inflate request metrics.

Bodies, headers, client addresses and query strings are never logged.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from utils import metrics
from utils.logger import bind_log_context, get_logger

REQUEST_ID_HEADER = "X-Request-ID"
_PROBE_ROUTES = frozenset({"/health", "/health/live", "/health/ready", "/readiness", "/liveness"})
_UNMETERED_ROUTES = frozenset({"/metrics"})
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

logger = get_logger(__name__)


def is_safe_request_id(value: str | None) -> bool:
    return bool(value) and _SAFE_REQUEST_ID.fullmatch(value) is not None


def resolve_request_id(inbound: str | None) -> str:
    """Return the inbound id when it is safe to log and echo, otherwise a new uuid4."""
    if inbound is not None and is_safe_request_id(inbound):
        return inbound
    return str(uuid.uuid4())


def _inbound_request_id(scope: dict[str, Any]) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == _REQUEST_ID_HEADER_BYTES:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes any byte
                return None
    return None


def _route_template(scope: dict[str, Any]) -> str | None:
    route = scope.get("route")
    return getattr(route, "path", None)


class RequestContextMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(_inbound_request_id(scope))
        scope.setdefault("state", {})["request_id"] = request_id

        if scope["type"] == "websocket":
            with bind_log_context(request_id=request_id):
                await self.app(scope, receive, send)
            return

        status_code = 500
        header_value = request_id.encode("latin-1")

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != _REQUEST_ID_HEADER_BYTES
                ]
                headers.append((_REQUEST_ID_HEADER_BYTES, header_value))
                message = {**message, "headers": headers}
            await send(message)

        started = time.perf_counter()
        with bind_log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                route = _route_template(scope)
                elapsed = time.perf_counter() - started
                if route not in _UNMETERED_ROUTES:
                    method = scope.get("method")
                    metrics.inc(
                        "http_requests_total",
                        {
                            "method": method,
                            "route": route,
                            "status_class": metrics.status_class(status_code),
                        },
                    )
                    metrics.observe(
                        "http_request_duration_seconds",
                        elapsed,
                        {"method": method, "route": route},
                    )
                # Probe endpoints are polled constantly; keep them out of INFO logs.
                level = logging.DEBUG if route in _PROBE_ROUTES else logging.INFO
                logger.log(
                    level,
                    "request completed",
                    extra={
                        "http_method": scope.get("method"),
                        "route": route,
                        "status_code": status_code,
                        "duration_ms": round(elapsed * 1000, 2),
                    },
                )
