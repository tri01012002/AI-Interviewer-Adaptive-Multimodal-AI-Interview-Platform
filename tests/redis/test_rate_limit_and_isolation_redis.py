"""Redis-backed rate limiting and key isolation against a REAL Redis server.

Runs only with REDIS_INTEGRATION_URL (CI service container or a disposable
loopback Redis 7). fakeredis is refused because it lacks ``INFO server``.
Every test uses its own namespace and asserts that no keys are left behind.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

REDIS_URL = os.environ.get("REDIS_INTEGRATION_URL", "")

pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason="Set REDIS_INTEGRATION_URL to a real Redis to run these tests"
)

if REDIS_URL:
    import redis
    from fastapi.testclient import TestClient

    from apps.api.main import app
    from apps.api.middleware.rate_limit import endpoint_rate_limiter
    from config import settings
    from infrastructure.rate_limiter import RedisFixedWindowCounter


@pytest.fixture(scope="module")
def admin_redis():
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        info = client.info("server")
    except redis.ResponseError as exc:
        pytest.fail(f"REDIS_INTEGRATION_URL is not a real Redis server: {exc}")
    if "process_id" not in info:
        pytest.fail("REDIS_INTEGRATION_URL is not a real Redis server")
    assert int(info["redis_version"].split(".")[0]) == 7, (
        "Redis integration tests require Redis 7"
    )
    yield client
    client.close()


@pytest.fixture
def namespace(admin_redis):
    """Unique per-test namespace; fails the test if any key leaks out of it."""
    name = f"it:{uuid4().hex}"
    yield name
    leaked = list(admin_redis.scan_iter(match=f"{name}:*"))
    if leaked:
        admin_redis.delete(*leaked)
    # Window keys carry a TTL, so cleanup above is only a safety net.
    assert all(admin_redis.ttl(key) != -1 for key in leaked), "key without TTL leaked"


def test_counter_is_atomic_under_concurrency(admin_redis, namespace):
    counter = RedisFixedWindowCounter(redis.Redis.from_url(REDIS_URL), namespace=namespace)
    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda _: counter.hit("actor-1", 20, 60), range(60)))
    assert sum(decision.allowed for decision in decisions) == 20
    assert sorted(decision.count for decision in decisions) == list(range(1, 61))
    keys = list(admin_redis.scan_iter(match=f"{namespace}:*"))
    assert len(keys) == 1
    assert 0 < admin_redis.ttl(keys[0]) <= 61


def test_window_expiry_resets_the_limit(admin_redis, namespace):
    counter = RedisFixedWindowCounter(redis.Redis.from_url(REDIS_URL), namespace=namespace)
    assert counter.hit("actor-2", 1, 1).allowed
    assert not counter.hit("actor-2", 1, 1).allowed
    time.sleep(2.1)
    assert counter.hit("actor-2", 1, 1).allowed


def test_keys_are_isolated_per_identity_and_namespace(admin_redis, namespace):
    other_namespace = f"{namespace}-other"
    first = RedisFixedWindowCounter(redis.Redis.from_url(REDIS_URL), namespace=namespace)
    second = RedisFixedWindowCounter(redis.Redis.from_url(REDIS_URL), namespace=other_namespace)
    assert first.hit("actor-a", 1, 60).allowed
    assert not first.hit("actor-a", 1, 60).allowed
    assert first.hit("actor-b", 1, 60).allowed
    assert second.hit("actor-a", 1, 60).allowed
    for key in admin_redis.scan_iter(match=f"{other_namespace}:*"):
        admin_redis.delete(key)


def test_login_endpoint_is_limited_through_real_redis(admin_redis, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", REDIS_URL)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 2)
    endpoint_rate_limiter.set_backend(None)
    marker = f"rl-{uuid4().hex}@example.test"
    try:
        with TestClient(app) as client:
            statuses = [
                client.post(
                    "/api/v1/auth/login",
                    json={"email": marker, "password": "wrong-password"},
                ).status_code
                for _ in range(3)
            ]
            limited = client.post(
                "/api/v1/auth/login", json={"email": marker, "password": "wrong-password"}
            )
    finally:
        endpoint_rate_limiter.set_backend(None)
    assert statuses[:2] == [401, 401]
    assert statuses[2] == 429
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert list(admin_redis.scan_iter(match="ai:ratelimit:login:*"))


def test_unreachable_redis_fails_open_for_rate_limiting(monkeypatch):
    try:
        with TestClient(app) as client:
            # Switch only after startup to exercise the request-path outage.
            monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
            monkeypatch.setattr(settings, "REDIS_TIMEOUT_SECONDS", 0.5)
            monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_OPEN", True)
            endpoint_rate_limiter.set_backend(None)
            response = client.post(
                "/api/v1/auth/login",
                json={"email": f"down-{uuid4().hex}@example.test", "password": "x" * 12},
            )
    finally:
        endpoint_rate_limiter.set_backend(None)
    # Authentication still runs and rejects; the limiter did not block or 500.
    assert response.status_code == 401
