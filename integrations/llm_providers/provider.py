"""Provider-neutral LLM boundary and OpenAI-compatible implementation."""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from utils import metrics

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


class LLMProviderError(RuntimeError):
    """Base provider failure."""


class LLMConfigurationError(LLMProviderError):
    """Provider configuration is missing or invalid."""


class LLMTimeoutError(LLMProviderError):
    """Provider request exceeded its timeout."""


class LLMRateLimitError(LLMProviderError):
    """Provider rejected the request due to rate limiting."""


class LLMInvalidOutputError(LLMProviderError):
    """Provider output could not be validated against the requested schema."""


_ERROR_CATEGORY_BY_MESSAGE = {
    "LLM provider server failure": "server_error",
    "LLM provider network failure": "network",
    "LLM provider returned an invalid response": "invalid_response",
}


def _error_category(exc: BaseException) -> str:
    """Map a provider failure onto a fixed, low-cardinality metric category."""
    if isinstance(exc, LLMConfigurationError):
        return "auth"
    if isinstance(exc, LLMRateLimitError):
        return "rate_limited"
    if isinstance(exc, LLMTimeoutError):
        return "timeout"
    if isinstance(exc, LLMProviderError):
        return _ERROR_CATEGORY_BY_MESSAGE.get(str(exc), "http_error")
    if isinstance(exc, ValueError):  # undecodable JSON body
        return "invalid_response"
    return "http_error"


class LLMProvider(Protocol):
    provider_name: str
    model: str
    prompt_version: str

    def generate(self, system_instruction: str, user_content: str) -> str:
        ...

    def generate_structured(self, system_instruction: str, user_content: str, schema: type[T]) -> T:
        ...


@dataclass(frozen=True)
class OpenAISettings:
    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 20.0
    max_tokens: int = 800
    temperature: float = 0.2
    max_retries: int = 1
    prompt_version: str = "question-v1"


class OpenAIProvider:
    """Real OpenAI chat-completions provider using normalized HTTP responses."""

    provider_name = "openai"

    def __init__(self, settings: OpenAISettings, client: httpx.Client | None = None) -> None:
        if not settings.api_key.strip():
            raise LLMConfigurationError("OPENAI_API_KEY is not configured")
        self.settings = settings
        self.model = settings.model
        self.prompt_version = settings.prompt_version
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)

    def generate(self, system_instruction: str, user_content: str) -> str:
        return self._complete(system_instruction, user_content, "generate")

    def _complete(self, system_instruction: str, user_content: str, operation: str) -> str:
        """One logical completion (retries included), recorded in ``utils.metrics``.

        Metric labels are provider / operation / outcome / error category only;
        prompts, outputs and credentials never reach metrics.
        """
        started = perf_counter()
        try:
            content, usage = self._request_completion(system_instruction, user_content, operation, started)
        except Exception as exc:
            self._record_request(operation, "error", _error_category(exc), started)
            raise
        self._record_request(operation, "success", None, started)
        self._record_usage(usage)
        return content

    def _record_request(self, operation: str, outcome: str, error_category: str | None, started: float) -> None:
        labels = {"provider": self.provider_name, "operation": operation, "outcome": outcome}
        metrics.inc("llm_requests_total", {**labels, "error_category": error_category})
        metrics.observe("llm_request_duration_seconds", perf_counter() - started, labels)

    def _record_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        from config import settings as app_settings

        prices = {
            "prompt": getattr(app_settings, "LLM_COST_PER_1K_PROMPT_TOKENS_USD", None),
            "completion": getattr(app_settings, "LLM_COST_PER_1K_COMPLETION_TOKENS_USD", None),
        }
        for kind, field_name in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
            tokens = usage.get(field_name)
            if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
                continue
            labels = {"provider": self.provider_name, "kind": kind}
            metrics.inc("llm_tokens_total", labels, tokens)
            price = prices[kind]
            # Cost is only estimated from operator-configured prices; never invented.
            if price is not None and tokens:
                metrics.inc("llm_estimated_cost_usd_total", labels, tokens / 1000 * float(price))

    def _request_completion(
        self, system_instruction: str, user_content: str, operation: str, started: float
    ) -> tuple[str, Any]:
        payload = {
            "model": self.model,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
        }
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.settings.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.api_key}"},
                    json=payload,
                )
                if response.status_code == 401 or response.status_code == 403:
                    raise LLMConfigurationError("LLM provider authentication failed")
                if response.status_code == 429:
                    raise LLMRateLimitError("LLM provider rate limit")
                if response.status_code >= 500:
                    if attempt >= self.settings.max_retries:
                        raise LLMProviderError("LLM provider server failure")
                    self._backoff(attempt)
                    continue
                response.raise_for_status()
                body = response.json()
                try:
                    content = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise LLMProviderError("LLM provider returned an invalid response") from exc
                usage = body.get("usage") if isinstance(body, dict) else None
                usage_fields = usage if isinstance(usage, dict) else {}
                logger.info(
                    "llm request completed",
                    extra={
                        "operation": operation,
                        "provider": self.provider_name,
                        "model": self.model,
                        "latency_ms": round((perf_counter() - started) * 1000, 2),
                        "retry_count": attempt,
                        "prompt_version": self.prompt_version,
                        "input_tokens": usage_fields.get("prompt_tokens"),
                        "output_tokens": usage_fields.get("completion_tokens"),
                    },
                )
                return str(content), usage
            except httpx.TimeoutException as exc:
                if attempt >= self.settings.max_retries:
                    raise LLMTimeoutError("LLM provider timed out") from exc
                self._backoff(attempt)
            except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt >= self.settings.max_retries:
                    raise LLMProviderError("LLM provider network failure") from exc
                self._backoff(attempt)
        raise LLMProviderError("LLM provider request failed")

    def generate_structured(self, system_instruction: str, user_content: str, schema: type[T]) -> T:
        structured_instruction = (
            f"{system_instruction}\nReturn only valid JSON matching this schema: "
            f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
        )
        for attempt in range(self.settings.max_retries + 1):
            raw = self._complete(structured_instruction, user_content, "generate_structured")
            try:
                return schema.model_validate_json(raw)
            except (ValidationError, ValueError, TypeError) as exc:
                metrics.inc("llm_structured_output_invalid_total", {"provider": self.provider_name})
                if attempt >= self.settings.max_retries:
                    raise LLMInvalidOutputError("LLM structured output failed schema validation") from exc
                self._backoff(attempt)
        raise LLMInvalidOutputError("LLM structured output failed")

    @staticmethod
    def _backoff(attempt: int) -> None:
        import time

        time.sleep(min(0.5, (0.1 * (2**attempt)) + random.uniform(0, 0.05)))


class DeterministicMockProvider:
    """Offline structured fixtures for development; outputs are explicitly labeled as mock."""

    provider_name = "mock"
    model = "offline-fixture-v1"
    prompt_version = "offline-fixture-v1"

    def generate(self, system_instruction: str, user_content: str) -> str:
        return "DETERMINISTIC MOCK FIXTURE - no AI provider was called."

    def generate_structured(self, system_instruction: str, user_content: str, schema: type[T]) -> T:
        if schema.__name__ == "AnswerAnalysis":
            fixture: dict[str, Any] = {
                "competencies": [],
                "evidence": [],
                "evidence_strength": "weak",
                "gaps": ["Mock fixture only; no real answer analysis was performed."],
            }
        elif schema.__name__ == "QuestionProposal":
            competency = self._field(user_content, "COMPETENCY") or "general"
            difficulty = self._field(user_content, "DIFFICULTY")
            if difficulty not in {"easy", "medium", "hard"}:
                difficulty = "medium"
            fixture = {
                "question": (
                    "[MOCK QUESTION - NOT AI-GENERATED] Describe a concrete "
                    f"{competency} example, your role, and its measurable outcome."
                ),
                "competency": competency,
                "difficulty": difficulty,
                "intent": "Deterministic fixture question; not generated by an AI model.",
                "expected_evidence": ["a concrete example", "the candidate's role", "an outcome"],
            }
        elif schema.__name__ == "StructuredAnswerEvaluation":
            fixture = {
                "technical_correctness": 0,
                "relevance": 0,
                "depth": 0,
                "completeness": 0,
                "practical_experience": 0,
                "evidence": [],
                "strengths": [],
                "gaps": ["Mock fixture only; no competency judgment was made."],
                "missing_evidence": ["A real evaluation is not configured."],
                "confidence": 0.0,
                "rationale": (
                    "MOCK EVALUATION ONLY: no real AI evaluation was performed; "
                    "the score is not a candidate assessment."
                ),
                "next_action": "DIG_DEEPER",
            }
        elif schema.__name__ == "GeneratedJobDesign":
            fixture = {
                "structured_requirements": {
                    "mock_fixture": True,
                    "notice": "MOCK DATA - NOT AI-GENERATED",
                },
                "job_description": (
                    "[MOCK - NOT AI-GENERATED] Offline fixture only; "
                    "replace with a human-reviewed job description."
                ),
                "competency_rubric": [
                    {
                        "competency": "mock-placeholder",
                        "weight": 1.0,
                        "notice": "MOCK DATA - NOT A REAL HIRING CRITERION",
                    }
                ],
                "application_form_schema": {
                    "type": "object",
                    "properties": {
                        "mock_placeholder": {
                            "type": "string",
                            "description": "MOCK DATA - NOT AI-GENERATED",
                        }
                    },
                },
            }
        else:
            raise LLMProviderError(
                f"No deterministic mock fixture is defined for {schema.__name__}"
            )
        return schema.model_validate(fixture)

    @staticmethod
    def _field(content: str, name: str) -> str:
        prefix = f"{name}:"
        for line in content.splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        return ""


def validate_provider_settings(settings: Any) -> str:
    """Validate explicit provider selection before constructing application services."""
    provider_name = str(getattr(settings, "LLM_PROVIDER", "none")).strip().lower()
    if provider_name in {"none", "mock"}:
        return provider_name
    if provider_name == "openai":
        api_key = str(getattr(settings, "OPENAI_API_KEY", "")).strip()
        if not api_key:
            raise LLMConfigurationError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        return provider_name
    raise LLMConfigurationError(f"Unsupported LLM_PROVIDER: {provider_name or '<empty>'}")


def provider_from_settings(settings: Any) -> LLMProvider | None:
    """Return the explicitly selected provider; never silently substitute another mode."""
    provider_name = validate_provider_settings(settings)
    if provider_name == "none":
        return None
    if provider_name == "mock":
        return DeterministicMockProvider()
    return OpenAIProvider(
        OpenAISettings(
            api_key=settings.OPENAI_API_KEY.strip(),
            model=settings.OPENAI_MODEL,
            base_url=settings.OPENAI_BASE_URL,
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
            max_tokens=settings.OPENAI_MAX_TOKENS,
            temperature=settings.OPENAI_TEMPERATURE,
            max_retries=settings.LLM_MAX_RETRIES,
        )
    )
