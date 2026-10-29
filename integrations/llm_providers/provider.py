"""Provider-neutral LLM boundary and OpenAI-compatible implementations."""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)

_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE
)
_BEARER_PATTERN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_API_KEY_PATTERN = re.compile(
    r"\b(?:sk|sess|key)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE
)
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_LONG_ID_PATTERN = re.compile(r"\b[A-F0-9]{16,}\b", re.IGNORECASE)


def _safe_error_message(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    message = " ".join(value.split())
    message = _BEARER_PATTERN.sub("[REDACTED]", message)
    message = _API_KEY_PATTERN.sub("[REDACTED]", message)
    message = _EMAIL_PATTERN.sub("[REDACTED]", message)
    message = _PHONE_PATTERN.sub("[REDACTED]", message)
    message = _LONG_ID_PATTERN.sub("[REDACTED]", message)
    return message[:300] or None


def _provider_error_metadata(response: httpx.Response) -> dict[str, str]:
    try:
        body = response.json()
    except ValueError:
        body = None

    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {}

    metadata: dict[str, str] = {}
    for field in ("type", "code"):
        value = error.get(field)
        if isinstance(value, str):
            safe_value = re.sub(r"[^A-Za-z0-9_.-]", "", value)[:80]
            if safe_value:
                metadata[f"error_{field}"] = safe_value

    message = _safe_error_message(error.get("message"))
    if message:
        metadata["error_message"] = message
    return metadata


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


class LLMProvider(Protocol):
    provider_name: str
    model: str
    prompt_version: str

    def generate(self, system_instruction: str, user_content: str) -> str:
        ...

    def generate_structured(
        self,
        system_instruction: str,
        user_content: str,
        schema: type[T],
    ) -> T:
        ...


@dataclass(frozen=True)
class OpenAISettings:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 20.0
    max_tokens: int = 800
    temperature: float = 0.2
    max_retries: int = 1
    prompt_version: str = "question-v1"


class OpenAIProvider:
    """OpenAI chat-completions provider using normalized HTTP responses."""

    provider_name = "openai"

    def __init__(self, settings: OpenAISettings, client: httpx.Client | None = None) -> None:
        if not settings.api_key.strip():
            raise LLMConfigurationError("OPENAI_API_KEY is not configured")

        self.settings = settings
        self.model = settings.model
        self.prompt_version = settings.prompt_version
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)

    def generate(self, system_instruction: str, user_content: str) -> str:
        return self._generate(system_instruction, user_content)

    def _generate(
        self,
        system_instruction: str,
        user_content: str,
        response_format: dict[str, str] | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
        }
        if response_format is not None:
            payload["response_format"] = response_format

        started = perf_counter()

        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.settings.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.settings.api_key}"
                    },
                    json=payload,
                )

                if response.status_code in (401, 403):
                    raise LLMConfigurationError(
                        "LLM provider authentication failed"
                    )

                if response.status_code == 429:
                    raise LLMRateLimitError("LLM provider rate limit")

                if 400 <= response.status_code < 500:
                    logger.warning(
                        "LLM provider rejected request",
                        extra={
                            "provider": self.provider_name,
                            "model": self.model,
                            "http_status": response.status_code,
                            **_provider_error_metadata(response),
                        },
                    )
                    raise LLMConfigurationError(
                        "LLM provider rejected the request "
                        f"(HTTP {response.status_code})"
                    )

                if response.status_code >= 500:
                    if attempt >= self.settings.max_retries:
                        raise LLMProviderError(
                            "LLM provider server failure"
                        )
                    self._backoff(attempt)
                    continue

                response.raise_for_status()
                body = response.json()

                try:
                    content = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise LLMProviderError(
                        "LLM provider returned an invalid response"
                    ) from exc

                usage = body.get("usage") or {}
                logger.info(
                    "llm request completed",
                    extra={
                        "operation": "generate",
                        "provider": self.provider_name,
                        "model": self.model,
                        "latency_ms": round(
                            (perf_counter() - started) * 1000, 2
                        ),
                        "retry_count": attempt,
                        "prompt_version": self.prompt_version,
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                    },
                )
                return str(content)

            except httpx.TimeoutException as exc:
                if attempt >= self.settings.max_retries:
                    raise LLMTimeoutError(
                        "LLM provider timed out"
                    ) from exc
                self._backoff(attempt)

            except (
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as exc:
                if attempt >= self.settings.max_retries:
                    raise LLMProviderError(
                        "LLM provider network failure"
                    ) from exc
                self._backoff(attempt)

        raise LLMProviderError("LLM provider request failed")

    def generate_structured(
        self,
        system_instruction: str,
        user_content: str,
        schema: type[T],
    ) -> T:
        structured_instruction = (
            f"{system_instruction}\n"
            "Return only valid JSON matching this schema: "
            f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
        )

        for attempt in range(self.settings.max_retries + 1):
            raw = self._generate(
                structured_instruction,
                user_content,
                response_format={"type": "json_object"},
            )

            try:
                return schema.model_validate_json(raw)
            except (ValidationError, ValueError, TypeError) as exc:
                if attempt >= self.settings.max_retries:
                    raise LLMInvalidOutputError(
                        "LLM structured output failed schema validation"
                    ) from exc
                self._backoff(attempt)

        raise LLMInvalidOutputError("LLM structured output failed")

    @staticmethod
    def _backoff(attempt: int) -> None:
        import time

        time.sleep(
            min(0.5, (0.1 * (2**attempt)) + random.uniform(0, 0.05))
        )


class OllamaProvider(OpenAIProvider):
    """Local Ollama provider using its OpenAI-compatible API."""

    provider_name = "ollama"

    def __init__(
        self,
        settings: OpenAISettings,
        client: httpx.Client | None = None,
    ) -> None:
        # Ollama runs locally and does not require an OpenAI API key.
        # A placeholder key is used only for the compatible HTTP endpoint.
        ollama_settings = OpenAISettings(
            api_key="ollama",
            model=settings.model,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            max_retries=settings.max_retries,
            prompt_version=settings.prompt_version,
        )
        super().__init__(ollama_settings, client=client)


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
