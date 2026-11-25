# Customer Acceptance Checklist — P14.1

Use only these labels:

- **VERIFIED** — demonstrated with actual local/infra evidence
- **TESTED_OFFLINE** — deterministic/offline tests only
- **IMPLEMENTED_NOT_VERIFIED** — code exists, required evidence is absent
- **BLOCKED** — cannot proceed in this environment
- **REQUIRES_CUSTOMER_DECISION** — customer/product/legal decision missing
- **NOT_IMPLEMENTED** — absent

## Git and release

| Item | Status | Evidence/action |
|---|---|---|
| HEAD and origin/main are P13 commit | VERIFIED | `71a3314edd7962ed0f6629291b403c5e6ef2ff37` |
| Working tree baseline was clean | VERIFIED | P14 initial audit |
| Current P14/P14.1 work preserved and uncommitted on main | VERIFIED | No branch, commit or push |
| account-linking and frontend untouched | VERIFIED | File diff audit |

## Docker and infrastructure

| Item | Status | Evidence/action |
|---|---|---|
| `.dockerignore` present | VERIFIED | Excludes secrets, storage, git, virtualenv, node_modules, local DB/log/test artifacts |
| `.env.example` retained | IMPLEMENTED_NOT_VERIFIED | Explicit exception in `.dockerignore`; image build pending |
| API Dockerfile non-root | IMPLEMENTED_NOT_VERIFIED | UID/GID 10001 defined; runtime not built |
| Worker Dockerfile non-root | IMPLEMENTED_NOT_VERIFIED | UID/GID 10001 defined; runtime not built |
| Storage writable by non-root | IMPLEMENTED_NOT_VERIFIED | `/app/storage/private_cv` chowned; external mount permissions untested |
| Images build/inspect safely | BLOCKED | Docker daemon unavailable at `npipe:////./pipe/dockerDesktopLinuxEngine` |
| PostgreSQL integration on P14.1 changes | BLOCKED | No disposable PostgreSQL service available locally |
| Redis 7 integration on P14.1 changes | BLOCKED | Local Redis is 3.0.504; suite requires Redis 6+ |
| GitHub Actions on P14.1 changes | BLOCKED | No current-branch run; P13 baseline run failed overall |

## Business workflow

| Scenario | Status | Evidence/action |
|---|---|---|
| Create campaign / objectives / JD / plan / approval | TESTED_OFFLINE | Campaign/E2E unit suites |
| Candidate apply / CV parse / screening | TESTED_OFFLINE | E2E and campaign suites |
| Candidate invitation / availability / scheduling | TESTED_OFFLINE | E2E/campaign suites |
| Interview evidence / evaluation / report / hire decision | TESTED_OFFLINE | E2E suite |
| Campaign progress / target achievement | TESTED_OFFLINE | Campaign/workflow group: 173 passed |
| Erasure with approved policy | IMPLEMENTED_NOT_VERIFIED | Policy execution disabled; implementation/tests offline |
| Unauthorized/cross-tenant access | TESTED_OFFLINE | Authorization suites |
| Unapproved campaign / invalid schedule / duplicate answer | TESTED_OFFLINE | E2E and negative suites |
| Worker crash / provider timeout / Redis unavailable | TESTED_OFFLINE | Deterministic suites; real Redis blocked |
| Partial deletion retry | TESTED_OFFLINE | Candidate deletion suite |

## Data protection

| Item | Status | Evidence/action |
|---|---|---|
| Cross-tenant isolation | TESTED_OFFLINE | Tenant/privacy suites |
| Idempotent erasure | TESTED_OFFLINE | Candidate deletion suite |
| Legal hold | TESTED_OFFLINE | Retention/deletion suites |
| Report export erasure | TESTED_OFFLINE | Report erasure suite |
| Candidate API allowlists | TESTED_OFFLINE | Interview source visibility suite |
| Staff permissions | TESTED_OFFLINE | Authorization/support audit suites |
| Retention periods | REQUIRES_CUSTOMER_DECISION | No periods or automatic purge enabled |
| Legal-hold governance | REQUIRES_CUSTOMER_DECISION | Owner/expiry/queue process unresolved |
| Report export content | REQUIRES_CUSTOMER_DECISION | Scores/evidence/recommendation policy unresolved |

## Provider readiness

| Provider | Status | Evidence/action |
|---|---|---|
| OpenAI | TESTED_OFFLINE | Mock transport/tests; live smoke blocked |
| AssemblyAI | TESTED_OFFLINE | Offline STT suite; live upload/poll blocked |
| ElevenLabs | IMPLEMENTED_NOT_VERIFIED | Adapter/lifecycle tests; live request blocked |
| SMTP | TESTED_OFFLINE | Stubbed provider/delivery tests; live relay blocked |
| WebSocket lifecycle/cancel/reconnect | TESTED_OFFLINE | 51 offline voice suite tests |

## Final acceptance gate

P14.1 remains **BLOCKED** for customer acceptance until Docker images are built
and inspected, supported-version Redis and PostgreSQL integrations are run,
GitHub Actions verifies the P14.1 changes, and customer/legal decisions are
recorded. The offline unit suite passes, but test success alone is not
production readiness. See
[`P14_1_STABILIZATION_REPORT.md`](./P14_1_STABILIZATION_REPORT.md) for exact
commands, results, original failure analysis, and infrastructure limitations.
