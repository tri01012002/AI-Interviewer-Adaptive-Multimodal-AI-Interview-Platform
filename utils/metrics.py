"""Provider-neutral application metrics with a strict low-cardinality label policy.

Callers use the module functions ``inc``, ``observe`` and ``set_gauge`` with a
metric name from ``METRIC_SPECS``. When ``prometheus_client`` is importable and
``settings.METRICS_ENABLED`` is true, samples go to a *dedicated*
``CollectorRegistry`` (never the process-global default registry) that
``GET /metrics`` renders. Otherwise every call is a cheap no-op, so the app and
workers run unchanged without the library.

Label policy
------------
Labels are low-cardinality enums only. Every label value is checked against a
per-label allowlist (or, for ``route``, a route-template validator); anything
unexpected is recorded as ``other``. Organization / candidate / interview ids,
emails, raw paths, query strings, prompts, transcripts and tokens can therefore
never become label values. Unknown metric or label *names* raise ``ValueError``
because they are programming errors that tests must catch.

Recording never raises for runtime reasons: a failure inside the metrics
library is logged at DEBUG and swallowed so observability cannot break a
request, an LLM call or a worker.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from config import settings

try:  # pragma: no cover - exercised implicitly by import
    import prometheus_client as _prometheus
except ImportError:  # pragma: no cover - offline installs without the library
    _prometheus = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

METRIC_PREFIX = "ai_interviewer_"
OTHER = "other"
NONE = "none"
CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_SLOW_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0)
_COUNT_BUCKETS = (0, 1, 2, 3, 5, 8, 10, 20, 50)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_ROUTE_TEMPLATE_RE = re.compile(r"/[A-Za-z0-9_./{}:-]{0,200}")
_DIGITS_SEGMENT_RE = re.compile(r"/\d+(/|$)")


def _allow(*values: str) -> frozenset[str]:
    return frozenset(values)


def normalize_route(value: str | None) -> str:
    """Accept only FastAPI route templates; never a raw path containing ids."""
    if not value:
        return "unmatched"
    if (
        _ROUTE_TEMPLATE_RE.fullmatch(value) is None
        or "@" in value
        or _UUID_RE.search(value)
        or _DIGITS_SEGMENT_RE.search(value)
    ):
        return OTHER
    return value


_STATUS_CLASSES = _allow("1xx", "2xx", "3xx", "4xx", "5xx")
_METHODS = _allow("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_LLM_PROVIDERS = _allow("openai", "mock")
_LLM_ERROR_CATEGORIES = _allow(
    NONE, "auth", "rate_limited", "server_error", "timeout", "network", "invalid_response", "http_error"
)
_OUTCOMES_OK_ERROR = _allow("success", "error")
_WORK_KINDS = _allow("campaign_execution", "candidate_application", "candidate_communication")
_VOICE_PROVIDERS = _allow("fake", "assemblylabs", "elevenlabs")
_VOICE_ERROR_CODES = _allow(
    "STT_PROVIDER_ERROR",
    "STT_TIMEOUT",
    "STT_RATE_LIMITED",
    "STT_AUTH_ERROR",
    "TTS_PROVIDER_ERROR",
    "TTS_TIMEOUT",
    "TTS_RATE_LIMITED",
)


@dataclass(frozen=True)
class MetricSpec:
    kind: str  # counter | histogram | gauge
    documentation: str
    labels: Mapping[str, frozenset[str] | Callable[[str | None], str]] = field(default_factory=dict)
    buckets: tuple[float, ...] | None = None


METRIC_SPECS: dict[str, MetricSpec] = {
    # HTTP (apps/api/middleware/request_context.py)
    "http_requests_total": MetricSpec(
        "counter",
        "HTTP requests by route template, method and status class.",
        {"method": _METHODS, "route": normalize_route, "status_class": _STATUS_CLASSES},
    ),
    "http_request_duration_seconds": MetricSpec(
        "histogram",
        "HTTP request latency by route template and method.",
        {"method": _METHODS, "route": normalize_route},
        _LATENCY_BUCKETS,
    ),
    # Rate limiting (apps/api/middleware/rate_limit.py)
    "rate_limit_decisions_total": MetricSpec(
        "counter",
        "Endpoint rate-limit decisions.",
        {
            "scope": _allow("interview_answer", "cv_upload", "login"),
            "outcome": _allow("allowed", "limited", "backend_error"),
        },
    ),
    # LLM provider (integrations/llm_providers/provider.py)
    "llm_requests_total": MetricSpec(
        "counter",
        "LLM provider HTTP requests (one per logical call, retries included).",
        {
            "provider": _LLM_PROVIDERS,
            "operation": _allow("generate", "generate_structured"),
            "outcome": _OUTCOMES_OK_ERROR,
            "error_category": _LLM_ERROR_CATEGORIES,
        },
    ),
    "llm_request_duration_seconds": MetricSpec(
        "histogram",
        "LLM provider request latency, retries included.",
        {"provider": _LLM_PROVIDERS, "operation": _allow("generate", "generate_structured"), "outcome": _OUTCOMES_OK_ERROR},
        _SLOW_BUCKETS,
    ),
    "llm_tokens_total": MetricSpec(
        "counter",
        "Tokens reported by the provider response `usage` block.",
        {"provider": _LLM_PROVIDERS, "kind": _allow("prompt", "completion")},
    ),
    "llm_estimated_cost_usd_total": MetricSpec(
        "counter",
        "Estimated LLM spend from operator-configured per-1K-token prices (absent when unpriced).",
        {"provider": _LLM_PROVIDERS, "kind": _allow("prompt", "completion")},
    ),
    "llm_structured_output_invalid_total": MetricSpec(
        "counter",
        "Structured LLM outputs that failed schema validation.",
        {"provider": _LLM_PROVIDERS},
    ),
    # RAG (rag/service.py HybridRetriever.retrieve)
    "rag_retrievals_total": MetricSpec(
        "counter",
        "Hybrid retrievals by outcome (hit = at least one chunk selected).",
        {"outcome": _allow("hit", "miss", "error")},
    ),
    "rag_retrieval_duration_seconds": MetricSpec(
        "histogram",
        "Hybrid retrieval latency.",
        {"outcome": _allow("hit", "miss", "error")},
        _LATENCY_BUCKETS,
    ),
    "rag_retrieved_chunks": MetricSpec(
        "histogram",
        "Chunks selected per successful retrieval.",
        {},
        _COUNT_BUCKETS,
    ),
    # Recruitment worker (services/recruitment_campaign_worker.py, services/lease_renewal.py)
    "worker_work_items_total": MetricSpec(
        "counter",
        "Worker work items by kind and outcome (completed / skipped / error).",
        {"kind": _WORK_KINDS, "outcome": _allow("completed", "skipped", "error")},
    ),
    "worker_work_item_duration_seconds": MetricSpec(
        "histogram",
        "Worker work item execution latency.",
        {"kind": _WORK_KINDS, "outcome": _allow("completed", "skipped", "error")},
        _SLOW_BUCKETS,
    ),
    "worker_lease_renewals_total": MetricSpec(
        "counter",
        "Lease renewal attempts (ok / lost = ownership lost / failed = renew raised).",
        {"kind": _WORK_KINDS, "outcome": _allow("ok", "lost", "failed")},
    ),
    "worker_heartbeats_total": MetricSpec(
        "counter",
        "Worker loop heartbeats by worker state.",
        {"worker_state": _allow("running", "paused")},
    ),
    "worker_heartbeat_timestamp_seconds": MetricSpec(
        "gauge",
        "Unix time of the last successful worker heartbeat in this process.",
    ),
    "worker_loop_errors_total": MetricSpec(
        "counter",
        "Worker dispatcher loop failures by error category.",
        {"error_category": _allow("database", "unexpected")},
    ),
    # Voice (voice/session.py)
    "voice_provider_requests_total": MetricSpec(
        "counter",
        "STT/TTS provider operations by outcome.",
        {
            "kind": _allow("stt", "tts"),
            "provider": _VOICE_PROVIDERS,
            "operation": _allow("start", "stream", "finalize", "synthesize"),
            "outcome": _OUTCOMES_OK_ERROR,
        },
    ),
    "voice_provider_duration_seconds": MetricSpec(
        "histogram",
        "STT finalization and full TTS stream latency.",
        {"kind": _allow("stt", "tts"), "provider": _VOICE_PROVIDERS, "operation": _allow("finalize", "synthesize")},
        _SLOW_BUCKETS,
    ),
    "voice_provider_errors_total": MetricSpec(
        "counter",
        "STT/TTS provider failures by normalized voice error code.",
        {"kind": _allow("stt", "tts"), "provider": _VOICE_PROVIDERS, "error_code": _VOICE_ERROR_CODES},
    ),
    # Readiness (apps/api/main.py /health/ready)
    "dependency_up": MetricSpec(
        "gauge",
        "1 when the last readiness probe of the dependency succeeded, else 0.",
        {"dependency": _allow("database", "redis")},
    ),
    "dependency_check_duration_seconds": MetricSpec(
        "histogram",
        "Readiness probe latency per dependency.",
        {"dependency": _allow("database", "redis")},
        _LATENCY_BUCKETS,
    ),
}


def normalize_label(metric: str, label: str, value: Any) -> str:
    """Map a label value onto its allowlist (``other`` when unexpected)."""
    spec = _spec(metric)
    if label not in spec.labels:
        raise ValueError(f"Metric {metric} has no label {label!r}")
    rule = spec.labels[label]
    text = None if value is None else str(value)
    if callable(rule):
        return rule(text)
    if text is None:
        return NONE if NONE in rule else OTHER
    return text if text in rule else OTHER


def _spec(metric: str) -> MetricSpec:
    try:
        return METRIC_SPECS[metric]
    except KeyError:
        raise ValueError(f"Unknown metric {metric!r}") from None


def _label_values(metric: str, labels: Mapping[str, Any] | None) -> dict[str, str]:
    spec = _spec(metric)
    provided = dict(labels or {})
    unknown = set(provided) - set(spec.labels)
    if unknown:
        raise ValueError(f"Metric {metric} has no labels {sorted(unknown)}")
    return {name: normalize_label(metric, name, provided.get(name)) for name in spec.labels}


class MetricsRegistry:
    """Owns one dedicated ``CollectorRegistry`` and the collectors in it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registry: Any = None
        self._collectors: dict[str, Any] = {}
        if _prometheus is not None:
            self._registry = _prometheus.CollectorRegistry(auto_describe=True)

    @property
    def available(self) -> bool:
        return self._registry is not None

    @property
    def enabled(self) -> bool:
        return self.available and bool(getattr(settings, "METRICS_ENABLED", True))

    def _collector(self, metric: str) -> Any:
        collector = self._collectors.get(metric)
        if collector is not None:
            return collector
        spec = _spec(metric)
        with self._lock:
            collector = self._collectors.get(metric)
            if collector is None:
                name = METRIC_PREFIX + metric
                labelnames = tuple(spec.labels)
                if spec.kind == "counter":
                    collector = _prometheus.Counter(
                        name.removesuffix("_total"), spec.documentation, labelnames, registry=self._registry
                    )
                elif spec.kind == "histogram":
                    collector = _prometheus.Histogram(
                        name, spec.documentation, labelnames, registry=self._registry, buckets=spec.buckets
                    )
                else:
                    collector = _prometheus.Gauge(name, spec.documentation, labelnames, registry=self._registry)
                self._collectors[metric] = collector
        return collector

    def _child(self, metric: str, labels: Mapping[str, Any] | None) -> Any:
        values = _label_values(metric, labels)
        collector = self._collector(metric)
        return collector.labels(**values) if values else collector

    def inc(self, metric: str, labels: Mapping[str, Any] | None = None, amount: float = 1.0) -> None:
        if not self.enabled:
            _label_values(metric, labels)
            return
        child = self._child(metric, labels)
        try:
            child.inc(amount)
        except Exception as exc:  # pragma: no cover - library failure
            logger.debug("metric update failed", extra={"metric": metric, "failure_type": type(exc).__name__})

    def observe(self, metric: str, value: float, labels: Mapping[str, Any] | None = None) -> None:
        if not self.enabled:
            _label_values(metric, labels)
            return
        child = self._child(metric, labels)
        try:
            child.observe(value)
        except Exception as exc:  # pragma: no cover - library failure
            logger.debug("metric update failed", extra={"metric": metric, "failure_type": type(exc).__name__})

    def set_gauge(self, metric: str, value: float, labels: Mapping[str, Any] | None = None) -> None:
        if not self.enabled:
            _label_values(metric, labels)
            return
        child = self._child(metric, labels)
        try:
            child.set(value)
        except Exception as exc:  # pragma: no cover - library failure
            logger.debug("metric update failed", extra={"metric": metric, "failure_type": type(exc).__name__})

    def render(self) -> bytes:
        if not self.available:
            return b""
        return _prometheus.generate_latest(self._registry)

    def sample_value(self, sample_name: str, labels: Mapping[str, str] | None = None) -> float | None:
        """Read one exported sample (full name without prefix, e.g. ``http_requests_total``)."""
        if not self.available:
            return None
        return self._registry.get_sample_value(METRIC_PREFIX + sample_name, dict(labels or {}))


_registry_lock = threading.Lock()
_registry = MetricsRegistry()


def get_registry() -> MetricsRegistry:
    return _registry


def reset_registry() -> MetricsRegistry:
    """Replace the registry with an empty one (tests, or after toggling availability)."""
    global _registry
    with _registry_lock:
        _registry = MetricsRegistry()
        return _registry


def metrics_enabled() -> bool:
    return _registry.enabled


def inc(metric: str, labels: Mapping[str, Any] | None = None, amount: float = 1.0) -> None:
    _registry.inc(metric, labels, amount)


def observe(metric: str, value: float, labels: Mapping[str, Any] | None = None) -> None:
    _registry.observe(metric, value, labels)


def set_gauge(metric: str, value: float, labels: Mapping[str, Any] | None = None) -> None:
    _registry.set_gauge(metric, value, labels)


def render_latest() -> bytes:
    return _registry.render()


def status_class(status_code: int) -> str:
    return f"{int(status_code) // 100}xx" if 100 <= int(status_code) <= 599 else OTHER
