"""Phase G observability: metrics wired to real events, /metrics policy, label safety.

Every test runs against a fresh dedicated metrics registry. Nothing here touches
a network provider: LLM calls use ``httpx.MockTransport``, rate limits use an
in-memory backend, voice uses fake providers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.main import app
from apps.api.middleware.rate_limit import endpoint_rate_limiter
from config import settings
from infrastructure.rate_limiter import InMemoryFixedWindowCounter
from integrations.llm_providers.provider import (
    LLMRateLimitError,
    OpenAIProvider,
    OpenAISettings,
)
from rag.service import (
    DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    DocumentChunk,
    HybridRetriever,
    RetrievalFilters,
)
from services.lease_renewal import LeaseRenewal
from services.recruitment_campaign_worker import run_work_item
from utils import metrics
from utils.logger import get_log_context
from voice.contracts import ClientVoiceEvent, STTEvent, VoiceErrorCode
from voice.real_providers import STTProviderError, TTSProviderError
from voice.session import VoiceSessionService

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
API_KEY_SENTINEL = "sk-observability-sentinel-DO-NOT-LEAK-1234567890"
FROZEN_NOW = 6_000_030.0

DOCUMENTS = [
    DocumentChunk(
        "doc-a",
        "chunk-a",
        "Redis Cluster handles distributed cache availability.",
        competency="distributed_systems",
        source_type="knowledge",
        scope="global",
        scope_approval_reference=DEMO_CORPUS_GLOBAL_APPROVAL_REFERENCE,
    ),
]


@pytest.fixture(autouse=True)
def fresh_metrics(monkeypatch):
    monkeypatch.setattr(settings, "METRICS_ENABLED", True)
    monkeypatch.setattr(settings, "METRICS_BEARER_TOKEN", None)
    monkeypatch.setattr(settings, "LLM_COST_PER_1K_PROMPT_TOKENS_USD", None)
    monkeypatch.setattr(settings, "LLM_COST_PER_1K_COMPLETION_TOKENS_USD", None)
    registry = metrics.reset_registry()
    assert registry.enabled
    yield registry
    metrics.reset_registry()


@pytest.fixture
def client():
    endpoint_rate_limiter.set_backend(InMemoryFixedWindowCounter(clock=lambda: FROZEN_NOW))
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        endpoint_rate_limiter.set_backend(None)


def value(name: str, **labels: str) -> float:
    return metrics.get_registry().sample_value(name, labels) or 0.0


def exposition() -> str:
    return metrics.render_latest().decode("utf-8")


def _llm_client(status_code: int = 200, usage: dict | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {API_KEY_SENTINEL}"
        if status_code != 200:
            return httpx.Response(status_code, json={"error": {"message": "synthetic"}})
        body = {"choices": [{"message": {"content": '{"value": "ok"}'}}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _provider(client: httpx.Client) -> OpenAIProvider:
    return OpenAIProvider(OpenAISettings(api_key=API_KEY_SENTINEL, model="test-model", max_retries=0), client)


# --------------------------------------------------------------------- HTTP


def test_http_requests_are_counted_by_route_template_and_status_class(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200

    labels = {"method": "GET", "route": "/health"}
    assert value("http_requests_total", status_class="2xx", **labels) == 2
    assert value("http_request_duration_seconds_count", **labels) == 2

    # Scraping /metrics is never itself metered.
    assert client.get("/metrics").status_code == 200
    assert "/metrics" not in exposition()


def test_readiness_updates_dependency_gauges(client, monkeypatch):
    import redis
    import sqlalchemy

    def fail_database(*_args, **_kwargs):
        raise RuntimeError("synthetic outage")

    monkeypatch.setattr(sqlalchemy, "create_engine", fail_database)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_k: MagicMock())
    assert client.get("/health/ready").status_code == 503

    assert value("dependency_up", dependency="database") == 0
    assert value("dependency_up", dependency="redis") == 1
    assert value("dependency_check_duration_seconds_count", dependency="database") == 1


# --------------------------------------------------------------- rate limit


def test_rate_limit_decisions_are_counted_including_429(client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MINUTE", 1)
    email = f"metrics-{uuid.uuid4()}@example.com"
    assert client.post("/api/v1/auth/register", json={"email": email, "password": "password123"}).status_code == 200
    credentials = {"email": email, "password": "password123"}

    assert client.post("/api/v1/auth/login", json=credentials).status_code == 200
    assert client.post("/api/v1/auth/login", json=credentials).status_code == 429

    assert value("rate_limit_decisions_total", scope="login", outcome="allowed") == 1
    assert value("rate_limit_decisions_total", scope="login", outcome="limited") == 1
    assert value("http_requests_total", method="POST", route="/api/v1/auth/login", status_class="4xx") == 1
    text = exposition()
    assert email not in text and "ip:" not in text


def test_rate_limit_backend_error_is_counted_when_failing_open(monkeypatch):
    class BrokenBackend:
        def hit(self, key, limit, window_seconds):
            raise ConnectionError("synthetic redis outage")

    from apps.api.middleware.rate_limit import enforce_rate_limit

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_FAIL_OPEN", True)
    endpoint_rate_limiter.set_backend(BrokenBackend())
    try:
        enforce_rate_limit("login", "ip:198.51.100.7", 5)
    finally:
        endpoint_rate_limiter.set_backend(None)
    assert value("rate_limit_decisions_total", scope="login", outcome="backend_error") == 1


# ---------------------------------------------------------------------- LLM


def test_llm_tokens_and_outcome_are_recorded_from_response_usage():
    provider = _provider(_llm_client(usage={"prompt_tokens": 11, "completion_tokens": 7}))
    provider.generate("system", "user")

    assert value("llm_tokens_total", provider="openai", kind="prompt") == 11
    assert value("llm_tokens_total", provider="openai", kind="completion") == 7
    assert value(
        "llm_requests_total", provider="openai", operation="generate", outcome="success", error_category="none"
    ) == 1
    assert value("llm_request_duration_seconds_count", provider="openai", operation="generate", outcome="success") == 1
    # No prices configured -> no cost metric is ever invented.
    assert "llm_estimated_cost_usd" not in exposition()


def test_llm_cost_is_estimated_only_from_configured_prices(monkeypatch):
    monkeypatch.setattr(settings, "LLM_COST_PER_1K_PROMPT_TOKENS_USD", 0.5)
    monkeypatch.setattr(settings, "LLM_COST_PER_1K_COMPLETION_TOKENS_USD", 2.0)
    provider = _provider(_llm_client(usage={"prompt_tokens": 1000, "completion_tokens": 500}))

    class OutputModel(__import__("pydantic").BaseModel):
        value: str

    assert provider.generate_structured("system", "user", OutputModel).value == "ok"
    assert value("llm_estimated_cost_usd_total", provider="openai", kind="prompt") == pytest.approx(0.5)
    assert value("llm_estimated_cost_usd_total", provider="openai", kind="completion") == pytest.approx(1.0)
    assert value(
        "llm_requests_total",
        provider="openai",
        operation="generate_structured",
        outcome="success",
        error_category="none",
    ) == 1


def test_llm_missing_usage_and_failures_are_categorized():
    _provider(_llm_client(usage=None)).generate("system", "user")
    assert value("llm_tokens_total", provider="openai", kind="prompt") == 0

    with pytest.raises(LLMRateLimitError):
        _provider(_llm_client(status_code=429)).generate("system", "user")
    assert value(
        "llm_requests_total", provider="openai", operation="generate", outcome="error", error_category="rate_limited"
    ) == 1


# ---------------------------------------------------------------------- RAG


def test_rag_retrieval_records_hit_miss_and_error():
    retriever = HybridRetriever(list(DOCUMENTS))
    assert retriever.retrieve("Redis cache", RetrievalFilters(include_global=True)).chunks
    assert not retriever.retrieve("Redis cache", RetrievalFilters(include_global=False)).chunks

    class BrokenStore:
        def documents(self, filters):
            raise RuntimeError("synthetic store outage")

    with pytest.raises(RuntimeError):
        HybridRetriever(vector_store=BrokenStore()).retrieve("Redis cache")

    assert value("rag_retrievals_total", outcome="hit") == 1
    assert value("rag_retrievals_total", outcome="miss") == 1
    assert value("rag_retrievals_total", outcome="error") == 1
    assert value("rag_retrieved_chunks_count") == 2
    assert value("rag_retrieval_duration_seconds_count", outcome="hit") == 1
    assert "Redis cache" not in exposition()


# ------------------------------------------------------------------- worker


def test_lease_renewal_outcomes_are_counted():
    with LeaseRenewal(lambda: True, work_type="campaign_execution", work_id=str(uuid.uuid4()), interval_seconds=0.02):
        time.sleep(0.15)
    assert value("worker_lease_renewals_total", kind="campaign_execution", outcome="ok") >= 2

    with LeaseRenewal(lambda: False, work_type="candidate_communication", work_id="c-1", interval_seconds=0.02):
        time.sleep(0.1)
    assert value("worker_lease_renewals_total", kind="candidate_communication", outcome="lost") == 1

    def broken():
        raise ConnectionError("synthetic database outage")

    with LeaseRenewal(broken, work_type="unexpected-kind", work_id="x", interval_seconds=0.02, max_consecutive_failures=1):
        time.sleep(0.1)
    assert value("worker_lease_renewals_total", kind="other", outcome="failed") == 1
    assert UUID_RE.search(exposition()) is None


def test_worker_items_bind_log_context_and_record_outcomes():
    execution_id = str(uuid.uuid4())
    seen = {}

    def work(item_id):
        seen.update(get_log_context())
        return MagicMock(status="completed")

    run_work_item("campaign_execution", execution_id, work)
    assert seen["execution_id"] == execution_id
    assert "execution_id" not in get_log_context()

    run_work_item("candidate_application", "app-1", lambda _id: None)

    def boom(_id):
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError):
        run_work_item("candidate_communication", "comm-1", boom)

    assert value("worker_work_items_total", kind="campaign_execution", outcome="completed") == 1
    assert value("worker_work_items_total", kind="candidate_application", outcome="skipped") == 1
    assert value("worker_work_items_total", kind="candidate_communication", outcome="error") == 1
    assert execution_id not in exposition()


# -------------------------------------------------------------------- voice


class FailingSTT:
    provider_name = "assemblylabs"

    async def start_session(self, utterance_id, audio_format):
        return None

    async def end_utterance(self, utterance_id):
        raise STTProviderError("STT_TIMEOUT", "synthetic timeout")
        yield  # pragma: no cover

    async def close(self):
        return None


class WorkingSTT(FailingSTT):
    async def end_utterance(self, utterance_id):
        yield STTEvent(utterance_id=utterance_id, text="Four", is_final=True)


class FailingTTS:
    provider_name = "elevenlabs"

    async def stream(self, text):
        raise TTSProviderError("TTS_RATE_LIMITED", "synthetic rate limit")
        yield b""  # pragma: no cover

    async def close(self):
        return None


def test_stt_and_tts_failures_are_counted_by_provider_and_code():
    interview_service = MagicMock()
    interview_service.submit_answer.return_value = {"current_question": "What next?"}

    async def scenario():
        failing = VoiceSessionService(interview_service, user_id="u", role="candidate", stt=FailingSTT())
        events = await failing.handle("int-1", ClientVoiceEvent(type="audio.end", utterance_id="utt-1"))
        assert any(e.code == VoiceErrorCode.STT_TIMEOUT for e in events)
        tts_failing = VoiceSessionService(
            interview_service, user_id="u", role="candidate", stt=WorkingSTT(), tts=FailingTTS()
        )
        events = await tts_failing.handle("int-1", ClientVoiceEvent(type="audio.end", utterance_id="utt-2"))
        assert any(e.code == VoiceErrorCode.TTS_RATE_LIMITED for e in events)
        assert any(e.type == "interview.question" for e in events)

    asyncio.run(scenario())

    assert value("voice_provider_errors_total", kind="stt", provider="assemblylabs", error_code="STT_TIMEOUT") == 1
    assert value(
        "voice_provider_requests_total", kind="stt", provider="assemblylabs", operation="finalize", outcome="success"
    ) == 1
    assert value("voice_provider_errors_total", kind="tts", provider="elevenlabs", error_code="TTS_RATE_LIMITED") == 1
    assert value("voice_provider_duration_seconds_count", kind="tts", provider="elevenlabs", operation="synthesize") == 1
    text = exposition()
    assert "Four" not in text and "What next?" not in text


# ------------------------------------------------------------ /metrics auth


def test_metrics_endpoint_is_open_in_test_env_without_token(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_endpoint_requires_configured_bearer_token(client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_BEARER_TOKEN", SecretStr("scrape-token-123"))
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Basic scrape-token-123"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer scrape-token-123"})
    assert ok.status_code == 200
    assert "scrape-token-123" not in ok.text


def test_metrics_endpoint_hidden_in_production_without_token(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert client.get("/metrics").status_code == 404
    monkeypatch.setattr(settings, "METRICS_BEARER_TOKEN", SecretStr("prod-token"))
    assert client.get("/metrics", headers={"Authorization": "Bearer prod-token"}).status_code == 200


def test_metrics_endpoint_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_ENABLED", False)
    assert client.get("/metrics").status_code == 404
    client.get("/health")
    monkeypatch.setattr(settings, "METRICS_ENABLED", True)
    assert value("http_requests_total", method="GET", route="/health", status_class="2xx") == 0


# --------------------------------------------------------- label cardinality


def test_no_ids_emails_or_raw_paths_reach_label_values(client):
    interview_id = str(uuid.uuid4())
    client.get(f"/api/v1/interview/{interview_id}")
    client.get(f"/api/v1/interview/someone@example.com?token={uuid.uuid4()}")
    client.get(f"/no/such/route/{uuid.uuid4()}/user@example.com")

    text = exposition()
    assert value("http_requests_total", method="GET", route="/api/v1/interview/{interview_id}", status_class="4xx") == 2
    assert value("http_requests_total", method="GET", route="unmatched", status_class="4xx") == 1
    assert UUID_RE.search(text) is None
    assert EMAIL_RE.search(text) is None
    assert "/no/such/route" not in text


def test_label_guard_normalizes_unexpected_values_and_rejects_unknown_names():
    assert metrics.normalize_route("/api/v1/interview/{interview_id}") == "/api/v1/interview/{interview_id}"
    assert metrics.normalize_route(f"/api/v1/interview/{uuid.uuid4()}") == "other"
    assert metrics.normalize_route("/api/v1/users/a@b.com") == "other"
    assert metrics.normalize_route("/api/v1/orders/12345") == "other"
    assert metrics.normalize_route(None) == "unmatched"
    assert metrics.normalize_label("rag_retrievals_total", "outcome", "org-123") == "other"
    assert metrics.normalize_label("llm_tokens_total", "provider", "x@y.com") == "other"
    with pytest.raises(ValueError):
        metrics.inc("http_requests_total", {"organization_id": "org-1"})
    with pytest.raises(ValueError):
        metrics.inc("no_such_metric")
    for name, spec in metrics.METRIC_SPECS.items():
        assert not {"organization_id", "candidate_id", "interview_id", "email", "path", "query"} & set(spec.labels), name


# ------------------------------------------------------------ secret safety


def test_api_key_never_appears_in_exposition_or_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    _provider(_llm_client(usage={"prompt_tokens": 1, "completion_tokens": 1})).generate("system", "user")
    with pytest.raises(LLMRateLimitError):
        _provider(_llm_client(status_code=429)).generate("system", "user")
    scraped = client.get("/metrics").text
    assert API_KEY_SENTINEL not in scraped
    assert API_KEY_SENTINEL not in exposition()
    assert API_KEY_SENTINEL not in caplog.text


# ------------------------------------------------------------- offline mode


def test_app_works_when_prometheus_client_is_unavailable(monkeypatch):
    original = metrics._prometheus
    metrics._prometheus = None
    try:
        registry = metrics.reset_registry()
        assert not registry.available and not metrics.metrics_enabled()
        metrics.inc("http_requests_total", {"method": "GET", "route": "/health", "status_class": "2xx"})
        assert registry.sample_value("http_requests_total") is None
        _provider(_llm_client(usage={"prompt_tokens": 1, "completion_tokens": 1})).generate("system", "user")
        endpoint_rate_limiter.set_backend(InMemoryFixedWindowCounter(clock=lambda: FROZEN_NOW))
        try:
            with TestClient(app) as test_client:
                assert test_client.get("/health").status_code == 200
                assert test_client.get("/metrics").status_code == 404
        finally:
            endpoint_rate_limiter.set_backend(None)
    finally:
        metrics._prometheus = original
        metrics.reset_registry()
