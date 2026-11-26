import asyncio
import json

from apps.api import main
from config import settings


def test_readiness_reports_unavailable_dependencies_with_http_503(monkeypatch):
    class RedisProbe:
        def ping(self):
            return True

        def close(self):
            pass

    def fail_database(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    class DatabaseProbe:
        def connect(self):
            return fail_database()

    monkeypatch.setattr(main, "engine", DatabaseProbe())
    monkeypatch.setattr(main.app.state, "redis_client", RedisProbe(), raising=False)

    response = asyncio.run(main.health_ready())

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "ready": False,
        "checks": {"database": False, "redis": True},
    }


def test_readiness_reuses_application_dependency_clients(monkeypatch):
    class DatabaseConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def execute(self, _statement):
            return 1

    class DatabaseProbe:
        def connect(self):
            return DatabaseConnection()

    class RedisProbe:
        pinged = False

        def ping(self):
            self.pinged = True
            return True

    database = DatabaseProbe()
    redis_client = RedisProbe()
    monkeypatch.setattr(main, "engine", database)
    monkeypatch.setattr(main.app.state, "redis_client", redis_client, raising=False)

    response = asyncio.run(main.health_ready())

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "ready": True,
        "checks": {"database": True, "redis": True},
    }
    assert redis_client.pinged


def test_readiness_reports_database_query_failure(monkeypatch):
    class DatabaseProbe:
        def connect(self):
            raise RuntimeError("database query failed")

        def dispose(self):
            self.disposed = True

    class RedisProbe:
        def ping(self):
            return True

    monkeypatch.setattr(main, "engine", DatabaseProbe())
    monkeypatch.setattr(main.app.state, "redis_client", RedisProbe(), raising=False)

    response = asyncio.run(main.health_ready())

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "ready": False,
        "checks": {"database": False, "redis": True},
    }


def test_api_startup_does_not_block_when_redis_is_unavailable(monkeypatch):
    class RedisProbe:
        closed = False

        def ping(self):
            raise RuntimeError("Redis unavailable")

        def close(self):
            self.closed = True

    client = RedisProbe()
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "ENABLE_DEV_ADMIN_BOOTSTRAP", False)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "RedisClient", lambda *args, **kwargs: client)

    async def start_and_stop():
        async with main.lifespan(main.app):
            assert not client.closed

    asyncio.run(start_and_stop())

    assert client.closed
