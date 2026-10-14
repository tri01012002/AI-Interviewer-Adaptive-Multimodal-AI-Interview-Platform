import asyncio
import json

import redis
import sqlalchemy

from apps.api import main


def test_readiness_reports_unavailable_dependencies_with_http_503(monkeypatch):
    class RedisProbe:
        def ping(self):
            return True

        def close(self):
            pass

    def fail_database(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(sqlalchemy, "create_engine", fail_database)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: RedisProbe())

    response = asyncio.run(main.health_ready())

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "ready": False,
        "checks": {"database": False, "redis": True},
    }


def test_readiness_disposes_database_and_redis_probes(monkeypatch):
    class DatabaseConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def execute(self, _statement):
            return 1

    class DatabaseProbe:
        disposed = False

        def connect(self):
            return DatabaseConnection()

        def dispose(self):
            self.disposed = True

    class RedisProbe:
        closed = False

        def ping(self):
            return True

        def close(self):
            self.closed = True

    database = DatabaseProbe()
    redis_client = RedisProbe()
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: redis_client)

    response = asyncio.run(main.health_ready())

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "ready": True,
        "checks": {"database": True, "redis": True},
    }
    assert database.disposed
    assert redis_client.closed


def test_readiness_disposes_database_when_query_fails(monkeypatch):
    class DatabaseProbe:
        disposed = False

        def connect(self):
            raise RuntimeError("database query failed")

        def dispose(self):
            self.disposed = True

    class RedisProbe:
        def ping(self):
            return True

        def close(self):
            pass

    database = DatabaseProbe()
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: RedisProbe())

    response = asyncio.run(main.health_ready())

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "ready": False,
        "checks": {"database": False, "redis": True},
    }
    assert database.disposed
