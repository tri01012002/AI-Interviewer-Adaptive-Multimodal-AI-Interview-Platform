"""Job description generation through the provider-neutral LLM boundary."""

from __future__ import annotations

from typing import Any

from apps.api.v1.schemas.job import JobDescription, JobRequestFromUser
from config import settings
from integrations.llm_providers.provider import (
    LLMConfigurationError,
    LLMProvider,
    provider_from_settings,
)
from services.job_store import JobStore
from services.jd_reference import make_jd_prompt


class JDGenerationService:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider if provider is not None else provider_from_settings(settings)

    def generate(self, payload: dict[str, Any], owner_user_id: str) -> dict[str, Any]:
        if self.provider is None:
            raise LLMConfigurationError("JD generation requires a configured LLM provider")

        result = self.provider.generate_structured(
                    """You are a recruitment assistant. Generate a clear, factual job description
        from the hiring request. Do not invent compensation, location, employer identity, or
        requirements that are not supported by the request. Return only the requested schema.""",
            (
                f"Role title: {payload['title']}\n"
                f"Hiring request: {payload['hiring_request']}\n"
                f"Additional requirements: {payload.get('requirements', [])}"
            ),
            JobDescription,
        )
        return JobStore.create(result.model_dump(), owner_user_id)

    def generate_from_brief(self, brief: JobRequestFromUser, owner_user_id: str) -> dict[str, Any]:
        if self.provider is None:
            raise LLMConfigurationError("JD generation requires a configured LLM provider")
        # Tạm thời chưa tham khảo JD cũ: truyền danh sách rỗng.
        system, user = make_jd_prompt(brief, [])
        result = self.provider.generate_structured(system, user, JobDescription)
        return JobStore.create(result.model_dump(), owner_user_id)
