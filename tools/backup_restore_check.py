"""Disposable PostgreSQL backup/restore round-trip check (pg_dump -Fc -> pg_restore).

Dumps SOURCE with ``pg_dump --format=custom``, restores into the EMPTY database
TARGET with ``pg_restore``, then compares per-table row counts and the Alembic
revision. Exit code 0 means every table matched.

Safety: both URLs must be PostgreSQL on a loopback host (127.0.0.1, localhost,
::1) with a database name starting with ``test_``; the target must be a
different, empty database. Passwords are passed via PGPASSWORD and never
printed. This tool is for rehearsing the procedure on disposable data, not for
production backups (see docs/DEPLOYMENT_RUNBOOK.md for those commands).

Usage:
    python tools/backup_restore_check.py --source URL --target URL \
        [--pg-bin DIR] [--dump-file PATH] [--keep-dump]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class UnsafeTargetError(ValueError):
    """Raised when a URL is not a disposable loopback test_ database."""


def validate_disposable_url(raw: str, role: str) -> URL:
    try:
        url = make_url(raw)
    except Exception as exc:  # noqa: BLE001 - never echo the raw URL (may hold a password)
        raise UnsafeTargetError(f"{role} is not a valid database URL") from exc
    if not url.drivername.startswith("postgresql"):
        raise UnsafeTargetError(f"{role} must be a PostgreSQL URL")
    if (url.host or "") not in LOOPBACK_HOSTS:
        raise UnsafeTargetError(f"{role} host must be loopback (127.0.0.1/localhost/::1)")
    if not (url.database or "").startswith("test_"):
        raise UnsafeTargetError(f"{role} database name must start with test_")
    return url


def _find_binary(name: str, pg_bin: str | None) -> str:
    candidates = []
    for directory in (pg_bin, os.environ.get("PG_BIN")):
        if directory:
            candidates += [Path(directory) / name, Path(directory) / f"{name}.exe"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} not found; pass --pg-bin or set PG_BIN")
    return found


def _conn_args(url: URL) -> list[str]:
    args = ["--host", url.host or "127.0.0.1", "--dbname", url.database or ""]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    return args


def _env(url: URL) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PGPASSWORD", None)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return env


def _engine(url: URL):
    return create_engine(url.set(drivername="postgresql+psycopg2"), pool_pre_ping=True)


def table_counts(url: URL) -> dict[str, int]:
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            names = sorted(inspect(conn).get_table_names(schema="public"))
            counts = {}
            for name in names:
                quoted = conn.dialect.identifier_preparer.quote(name)
                counts[name] = int(conn.execute(text(f"SELECT count(*) FROM public.{quoted}")).scalar_one())
            return counts
    finally:
        engine.dispose()


def alembic_revision(url: URL) -> str | None:
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version", schema="public"):
                return None
            return conn.execute(text("SELECT version_num FROM public.alembic_version")).scalar()
    finally:
        engine.dispose()


def _run(cmd: list[str], env: dict[str, str]) -> None:
    completed = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if completed.returncode != 0:
        # pg tools do not print passwords; still trim the output.
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({completed.returncode}): {completed.stderr[-800:]}")


def run_check(source: URL, target: URL, pg_bin: str | None, dump_file: Path) -> dict[str, object]:
    if (source.host, source.port, source.database) == (target.host, target.port, target.database):
        raise UnsafeTargetError("source and target must be different databases")
    if table_counts(target):
        raise UnsafeTargetError("target database must be empty (no tables in schema public)")

    pg_dump = _find_binary("pg_dump", pg_bin)
    pg_restore = _find_binary("pg_restore", pg_bin)
    _run([pg_dump, "--format=custom", "--no-owner", "--file", str(dump_file), *_conn_args(source)], _env(source))
    _run(
        [pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", *_conn_args(target), str(dump_file)],
        _env(target),
    )

    source_counts = table_counts(source)
    target_counts = table_counts(target)
    mismatches = {
        name: {"source": source_counts.get(name), "target": target_counts.get(name)}
        for name in sorted(set(source_counts) | set(target_counts))
        if source_counts.get(name) != target_counts.get(name)
    }
    source_revision = alembic_revision(source)
    target_revision = alembic_revision(target)
    return {
        "dump_bytes": dump_file.stat().st_size,
        "tables": len(source_counts),
        "source_rows": sum(source_counts.values()),
        "target_rows": sum(target_counts.values()),
        "non_empty_tables": {k: v for k, v in source_counts.items() if v},
        "mismatches": mismatches,
        "alembic_revision": {"source": source_revision, "target": target_revision},
        "ok": not mismatches and source_revision == target_revision,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="disposable source DB URL (test_* on loopback)")
    parser.add_argument("--target", required=True, help="disposable EMPTY target DB URL (test_* on loopback)")
    parser.add_argument("--pg-bin", help="directory containing pg_dump/pg_restore")
    parser.add_argument("--dump-file", help="where to write the custom-format dump")
    parser.add_argument("--keep-dump", action="store_true", help="keep the dump file afterwards")
    args = parser.parse_args(argv)
    try:
        source = validate_disposable_url(args.source, "--source")
        target = validate_disposable_url(args.target, "--target")
    except UnsafeTargetError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    temp_dir = None
    if args.dump_file:
        dump_file = Path(args.dump_file)
    else:
        temp_dir = tempfile.mkdtemp(prefix="backup_restore_check_")
        dump_file = Path(temp_dir) / "source.dump"
    try:
        report = run_check(source, target, args.pg_bin, dump_file)
    except UnsafeTargetError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_dump:
            if dump_file.exists():
                dump_file.unlink()
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
