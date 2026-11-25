"""Interview turn locking against a REAL Redis server.

Runs only when REDIS_INTEGRATION_URL points at a real Redis (CI service
container). fakeredis and other emulators are refused: they do not implement
``INFO server``, so a passing run here is real-Redis evidence.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

REDIS_URL = os.environ.get("REDIS_INTEGRATION_URL", "")

pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason="Set REDIS_INTEGRATION_URL to a real Redis to run these tests"
)

if REDIS_URL:
    import redis

    from apps.api.main import app
    from apps.api.v1.routes.interview import service as interview_service
    from infrastructure.distributed_lock import DistributedLock
    from infrastructure.redis_store import RedisClient
    from integrations.llm_providers.provider import LLMTimeoutError
    from tests.unit.tenant_test_utils import (
        create_candidate_through_invitation,
        create_tenant,
        start_interview,
        tenant_headers,
    )


@pytest.fixture(scope="module")
def admin_redis():
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        info = client.info("server")
    except redis.ResponseError as exc:  # fakeredis: unknown command 'info'
        pytest.fail(f"REDIS_INTEGRATION_URL is not a real Redis server: {exc}")
    if "process_id" not in info or "redis_version" not in info:
        pytest.fail("REDIS_INTEGRATION_URL is not a real Redis server")
    assert int(info["redis_version"].split(".")[0]) >= 6
    yield client
    client.close()


@pytest.fixture
def real_redis_service(monkeypatch: pytest.MonkeyPatch, admin_redis):
    """Point the API's interview service at the real Redis for this test."""
    client = RedisClient(REDIS_URL, namespace="interview", socket_timeout=2.0)
    monkeypatch.setattr(interview_service, "redis_client", client)
    monkeypatch.setattr(interview_service.redis_store, "client", client, raising=False)
    return client


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _interview(client: TestClient):
    tenant = create_tenant(client)
    _candidate, candidate_headers, profile, _ = create_candidate_through_invitation(
        client, tenant
    )
    interview = start_interview(client, tenant, profile["candidate_id"])
    return interview["interview_id"], tenant_headers(candidate_headers, tenant.organization_id)


def _lock_key(interview_id: str) -> str:
    return interview_service.redis_store._key(f"processing:{interview_id}")


def _answer(client, interview_id, headers, turn_id=None):
    return client.post(
        f"/api/v1/interview/{interview_id}/answer",
        json={
            "turn_id": turn_id or f"turn-{uuid4().hex[:12]}",
            "answer": "I deployed a Python service and measured the latency result.",
        },
        headers=headers,
    )


def test_turn_succeeds_and_releases_lock(client, real_redis_service, admin_redis):
    interview_id, headers = _interview(client)
    response = _answer(client, interview_id, headers)
    assert response.status_code == 200, response.text
    assert admin_redis.exists(_lock_key(interview_id)) == 0


def test_contended_lock_returns_conflict_without_processing(
    client, real_redis_service, admin_redis, monkeypatch
):
    interview_id, headers = _interview(client)
    holder = DistributedLock(real_redis_service, _lock_key(interview_id), ttl_seconds=30)
    assert holder.acquire()
    monkeypatch.setattr(
        "services.interview_service.settings.REDIS_LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.3
    )
    try:
        response = _answer(client, interview_id, headers)
        assert response.status_code == 409, response.text
        # The contender must not have removed the holder's lock.
        assert admin_redis.get(_lock_key(interview_id)) == holder.owner
    finally:
        assert holder.release()


def test_expired_lock_allows_next_turn(client, real_redis_service, admin_redis):
    interview_id, headers = _interview(client)
    crashed_holder = DistributedLock(real_redis_service, _lock_key(interview_id), ttl_seconds=1)
    assert crashed_holder.acquire()
    assert 0 < admin_redis.pttl(_lock_key(interview_id)) <= 1000
    time.sleep(1.3)
    response = _answer(client, interview_id, headers)
    assert response.status_code == 200, response.text
    # A stale holder cannot release the lock after expiry.
    assert crashed_holder.release() is False


def test_lock_released_after_processing_exception(
    client, real_redis_service, admin_redis, monkeypatch
):
    interview_id, headers = _interview(client)
    original = interview_service.evaluator.evaluate_result

    def fail(*_args, **_kwargs):
        raise LLMTimeoutError("synthetic")

    monkeypatch.setattr(interview_service.evaluator, "evaluate_result", fail)
    failed = _answer(client, interview_id, headers, "retry-turn")
    assert failed.status_code == 503, failed.text
    assert admin_redis.exists(_lock_key(interview_id)) == 0
    monkeypatch.setattr(interview_service.evaluator, "evaluate_result", original)
    retried = _answer(client, interview_id, headers, "retry-turn")
    assert retried.status_code == 200, retried.text


def test_unreachable_redis_fails_closed(client, monkeypatch):
    interview_id, headers = _interview(client)
    unreachable = RedisClient("redis://127.0.0.1:1/0", namespace="interview", socket_timeout=0.5)
    monkeypatch.setattr(interview_service, "redis_client", unreachable)
    response = _answer(client, interview_id, headers)
    assert response.status_code == 503, response.text


def test_service_recovers_after_server_drops_connections(
    client, real_redis_service, admin_redis
):
    interview_id, headers = _interview(client)
    assert _answer(client, interview_id, headers).status_code == 200
    # Drop every normal client connection except the admin one.
    admin_redis.execute_command("CLIENT", "KILL", "TYPE", "normal", "SKIPME", "yes")
    first = _answer(client, interview_id, headers)
    # The first call may hit the dead pooled socket: it must fail closed, never 500.
    assert first.status_code in {200, 503}, first.text
    second = _answer(client, interview_id, headers)
    assert second.status_code == 200, second.text
    assert admin_redis.exists(_lock_key(interview_id)) == 0
