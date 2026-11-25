"""Request correlation IDs and endpoint rate limits (Phase F).

HTTP-level rate-limit tests use an isolated in-memory backend with a frozen
clock so they neither depend on nor pollute a shared Redis, and never straddle a
window boundary. The Redis counter is exercised against a recording fake client;
no real Redis server is involved.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.middleware import rate_limit
from apps.api.middleware.rate_limit import endpoint_rate_limiter, enforce_rate_limit
from apps.api.middleware.request_context import REQUEST_ID_HEADER, resolve_request_id
from config import settings
from infrastructure.rate_limiter import (
    InMemoryFixedWindowCounter,
    RateLimitExceeded,
    RedisFixedWindowCounter,
    RedisRateLimiter,
)
from tests.unit.tenant_test_utils import (
    create_candidate_through_invitation,
    create_tenant,
    start_interview,
    tenant_headers,
)
from utils.logger import LogContextFilter, bind_log_context, get_log_context

FROZEN_NOW = 6_000_030.0  # 30 seconds into a 60-second window


class ClientIpShim:
    """Test-only ASGI wrapper letting a request choose its client IP."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"x-test-client-ip":
                    scope = {**scope, "client": (value.decode("ascii"), 50000)}
        await self.inner(scope, receive, send)


@pytest.fixture
def memory_backend():
    backend = InMemoryFixedWindowCounter(clock=lambda: FROZEN_NOW)
    endpoint_rate_limiter.set_backend(backend)
    yield backend
    endpoint_rate_limiter.set_backend(None)


@pytest.fixture
def client(memory_backend):
    with TestClient(ClientIpShim(app)) as test_client:
        yield test_client


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.INFO)
    caplog.handler.addFilter(LogContextFilter())
    return caplog


def candidate_with_interview(client: TestClient, tenant=None):
    tenant = tenant or create_tenant(client)
    _candidate, headers, profile, _ = create_candidate_through_invitation(client, tenant)
    interview_id = start_interview(client, tenant, profile["candidate_id"])["interview_id"]
    return tenant, tenant_headers(headers, tenant.organization_id), interview_id


def answer(client: TestClient, interview_id: str, headers: dict[str, str], extra_headers=None):
    return client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": f"turn-{uuid.uuid4()}", "answer": "I built Python services."},
        headers={**headers, **(extra_headers or {})},
    )


# ---------------------------------------------------------------- request ids


def test_request_id_is_generated_and_echoed(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    generated = response.headers[REQUEST_ID_HEADER]
    assert str(uuid.UUID(generated)) == generated
    assert client.get("/health").headers[REQUEST_ID_HEADER] != generated


def test_safe_inbound_request_id_is_preserved(client: TestClient):
    response = client.get("/health/live", headers={REQUEST_ID_HEADER: "edge-01.trace_ABC-9"})
    assert response.headers[REQUEST_ID_HEADER] == "edge-01.trace_ABC-9"


@pytest.mark.parametrize(
    "unsafe",
    ["has space", "a" * 129, "<script>", "id;drop", "x/../y", "{json}"],
)
def test_unsafe_inbound_request_id_is_replaced(client: TestClient, unsafe: str):
    response = client.get("/health", headers={REQUEST_ID_HEADER: unsafe})
    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed != unsafe
    assert str(uuid.UUID(echoed)) == echoed


def test_resolve_request_id_boundaries():
    assert resolve_request_id("a" * 128) == "a" * 128
    assert resolve_request_id("") != ""
    assert resolve_request_id(None)


def test_log_records_emitted_during_request_carry_request_id(
    client: TestClient, captured, monkeypatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_PER_MINUTE", 1)
    _tenant, headers, interview_id = candidate_with_interview(client)
    captured.clear()

    trace = {REQUEST_ID_HEADER: "trace-log-1"}
    assert answer(client, interview_id, headers, trace).status_code == 200
    limited = answer(client, interview_id, headers, {REQUEST_ID_HEADER: "trace-log-2"})
    assert limited.status_code == 429

    # Emitted from a sync dependency running in the thread pool.
    exceeded = [r for r in captured.records if r.getMessage() == "Rate limit exceeded"]
    assert [r.request_id for r in exceeded] == ["trace-log-2"]
    completed = {
        r.request_id: r for r in captured.records if r.getMessage() == "request completed"
    }
    assert completed["trace-log-1"].status_code == 200
    assert completed["trace-log-1"].route == "/api/v1/interview/{interview_id}/answer"
    assert completed["trace-log-2"].status_code == 429
    # Nothing request-scoped leaks once the request finished.
    assert get_log_context() == {}


def test_bind_log_context_for_background_work(captured):
    worker_logger = logging.getLogger("tests.campaign_worker")
    with bind_log_context(execution_id="exec-1", communication_id=None):
        with bind_log_context(communication_id="comm-7"):
            worker_logger.info("dispatching")
        worker_logger.info("explicit wins", extra={"execution_id": "override"})
    worker_logger.info("outside")

    records = {r.getMessage(): r for r in captured.records}
    assert records["dispatching"].execution_id == "exec-1"
    assert records["dispatching"].communication_id == "comm-7"
    assert records["explicit wins"].execution_id == "override"
    assert not hasattr(records["explicit wins"], "communication_id")
    assert not hasattr(records["outside"], "execution_id")

    with pytest.raises(ValueError):
        with bind_log_context(email="person@example.com"):
            pass


# --------------------------------------------------------------- rate limits


def test_answer_endpoint_returns_429_with_retry_after_per_user(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_PER_MINUTE", 2)
    tenant, headers, interview_id = candidate_with_interview(client)
    _tenant, other_headers, other_interview_id = candidate_with_interview(client, tenant)

    assert answer(client, interview_id, headers).status_code == 200
    assert answer(client, interview_id, headers).status_code == 200
    limited = answer(client, interview_id, headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"
    assert limited.headers[REQUEST_ID_HEADER]

    # A different actor has an independent budget.
    assert answer(client, other_interview_id, other_headers).status_code == 200


def test_authentication_is_checked_before_rate_limit(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ANSWER_PER_MINUTE", 1)
    tenant, headers, interview_id = candidate_with_interview(client)
    assert answer(client, interview_id, headers).status_code == 200
    assert answer(client, interview_id, headers).status_code == 429

    anonymous = client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={"turn_id": "turn-anon", "answer": "hello"},
    )
    assert anonymous.status_code in {401, 403}
    recruiter = answer(
        client, interview_id, tenant_headers(tenant.recruiter_headers, tenant.organization_id)
    )
    assert recruiter.status_code == 403


def test_cv_upload_is_rate_limited(client: TestClient, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "CV_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "RATE_LIMIT_CV_UPLOAD_PER_MINUTE", 1)
    tenant = create_tenant(client)
    _candidate, candidate_headers, _profile, _ = create_candidate_through_invitation(client, tenant)
    headers = tenant_headers(candidate_headers, tenant.organization_id)
    url = f"/api/v1/recruitment-campaigns/{uuid.uuid4()}/applications/cv"
    files = {"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")}

    first = client.post(url, files=files, headers=headers)
    assert first.status_code != 429
    second = client.post(url, files=files, headers=headers)
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1


def test_login_is_limited_per_client_ip(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 2)
    email = f"login-{uuid.uuid4()}@example.com"
    assert client.post(
        "/api/v1/auth/register", json={"email": email, "password": "password123"}
    ).status_code == 200
    credentials = {"email": email, "password": "password123"}
    ip_a = {"x-test-client-ip": "203.0.113.10"}
    ip_b = {"x-test-client-ip": "203.0.113.20"}

    assert client.post("/api/v1/auth/login", json=credentials, headers=ip_a).status_code == 200
    assert client.post("/api/v1/auth/login", json=credentials, headers=ip_a).status_code == 200
    limited = client.post("/api/v1/auth/login", json=credentials, headers=ip_a)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"

    assert client.post("/api/v1/auth/login", json=credentials, headers=ip_b).status_code == 200


def test_limits_are_scoped_per_route_and_can_be_disabled(memory_backend, monkeypatch):
    enforce_rate_limit("interview_answer", "user:u1", 1)
    with pytest.raises(HTTPException) as exc:
        enforce_rate_limit("interview_answer", "user:u1", 1)
    assert exc.value.status_code == 429
    enforce_rate_limit("cv_upload", "user:u1", 1)

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    enforce_rate_limit("interview_answer", "user:u1", 1)


class FailingBackend:
    def __init__(self):
        self.calls = 0

    def hit(self, key, limit, window_seconds):
        self.calls += 1
        raise ConnectionError("redis down")


def test_backend_failure_fails_open_with_warning(client: TestClient, captured, monkeypatch):
    backend = FailingBackend()
    endpoint_rate_limiter.set_backend(backend)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    email = f"failopen-{uuid.uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    credentials = {"email": email, "password": "password123"}

    for _ in range(3):
        assert client.post("/api/v1/auth/login", json=credentials).status_code == 200

    # Backend is skipped during the retry cool-down instead of timing out each request.
    assert backend.calls == 1
    warnings = [r for r in captured.records if r.getMessage() == "Rate limit backend unavailable"]
    assert len(warnings) == 1
    assert warnings[0].failure_type == "ConnectionError"
    assert warnings[0].levelno == logging.WARNING
    assert "redis down" not in warnings[0].getMessage()


def test_backend_failure_can_fail_closed(memory_backend, monkeypatch):
    endpoint_rate_limiter.set_backend(FailingBackend())
    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_OPEN", False)
    with pytest.raises(HTTPException) as exc:
        enforce_rate_limit("login", "ip:198.51.100.1", 5)
    assert exc.value.status_code == 503
    assert exc.value.headers["Retry-After"]


def test_in_memory_backend_used_only_without_redis_url(monkeypatch):
    limiter = rate_limit.EndpointRateLimiter()
    monkeypatch.setattr(settings, "REDIS_URL", "")
    assert isinstance(limiter._current_backend(), InMemoryFixedWindowCounter)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:6390/0")
    assert isinstance(limiter._current_backend(), RedisFixedWindowCounter)


# ------------------------------------------------------- redis counter (fake)


class RecordingPipeline:
    def __init__(self, store, log, transaction):
        self.store = store
        self.log = log
        self.transaction = transaction
        self.commands = []

    def incr(self, key):
        self.commands.append(("incr", key))

    def expire(self, key, seconds):
        self.commands.append(("expire", key, seconds))

    def execute(self):
        self.log.append((self.transaction, list(self.commands)))
        results = []
        for command in self.commands:
            if command[0] == "incr":
                self.store[command[1]] = self.store.get(command[1], 0) + 1
                results.append(self.store[command[1]])
            else:
                results.append(True)
        return results


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.executed = []

    def pipeline(self, transaction=True):
        return RecordingPipeline(self.store, self.executed, transaction)

    def incr(self, *_args, **_kwargs):  # pragma: no cover - must not be used
        raise AssertionError("non-atomic INCR outside a transaction")

    def expire(self, *_args, **_kwargs):  # pragma: no cover - must not be used
        raise AssertionError("non-atomic EXPIRE outside a transaction")


def test_redis_counter_uses_single_transaction_per_hit():
    fake = FakeRedis()
    counter = RedisFixedWindowCounter(fake, namespace="ai:ratelimit", clock=lambda: FROZEN_NOW)

    decisions = [counter.hit("login:ip:1.2.3.4", 2, 60) for _ in range(3)]

    assert [d.allowed for d in decisions] == [True, True, False]
    assert [d.count for d in decisions] == [1, 2, 3]
    assert decisions[-1].retry_after_seconds == 30
    window_key = f"ai:ratelimit:login:ip:1.2.3.4:{int(FROZEN_NOW // 60)}"
    assert fake.executed == [
        (True, [("incr", window_key), ("expire", window_key, 61)])
    ] * 3


def test_redis_counter_starts_fresh_in_next_window():
    fake = FakeRedis()
    now = [FROZEN_NOW]
    counter = RedisFixedWindowCounter(fake, clock=lambda: now[0])
    assert counter.hit("k", 1, 60).allowed
    assert not counter.hit("k", 1, 60).allowed
    now[0] += 60
    assert counter.hit("k", 1, 60).allowed


def test_legacy_redis_rate_limiter_is_atomic():
    fake = FakeRedis()
    limiter = RedisRateLimiter(fake, limit=1, window_seconds=60)
    assert limiter.allow("user-1") is True
    with pytest.raises(RateLimitExceeded):
        limiter.allow("user-1")
    assert all(transaction for transaction, _ in fake.executed)
