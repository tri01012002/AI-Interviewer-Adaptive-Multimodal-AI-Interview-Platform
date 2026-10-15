from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal, Optional
import os

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)


class EnvConfig(BaseSettings):

    # =========================
    # Provider
    # =========================

    llm_provider: Literal[
        "groq",
        "anthropic",
        "openai",
        "openrouter"
    ] = "groq"

    # =========================
    # API Keys
    # =========================

    groq_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None

    # =========================
    # Models
    # =========================

    groq_model: str = "openai/gpt-oss-120b"
    anthropic_model: str = "claude-haiku-4-5-20251001"
    openai_model: str = "gpt-5"
    openrouter_model: str = ""

    embedding_model: str = ""

    # =========================
    # Qdrant
    # =========================

    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = None
    qdrant_collection: str = "interview_ai"

    # =========================
    # Logging
    # =========================

    console_log_level: Literal[
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL"
    ] = "INFO"

    model_config = SettingsConfigDict(
        env_file=os.path.join(PROJECT_ROOT, ".env"),
        extra="ignore",
        env_ignore_empty=True,
    )

    @property
    def model(self) -> str:

        if self.llm_provider == "groq":
            return self.groq_model

        if self.llm_provider == "anthropic":
            return self.anthropic_model

        if self.llm_provider == "openai":
            return self.openai_model

        if self.llm_provider == "openrouter":
            return self.openrouter_model

        raise ValueError(
            f"Unsupported provider: {self.llm_provider}"
        )


env_config = EnvConfig()


# ---------------------------------------------------------------------------
# Hằng số dùng chung ngoài phần LLM (đường dẫn lưu trữ local, giới hạn upload)
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
SAMPLE_DIR = os.path.join(DATA_DIR, "sample")
os.makedirs(SESSIONS_DIR, exist_ok=True)

MAX_UPLOAD_MB = 20

# from src.app_logging import setup_logging
# setup_logging()
print("model : ",env_config.groq_model)
print("api_key : ",env_config.groq_api_key)