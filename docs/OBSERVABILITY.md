# Observability

Provider-neutral metrics and structured logs wired to real application events.
Metrics live in `utils/metrics.py`; correlation logging lives in `utils/logger.py`.

## Metrics runtime

* Backed by `prometheus_client` (already in `requirements.txt`) using a **dedicated
  `CollectorRegistry`**, never the process-global default registry.
* `METRICS_ENABLED` (default `true`). When `false`, or when `prometheus_client` is not
  importable, every recording call is a no-op and `GET /metrics` returns 404. The API,
  the worker and the voice path behave identically either way.
* Recording never raises for runtime reasons. Unknown metric or label **names** raise
  `ValueError` (programming errors caught by `tests/unit/test_observability_metrics.py`).
* Metrics are per process. The API and the recruitment worker are separate processes, so
  worker metrics are held in the worker's own registry (see "Not implemented").

## Label policy

Only low-cardinality enum labels are allowed: `method`, `route` (FastAPI route
template), `status_class`, `scope` (rate-limit scope), `provider`, `operation`, `outcome`,
`error_category`, `error_code`, `kind`, `worker_state`, `dependency`.

Every label value is checked against a per-metric allowlist (`METRIC_SPECS`); anything
unexpected is recorded as `other`. `route` accepts only route templates
(`/api/v1/interview/{interview_id}`); a value containing a UUID, an `@`, or a numeric
path segment becomes `other`, and requests that matched no route are `unmatched`.
Organization, candidate, interview, execution and communication ids, emails, raw paths,
query strings, prompts, transcripts, audio, rate-limit identities and credentials are
**never** label values. A unit test exercises the app with ids and emails in the path
and asserts the exposition contains no UUID or email.

## Metric catalog

All names are prefixed `ai_interviewer_`.

| Metric | Type | Labels | Source event |
| --- | --- | --- | --- |
| `http_requests_total` | counter | method, route, status_class | Every HTTP request, `RequestContextMiddleware` (excludes `/metrics`) |
| `http_request_duration_seconds` | histogram | method, route | Same |
| `rate_limit_decisions_total` | counter | scope (`interview_answer`, `cv_upload`, `login`), outcome (`allowed`, `limited`, `backend_error`) | `enforce_rate_limit` in `apps/api/middleware/rate_limit.py` |
| `llm_requests_total` | counter | provider, operation (`generate`, `generate_structured`), outcome (`success`, `error`), error_category (`none`, `auth`, `rate_limited`, `server_error`, `timeout`, `network`, `invalid_response`, `http_error`) | Each logical `OpenAIProvider` completion (retries included) |
| `llm_request_duration_seconds` | histogram | provider, operation, outcome | Same |
| `llm_tokens_total` | counter | provider, kind (`prompt`, `completion`) | `usage.prompt_tokens` / `usage.completion_tokens` of the provider response, when present |
| `llm_estimated_cost_usd_total` | counter | provider, kind | Only when `LLM_COST_PER_1K_PROMPT_TOKENS_USD` / `LLM_COST_PER_1K_COMPLETION_TOKENS_USD` are configured. No prices are built in. |
| `llm_structured_output_invalid_total` | counter | provider | Structured output failing schema validation |
| `rag_retrievals_total` | counter | outcome (`hit`, `miss`, `error`) | `HybridRetriever.retrieve` (hit = at least one chunk selected) |
| `rag_retrieval_duration_seconds` | histogram | outcome | Same |
| `rag_retrieved_chunks` | histogram | - | Selected chunk count per successful retrieval |
| `worker_work_items_total` | counter | kind (`campaign_execution`, `candidate_application`, `candidate_communication`), outcome (`completed`, `skipped`, `error`) | `run_work_item` in `services/recruitment_campaign_worker.py` |
| `worker_work_item_duration_seconds` | histogram | kind, outcome | Same |
| `worker_lease_renewals_total` | counter | kind, outcome (`ok`, `lost`, `failed`) | `services/lease_renewal.LeaseRenewal` |
| `worker_heartbeats_total` | counter | worker_state (`running`, `paused`) | Worker loop heartbeat |
| `worker_heartbeat_timestamp_seconds` | gauge | - | Unix time of the last heartbeat written by this process |
| `worker_loop_errors_total` | counter | error_category (`database`, `unexpected`) | Worker dispatcher loop failure |
| `voice_provider_requests_total` | counter | kind (`stt`, `tts`), provider (`fake`, `assemblylabs`, `elevenlabs`), operation (`start`, `stream`, `finalize`, `synthesize`), outcome | `voice/session.py` STT start / streaming / finalization, TTS stream |
| `voice_provider_duration_seconds` | histogram | kind, provider, operation (`finalize`, `synthesize`) | STT finalization and full TTS stream |
| `voice_provider_errors_total` | counter | kind, provider, error_code (normalized `VoiceErrorCode`, e.g. `STT_TIMEOUT`, `TTS_RATE_LIMITED`) | Same, failures only |
| `dependency_up` | gauge | dependency (`database`, `redis`) | `/health/ready` (and `/readiness`) |
| `dependency_check_duration_seconds` | histogram | dependency | Same |

Notes:

* `worker_work_items_total` covers items dispatched by the worker's `run` loop and the
  application / communication steps of `dispatch_once`. Campaign executions run by
  `RecruitmentCampaignOrchestrator.dispatch_once` (the synchronous helper) are visible
  through lease-renewal metrics and logs only.
* Voice metrics record what the client was told; they never change the events sent.

## GET /metrics

Prometheus text format (`text/plain; version=0.0.4`). Policy, in order:

1. `METRICS_ENABLED=false` or `prometheus_client` missing: **404**.
2. `METRICS_BEARER_TOKEN` set: requires `Authorization: Bearer <token>` (compared with
   `hmac.compare_digest`); otherwise **401** with `WWW-Authenticate: Bearer`.
3. No token and `ENVIRONMENT=production`: **404**. Metrics are never exposed
   unauthenticated in production.
4. No token outside production: open (local development, tests, CI).

`/metrics` is excluded from `http_requests_total` so scrapes do not distort traffic.
It is not listed in the OpenAPI schema.

## Log fields

JSON logs (`utils/logger.CustomJsonFormatter`) carry `timestamp`, `level`, `logger`,
`environment` and `message`, plus correlation identifiers bound with
`bind_log_context` (allowlisted names only, values truncated to 128 characters):

* `request_id`: every HTTP and WebSocket request (`RequestContextMiddleware`, echoed in
  `X-Request-ID`). One `request completed` line per request with `http_method`, `route`
  (template), `status_code`, `duration_ms`; probe routes log at DEBUG.
* `execution_id` / `application_id` / `communication_id`: bound around each worker
  work item by `run_work_item`, so every log line emitted while processing the item
  (orchestrator, services, LLM provider, lease renewal caller) carries the id.
* LLM: `llm request completed` with `operation`, `provider`, `model`, `latency_ms`,
  `retry_count`, `prompt_version`, `input_tokens`, `output_tokens`.
* RAG: `retrieval completed` with strategy, counts, latency, fallback reason, versions.
* Failures log `failure_type` (exception class name), never messages that might hold
  secrets or candidate content.

## Not implemented

* Distributed tracing (no OpenTelemetry spans or trace propagation).
* Alerting rules, SLOs and dashboards (no Prometheus rules or Grafana JSON ship here).
* An error-reporting backend (`SENTRY_DSN` / `LANGWATCH_*` settings exist but nothing
  sends to them).
* Multi-process aggregation: uvicorn with several workers, or the separate worker
  process, each hold their own registry. `prometheus_client` multiprocess mode is not
  configured, and the recruitment worker does not serve its own `/metrics` endpoint.
* Embedding-provider and SMTP send metrics; per-model labels (deliberately omitted to keep
  cardinality fixed).

## Deployment integration requirements

* Install `prometheus-client` (pinned in `requirements.txt`) and keep `METRICS_ENABLED=true`.
* In production set `METRICS_BEARER_TOKEN` from a secret store and configure the scraper
  with the same bearer token; restrict `/metrics` at the ingress to the scraper network as
  defense in depth.
* Run one API process per container (or configure multiprocess mode) so a scrape sees the
  whole process's counters; scrape each replica individually.
* To scrape the recruitment worker, expose its registry (for example a sidecar HTTP server
  calling `utils.metrics.render_latest()`); until then rely on its logs and
  `python -m services.recruitment_campaign_worker status`.
* Optional: set `LLM_COST_PER_1K_PROMPT_TOKENS_USD` / `LLM_COST_PER_1K_COMPLETION_TOKENS_USD`
  from your provider contract to enable the estimated-cost counter.
* Ship stdout JSON logs to your log platform and index `request_id`, `execution_id`,
  `application_id` and `communication_id`.
