# Deployment Runbook

Provider-neutral procedure for running the backend: one FastAPI API process
(or several behind a load balancer), one or more recruitment campaign worker
processes, PostgreSQL, Redis and private persistent file storage. This
document makes no Kubernetes or high-availability claims; it describes what the
code does today and what an operator must provide. Section 15 records what was
actually verified and how.

## 1. Components

| Component | Command | State |
| --- | --- | --- |
| API | `uvicorn apps.api.main:app` | Stateless apart from file storage |
| Campaign worker | `python -m services.recruitment_campaign_worker run` | Stateless; work leased from the DB |
| PostgreSQL 16 | managed or self-hosted | **System of record** - back up |
| Redis 7 | managed or self-hosted | Ephemeral (locks, rate limits, short-TTL keys) - no backup |
| File storage | `STORAGE_PATH`, `CV_STORAGE_PATH` | **Private, persistent** - back up |
| One-shot migration job | `python -m alembic upgrade head` | Runs before each rollout |

The working directory of the API must be the repository root (the app mounts
`apps/web/src` with a relative path and `STORAGE_PATH` defaults to `./storage`).
The Dockerfiles use `/app`.

## 2. Required environment variables

Secrets are shown as `<placeholder>`; inject them from a secret manager, never
bake them into images or commit them. `config/settings.py` loads environment
variables and an optional `.env` file; it **rejects unknown keys in `.env`**,
and list values must be JSON arrays.

| Variable | Production value | Notes |
| --- | --- | --- |
| `ENVIRONMENT` | `production` | Enables the startup safety checks (section 3). `development`/`test` run `create_all` + optional admin bootstrap; never use them in production. |
| `SECRET_KEY` | `<random, >= 32 chars>` | JWT signing key, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Rotating it invalidates all tokens. |
| `DATABASE_URL` | `postgresql+psycopg2://<user>:<password>@<host>:5432/<db>` | Used by API, worker and Alembic (`alembic/env.py` reads it). Add `?sslmode=require` for a remote DB. |
| `REDIS_URL` | `redis://<host>:6379/0` (`rediss://` for TLS) | The API **refuses to start** if set and unreachable (verified). Empty disables Redis (single-process only; not for multi-instance). |
| `ALLOWED_ORIGINS` | `["https://<frontend-host>"]` | JSON array. |
| `ENABLE_DEV_ADMIN_BOOTSTRAP` | `false` | Startup fails in production if `true`. |
| `STORAGE_PATH` | absolute path, e.g. `/var/lib/ai-interviewer/storage` | Report exports (`<STORAGE_PATH>/reports/<org>/<interview>/`). Relative paths resolve against the CWD. |
| `CV_STORAGE_PATH` | absolute path, e.g. `/var/lib/ai-interviewer/private_cv` | Candidate CVs. Relative paths resolve against the repository root. Must not be a symlink. |
| `STORAGE_TYPE` | `local` | Only `local` is implemented for CVs. |
| `LOG_LEVEL` | `info` | `debug` switches to plain-text logs. |
| `LLM_PROVIDER` / `EMBEDDING_PROVIDER` | `none` or `openai` | `openai` requires `OPENAI_API_KEY=<secret>`, `OPENAI_MODEL`. Provider construction refuses an incomplete selection (raised when the provider is first built, not at API startup). |
| `STT_PROVIDER` / `TTS_PROVIDER` | `fake` or real | Real voice needs `ASSEMBLY_AI_API_KEY=<secret>`, `ELEVEN_LABS_API_KEY=<secret>`, `ELEVEN_LABS_VOICE_ID`, `TTS_MODEL`. |
| `EMAIL_PROVIDER` | `offline_simulation` or `smtp` | `smtp` needs `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT` (587, STARTTLS), `EMAIL_SMTP_USERNAME`, `EMAIL_SMTP_PASSWORD=<secret>`, `EMAIL_SMTP_FROM`. |
| `CAMPAIGN_WORKER_*` | defaults | `POLL_SECONDS` 2, `CONCURRENCY` 2, `LEASE_SECONDS` 120, retry base/max 5/300 s. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | 5 / 10 | Per process. Size PostgreSQL `max_connections` for (API processes + workers) x (pool + overflow) + migration/backup headroom. |

Give the API and the worker the **same** environment (the worker also sends
email and calls the LLM provider). Verify each real provider once with
`docs/PROVIDER_SMOKE_RUNBOOK.md` before enabling it.

## 3. Production safety settings (enforced in code)

`apps/api/main.py::validate_production_security()` runs at API startup when
`ENVIRONMENT=production` and aborts startup (uvicorn exits with code 3) if:

* `SECRET_KEY` is the default placeholder or shorter than 32 characters;
* `ENABLE_DEV_ADMIN_BOOTSTRAP=true`.

Also enforced: `create_all`/admin bootstrap only run for `development`/`test`;
`services/email_provider.create_email_provider()` refuses SMTP when
`ENVIRONMENT=test`; `integrations/llm_providers/provider.validate_provider_settings`
refuses `LLM_PROVIDER=openai` without a key when the provider is built. **Not** enforced (operator
responsibility): `DEBUG=false`, restrictive `ALLOWED_ORIGINS`, TLS termination,
non-root container user. Note: the worker does not run
`validate_production_security()`.

## 4. Image build

```bash
docker build -f docker/Dockerfile.api    -t <registry>/ai-interviewer-api:<git-sha> .
docker build -f docker/Dockerfile.worker -t <registry>/ai-interviewer-worker:<git-sha> .
```

Tag images with the commit SHA, not `latest`. Before building for production:

* `.dockerignore` is present and excludes `.env` except `.env.example`, local
  databases/logs, `storage/`, `.git/`, virtual environments and
  `node_modules/`;
* both API and worker Dockerfiles run as an application UID/GID and create
  writable `/app/storage/private_cv`; `/app/storage` must remain writable by
  UID 10001 when mounted as external storage;
* image build and image-inspection evidence is pending because Docker Desktop
  has no running daemon in this environment.

`docker-compose.yml` is a **development** stack (bind mount `.:/app`,
`--reload`, hard-coded dev DB password, `pgvector/pgvector:pg15` while CI tests
PostgreSQL 16, no migration service). Do not use it as a production manifest.

## 5. Migration procedure

Migrations are Alembic revisions in `alembic/versions` with a single head
(CI asserts exactly one head).

1. **Back up** (section 10) and record the current revision:
   `python -m alembic current`.
2. Run the migration as a **one-shot job** using the *new* image, before any
   new API/worker starts:
   ```bash
   docker run --rm --env-file <prod-env-file> <registry>/ai-interviewer-api:<sha> \
     python -m alembic upgrade head
   python -m alembic current          # must print "<rev> (head)"
   ```
   Run exactly one migration job at a time.
3. Roll out the worker(s) and API(s) (section 6/7).

Migrations are written to be additive within a release so the old code keeps
working against the new schema during rollout; review each revision for
destructive steps before running it.

**Rollback strategy.** Prefer **forward-fix** (a new revision/release). If the
data or schema must be reverted, **restore the pre-migration backup** (section
10). `alembic downgrade <rev>` exists for many revisions but is a last resort:
downgrades are not exercised in CI, can drop columns/tables and therefore data
written after the upgrade, and some may not be reversible. Never run a
downgrade against production without first testing it on a restored copy.

## 6. API

```bash
uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips '<load-balancer-ip>' --timeout-graceful-shutdown 30
```

Scale with more processes/instances behind a load balancer rather than many
`--workers` in one container (one process per container keeps signals and
health checks simple). WebSocket interviews are long-lived connections; the
load balancer needs WebSocket support and idle timeouts above
`VOICE_WS_IDLE_TIMEOUT_SECONDS` (300 s).

### Probes

| Endpoint | Use | Behavior |
| --- | --- | --- |
| `GET /health/live` (alias `/liveness`) | Liveness | 200 `{"alive": true}` while the process serves requests; no dependency checks. |
| `GET /health/ready` (alias `/readiness`) | Readiness | 200 `{"ready": true, "checks": {"database": true, "redis": true}}`; 503 with the failing check otherwise. Opens a new DB engine and Redis client per call - probe every 10-15 s, not faster. |
| `GET /health` | Info | 200 with version and environment. |

Suggested: liveness every 10 s, timeout 5 s, 3 failures; readiness every 10 s,
timeout 5 s; start probing after ~20 s (startup imports take a few seconds).

### Graceful shutdown

On SIGTERM/SIGINT (and CTRL_BREAK on Windows) uvicorn stops accepting
connections, waits for in-flight requests (bounded by
`--timeout-graceful-shutdown`), runs the lifespan shutdown (closes the Redis
client, logs `Shutting down AI Interviewer`) and exits 0. Open WebSocket
interviews are closed; clients must reconnect to another instance. Interview turn processing is guarded by a
Redis lock and a database processing lease (`TURN_PROCESSING_LEASE_SECONDS`,
default 300 s; see `services/interview_service.py`). Give the container a stop grace period
>= 35 s.

## 7. Campaign worker

```bash
python -m services.recruitment_campaign_worker run      # long-running dispatcher
python -m services.recruitment_campaign_worker status   # JSON; exit 0 only if healthy AND not paused
python -m services.recruitment_campaign_worker pause    # cluster-wide: stops new dispatch
python -m services.recruitment_campaign_worker resume
```

* **Heartbeat.** Each loop iteration writes `heartbeat_at` to the single
  `recruitment_campaign_worker_control` row. `status` reports `healthy` if the
  heartbeat is newer than `max(3 x CAMPAIGN_WORKER_POLL_SECONDS, 10 s)`. With
  several workers the row reflects whichever wrote last.
* **Pause/resume** is a DB flag shared by all workers; paused workers keep
  heartbeating but dispatch nothing. `status` exits 1 while paused, so a
  container health check based on `status` marks paused workers unhealthy -
  expected during maintenance; do not let the orchestrator restart-loop them
  (use the health check for alerting, not for restarts, or accept it).
* **Shutdown.** SIGTERM/SIGINT set a stop flag: no new work is claimed, and
  the process waits for **all in-flight items to finish** before exiting.
  An item can include an LLM call or SMTP send, so allow a stop grace period of
  at least `LLM_TIMEOUT_SECONDS` + `EMAIL_SMTP_TIMEOUT_SECONDS` + a margin
  (suggest 60-120 s). There is no SIGBREAK handler on Windows.
* **Restart / crash behavior.** Work items (campaign executions, applications,
  communications) are claimed with a `lease_token` and `lease_expires_at`
  (`CAMPAIGN_WORKER_LEASE_SECONDS`, renewed every lease/3 while running). If a
  worker is killed, its items become claimable again after the lease expires;
  the final write is **fenced** by the lease token so a stale worker cannot
  overwrite the new owner's result. This is at-least-once, not exactly-once:
  an SMTP send interrupted after DATA is recorded as `unknown` and is not
  blindly retried. `status` shows `expired_leases` / `*_retryable_due` counts.
* Multiple workers are safe (verified in `tests/postgres` multi-worker suites
  on PostgreSQL); SQLite is development-only.

## 8. Logs

API application logs are JSON lines on stdout (`utils/logger.py`), e.g.
`{"message": "Starting AI Interviewer", "environment": "production", "version": "0.1.0", "timestamp": "...", "level": "INFO", "logger": "apps.api.main"}`
with `request_id` on request-scoped records. Uvicorn's own startup and access
lines are plain text (`INFO:     127.0.0.1:50973 - "GET /health HTTP/1.1" 200 OK`).
`LOG_LEVEL=debug` switches application logs to plain text. The worker CLI logs
plain text (`%(asctime)s %(levelname)s %(name)s %(message)s`). Errors log
`failure_type` (exception class) rather than messages. Ship stdout to your log
platform; do not log request bodies.

## 9. Resource limits (starting points - measure and adjust)

| Process | CPU | Memory | Notes |
| --- | --- | --- | --- |
| API (1 uvicorn process) | 0.5-1 vCPU | 1 GiB request / 1.5 GiB limit | Imports LangChain/LangGraph; idle RSS is several hundred MB. |
| Worker (concurrency 2) | 0.25-0.5 vCPU | 512 MiB-1 GiB | Mostly I/O-bound (LLM/SMTP). |
| Migration job | 0.25 vCPU | 512 MiB | |
| PostgreSQL | per provider sizing | | `max_connections` per section 2. |
| Redis | small | 256 MiB, `maxmemory-policy noeviction` | Evicting lock keys would break mutual exclusion. |

## 10. Persistent private file storage

* Mount `STORAGE_PATH` and `CV_STORAGE_PATH` on persistent, **private**
  volumes (not web-served, not world-readable; the CV directory is chmod 700
  on POSIX). Encrypt at rest.
* Multiple API instances must share the same storage (a shared filesystem) or
  CV downloads/report exports/erasure will miss files written by another
  instance. Object storage is not implemented.
* Candidate erasure deletes CV files and report exports
  (`services/candidate_data_deletion.py`), so storage must be writable by the
  API.

## 11. Backup and restore

### PostgreSQL

```bash
# Backup (custom format, compressed, restorable selectively)
PGPASSWORD=<password> pg_dump --format=custom --no-owner \
  --host <host> --port 5432 --username <user> --dbname <db> \
  --file ai_interviewer_$(date -u +%Y%m%dT%H%M%SZ).dump

# Restore into a NEW, EMPTY database, then point the app at it
createdb --host <host> --username <user> <new_db>
PGPASSWORD=<password> pg_restore --no-owner --no-privileges --exit-on-error \
  --host <host> --port 5432 --username <user> --dbname <new_db> <file>.dump
DATABASE_URL=postgresql+psycopg2://<user>:<password>@<host>:5432/<new_db> python -m alembic current
```

Use the same major version of `pg_dump` as the server (or newer). Stop or
pause writers (`recruitment_campaign_worker pause`, API maintenance mode or
scale to 0) for a fully quiescent backup; `pg_dump` is transactionally
consistent on its own. Managed services' point-in-time recovery can replace
this; still test restores.

### Files

Back up `CV_STORAGE_PATH` and `STORAGE_PATH/reports` (and legacy
`STORAGE_PATH/report_*.{json,txt}`) with a filesystem/volume snapshot or
`tar --create --gzip --file storage_<ts>.tgz -C <parent> private_cv storage`.

**Consistency.** DB rows reference CV storage keys and export files. Take the
DB backup and the file snapshot close together (pause the worker and API
writes if you need exact agreement). After a restore, a file without a row is
an orphan to delete; a row without a file means a missing CV/export.

**Erasure and backups.** Candidate erasure removes data from the live DB and
storage but **not from existing backups**. Define a backup retention period,
document it in the privacy notice, and on restore re-apply erasures performed
after the backup was taken (the `candidate_data_deletion` audit rows record
them) before reopening the system.

### Redis

Redis holds only ephemeral coordination state (distributed locks, rate-limit
counters, short-TTL keys). **No backup is needed**; after a Redis loss, locks
and counters reset. Do not enable eviction (section 9).

## 12. Disposable Redis test environment

Real Redis 7 for the Redis suites (never point these at a shared Redis; the
suites write keys to the database in REDIS_INTEGRATION_URL):

```bash
docker run --rm --name ai-int-redis-test -p 127.0.0.1:6379:6379 redis:7-alpine
# second shell, from the repository root:
export DATABASE_URL=sqlite:///./storage/test_redis_suite.db ENVIRONMENT=test \
       ENABLE_DEV_ADMIN_BOOTSTRAP=false LLM_PROVIDER=none EMBEDDING_PROVIDER=none
python -m alembic upgrade head
REDIS_INTEGRATION_URL=redis://127.0.0.1:6379/1 \
  python -m pytest tests/redis tests/isolated/test_distributed_lock_verification.py --timeout=120 -ra
# stop: Ctrl+C in the first shell (--rm removes the container); rm storage/test_redis_suite.db
```

## 13. Disposable PostgreSQL test environment

```bash
docker run --rm --name ai-int-pg-test -p 127.0.0.1:5432:5432 \
  -e POSTGRES_PASSWORD=disposable-test-only -e POSTGRES_DB=test_local postgres:16-alpine
export DATABASE_URL=postgresql+psycopg2://postgres:disposable-test-only@127.0.0.1:5432/test_local
export RUN_POSTGRES_TESTS=1 ENVIRONMENT=test ENABLE_DEV_ADMIN_BOOTSTRAP=false \
       LLM_PROVIDER=none EMBEDDING_PROVIDER=none REDIS_URL=redis://127.0.0.1:6379/0
python -m alembic upgrade head && python -m alembic current
python -m pytest tests/postgres -m "not live" --timeout=300 -ra
```

`tests/conftest.py` refuses PostgreSQL targets that are not loopback, not
named `test_*`, or lack `RUN_POSTGRES_TESTS=1`.

### Backup/restore rehearsal (disposable data only)

`tools/backup_restore_check.py` dumps a source DB with `pg_dump -Fc`, restores
into an empty target with `pg_restore`, and compares per-table row counts and
the Alembic revision. It refuses non-loopback hosts, database names not
starting with `test_`, identical source/target and non-empty targets.

Plan:
1. Create `test_backup_src` and `test_backup_dst` on the disposable server
   (`createdb -h 127.0.0.1 -U postgres test_backup_src`, same for `_dst`).
2. `DATABASE_URL=<src url> python -m alembic upgrade head`.
3. Create disposable rows (e.g. start the API against the source in
   `ENVIRONMENT=staging` and register test users with `@example.invalid`
   addresses, or run the worker once to create its control row).
4. `python tools/backup_restore_check.py --source <src url> --target <dst url> [--pg-bin <dir>]`
   - expect `"ok": true`, `"mismatches": {}`, equal `alembic_revision`.
5. Point the API at the target and check `/health/ready` and a login.
6. Rehearse the file side: snapshot a test `CV_STORAGE_PATH`, restore it to a
   new directory and compare file counts/checksums with `storage_key` values.
7. Drop both databases / stop the container.

## 14. Triggering GitHub CI (OWNER ACTIONS ONLY)

The steps below push code and must only be run by the repository owner after
they have reviewed and authorized the change. Automation and assistants must
not run them.

```bash
git switch main && git pull --ff-only
git switch -c chore/deployment-runbook-<yyyymmdd>
git add docs/DEPLOYMENT_RUNBOOK.md docs/PROVIDER_SMOKE_RUNBOOK.md tests/live \
        tests/unit/test_provider_smoke_harness.py tools/backup_restore_check.py \
        .github/workflows/test.yml docker-compose.yml docker/Dockerfile.api .env.example
git status && git diff --cached --stat      # review exactly what will be pushed
git commit -m "Add provider smoke tests, deployment runbook and backup/restore check"
git push -u origin chore/deployment-runbook-<yyyymmdd>
gh pr create --base main --fill            # or open the PR in the GitHub UI
gh pr checks --watch
```

Expected jobs on the PR (workflow `Backend CI`): `Ruff, whitespace, timeout
contracts`; `test-groups` matrix of 7 groups x Python 3.11/3.12
(`fast-unit`, `api-security-report`, `database-concurrency`, `websocket-voice`,
`rag`, `campaign-workflow`, `security-regression`); `PostgreSQL 16 + Redis 7
integration`; `Real Redis 7 integration`. `Optional live AI verification` runs
only via manual `workflow_dispatch` with both confirmation inputs and is
skipped on PRs.

Reading failures: open the failed job, expand the failing step. `Every unit
test file is run by a CI job` means a new `tests/unit/test_*.py` is not listed
in `.github/workflows/test.yml`; `Fail if ... skipped` means a PostgreSQL/Redis
suite skipped instead of running (service container or env problem);
`Check changed-line whitespace` lists files/lines with trailing whitespace.
`gh run view <run-id> --log-failed` prints only failing steps.

## 15. Verification performed

Performed 2026-11-24 on **local Windows 11, Python 3.11 venv, embedded
PostgreSQL 16.2 (the `pgserver` package), and a fakeredis-based Redis
stand-in listening on 127.0.0.1:6379 - not real Redis**. Docker is not
available on that machine, so **no image was built and no container, compose
stack or real Redis was run**. No provider was called.

| Check | Command (abridged) | Result |
| --- | --- | --- |
| Migrations on fresh PG 16.2 DB `test_m13i_deploy` | `python -m alembic upgrade head`; `alembic current`; `alembic heads` | Upgraded through `g21_candidate_legal_hold`; current = `g21_candidate_legal_hold (head)`; single head |
| API start (production mode, random 64-char `SECRET_KEY`, PG, Redis stand-in) | `python -m uvicorn apps.api.main:app --host 127.0.0.1 --port <free>` | `/health` 200 (`environment: production`), `/health/live` 200, `/health/ready` 200 `{"database": true, "redis": true}`, `/readiness` 200, `/liveness` 200 |
| Graceful API shutdown | CTRL_BREAK_EVENT to the uvicorn process group | Log: `Shutting down` -> `Waiting for application shutdown.` -> JSON `Shutting down AI Interviewer` -> `Application shutdown complete.` -> `Finished server process`; exit 0 after 0.5 s |
| Production secret check | same, `ENVIRONMENT=production`, no `SECRET_KEY` | `RuntimeError: Production requires an explicitly configured SECRET_KEY of at least 32 characters`; `Application startup failed. Exiting.`; exit 3 |
| Redis unreachable | same, `REDIS_URL=redis://127.0.0.1:6390/0` | `redis.exceptions.TimeoutError`; `Application startup failed. Exiting.` (API fails fast) |
| Worker status before first run | `python -m services.recruitment_campaign_worker status` | exit 1, `healthy=false`, `heartbeat_at=null` |
| Worker run + stop | real `main(["run"])` with SIGINT raised in-process after 5 s (Windows cannot deliver SIGTERM to another process) | Logs `worker starting` -> `received shutdown signal` -> `stopping; waiting for active executions` -> `stopped`; exit 0 |
| Heartbeat | `status` right after run | exit 0, `healthy=true`, `ready=true`, `heartbeat_at` set, `last_error=null` |
| Pause / resume | `pause`, `status`, `resume`, `status` | `paused=true` -> status exit 1; after `resume` `paused=false` (status exit 1 because no worker was heartbeating - expected) |
| Backup/restore | seeded 3 disposable `@example.invalid` users; `python tools/backup_restore_check.py --source <test_m13i_deploy> --target <test_m13i_restore> --pg-bin <pgserver>/pginstall/bin` | exit 0; 34 tables, 6 rows source = 6 rows target (`users` 3, `alembic_version`, `recruitment_campaign_worker_control`, `interview_schedule_mutex` 1 each); revision equal; dump 140,653 bytes |
| Tool refusals | re-run into non-empty target; non-`test_` DB name; non-loopback host | each exit 2 with `refused: ...` |
| `.env.example` loads | `Settings(_env_file=".env.example")` | Before fix: `SettingsError` (ALLOWED_ORIGINS) and 51 `extra_forbidden` errors; after fix: loads |
| Cleanup | `DROP DATABASE ... WITH (FORCE)` for both test DBs | no `test_m13i_*` databases remain |

Not verified: container images, `docker compose`, real Redis, TLS, multiple
API instances, shared storage, restore of file storage, provider calls,
behavior under load.

### P14.1 verification update

The later offline P14.1 unit run completed with 600 passed and 4 deselected
using the project virtualenv and a disposable SQLite database. This does not
supersede the infrastructure gaps above. The available local Redis was
3.0.504, below the integration suite's Redis 6+ requirement. The P13 baseline
Actions run passed its PostgreSQL and Redis 7 jobs but failed overall; it does
not verify the uncommitted P14.1 changes. Docker remains unavailable, so no
image build or inspection was completed. See
[`P14_1_STABILIZATION_REPORT.md`](./P14_1_STABILIZATION_REPORT.md) for exact
commands and results.

## 16. Known deployment gaps (P14.1)

* `.dockerignore` is now present and both Dockerfiles configure UID/GID 10001,
  but image builds, image contents, runtime user and storage permissions have
  not been verified because Docker is unavailable.
* `docker-compose.yml` is dev-only: no migration service, worker lacks
  `REDIS_URL`/`ENVIRONMENT`/provider settings, optional profiles reference
  missing files (`docker/prometheus.yml`, `apps/web/Dockerfile`).
* No object-storage backend; multi-instance API needs a shared filesystem.
* Worker does not run the production security validation.
* Legacy `@app.on_event` startup/shutdown handlers in `apps/api/main.py` do
  not run because a lifespan is configured (their log lines never appear;
  harmless).
