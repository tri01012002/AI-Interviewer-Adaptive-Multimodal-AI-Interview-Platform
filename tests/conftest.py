"""Safety checks preventing pytest from using the application database."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from tempfile import gettempdir

import pytest
from sqlalchemy.engine import make_url


def _validate_local_database_path(database: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    storage_path = project_root / "storage"
    database_path = Path(os.path.abspath(database))
    if storage_path.resolve(strict=False) != storage_path:
        raise pytest.UsageError("The test storage directory must not be a symlink.")
    if (
        database_path.parent != storage_path
        or database_path.is_symlink()
        or database_path.resolve(strict=False) != database_path
    ):
        raise pytest.UsageError(
            "Test DATABASE_URL must resolve directly to a file under repository storage."
        )


def _validate_database_path(database: str) -> None:
    path = PurePosixPath(database.replace("\\", "/"))
    if ".." in path.parts:
        raise pytest.UsageError("Test DATABASE_URL must not traverse outside its test directory.")

    if path.is_absolute() or Path(database).is_absolute():
        if not path.name.startswith("live_ai_e2e_"):
            raise pytest.UsageError(
                "Absolute test database paths are allowed only for dedicated live_ai_e2e_ files."
            )
        live_database_url = os.environ.get("LIVE_AI_E2E_DATABASE_URL")
        if (
            os.environ.get("RUN_LIVE_AI_E2E") != "1"
            or not live_database_url
            or live_database_url != os.environ.get("DATABASE_URL")
        ):
            raise pytest.UsageError(
                "Absolute test database paths require the explicit live E2E opt-in "
                "and matching LIVE_AI_E2E_DATABASE_URL."
            )
        live_path = Path(database)
        temporary_root = Path(os.environ.get("RUNNER_TEMP", gettempdir())).resolve()
        if (
            not live_path.is_absolute()
            or live_path.name != path.name
            or live_path.parent.resolve(strict=False) != temporary_root
            or live_path.is_symlink()
            or live_path.resolve(strict=False) != live_path
            or live_path.exists()
        ):
            raise pytest.UsageError(
                "Live E2E database must be a fresh, non-symlink file directly under "
                "the runner's temporary directory."
            )
        return

    if (
        len(path.parts) != 2
        or path.parts[0] != "storage"
        or not path.name.startswith(("ci_", "test_"))
        or not path.name.endswith(".db")
    ):
        raise pytest.UsageError(
            "Use a fresh SQLite file under storage named ci_*.db or test_*.db."
        )
    _validate_local_database_path(database)


def pytest_configure() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise pytest.UsageError(
            "Set DATABASE_URL to an isolated SQLite test database before running pytest."
        )

    if os.environ.get("ENVIRONMENT", "").lower() not in {"test", "testing"}:
        raise pytest.UsageError("Set ENVIRONMENT=test before running pytest.")

    try:
        url = make_url(database_url)
    except Exception as exc:
        raise pytest.UsageError("DATABASE_URL is not a valid SQLAlchemy URL.") from exc

    if url.drivername.startswith("postgresql"):
        _validate_disposable_postgres(url)
        return
    if url.drivername != "sqlite" or not url.database:
        raise pytest.UsageError("Pytest requires an isolated SQLite DATABASE_URL.")
    _validate_database_path(url.database)


def _validate_disposable_postgres(url) -> None:
    if os.environ.get("RUN_POSTGRES_TESTS") != "1":
        raise pytest.UsageError(
            "PostgreSQL test runs require the explicit RUN_POSTGRES_TESTS=1 opt-in."
        )
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise pytest.UsageError(
            "PostgreSQL tests may only use a disposable server on the loopback interface."
        )
    if not url.database or not url.database.startswith("test_"):
        raise pytest.UsageError(
            "PostgreSQL test database names must start with test_ (disposable databases only)."
        )
