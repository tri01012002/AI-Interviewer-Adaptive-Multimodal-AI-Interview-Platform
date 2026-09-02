"""Provider-neutral LLM boundary and OpenAI-compatible implementation."""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

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
    api_key: str
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
        if not settings.api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is not configured")
        self.settings = settings
        self.model = settings.model
        self.prompt_version = settings.prompt_version
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)

    def generate(self, system_instruction: str, user_content: str) -> str:
        payload = {
            "model": self.model,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
        }
        started = perf_counter()
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
                logger.info(
                    "llm request completed",
                    extra={
                        "operation": "generate",
                        "provider": self.provider_name,
                        "model": self.model,
                        "latency_ms": round((perf_counter() - started) * 1000, 2),
                        "retry_count": attempt,
                        "prompt_version": self.prompt_version,
                        "input_tokens": body.get("usage", {}).get("prompt_tokens"),
                        "output_tokens": body.get("usage", {}).get("completion_tokens"),
                    },
                )
                return str(content)
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
            raw = self.generate(structured_instruction, user_content)
            try:
                return schema.model_validate_json(raw)
            except (ValidationError, ValueError, TypeError) as exc:
                if attempt >= self.settings.max_retries:
                    raise LLMInvalidOutputError("LLM structured output failed schema validation") from exc
                self._backoff(attempt)
        raise LLMInvalidOutputError("LLM structured output failed")

    @staticmethod
    def _backoff(attempt: int) -> None:
        import time

        time.sleep(min(0.5, (0.1 * (2**attempt)) + random.uniform(0, 0.05)))


def provider_from_settings(settings: Any) -> LLMProvider | None:
    """Return a real provider only when explicitly configured."""
    if getattr(settings, "LLM_PROVIDER", "none") != "openai" or not getattr(settings, "OPENAI_API_KEY", ""):
        return None
    return OpenAIProvider(
        OpenAISettings(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_MODEL,
            base_url=settings.OPENAI_BASE_URL,
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
            max_tokens=settings.OPENAI_MAX_TOKENS,
            temperature=settings.OPENAI_TEMPERATURE,
            max_retries=settings.LLM_MAX_RETRIES,
        )
    )
