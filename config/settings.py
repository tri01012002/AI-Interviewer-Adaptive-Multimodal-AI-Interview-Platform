"""
AI Interviewer - Main application configuration
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings"""

    # Application
    APP_NAME: str = "AI Interviewer"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = Field(default="development", alias="ENVIRONMENT")
    DEBUG: bool = False
    LOG_LEVEL: str = "info"

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_PREFIX: str = "/api/v1"

    # Frontend
    FRONTEND_URL: str = "http://localhost:3000"
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Database
    # Default is SQLite for local/dev reliability; set DATABASE_URL to a PostgreSQL DSN
    # in production environments where a managed database is available.
    DATABASE_URL: str = "sqlite:///./storage/ai_interviewer.db"
    SQLALCHEMY_ECHO: bool = False

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL: int = 3600

    # Security
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ENABLE_DEV_ADMIN_BOOTSTRAP: bool = False
    BOOTSTRAP_ADMIN_EMAIL: str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""

    # CORS
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: list[str] = ["*"]
    CORS_ALLOW_HEADERS: list[str] = ["*"]

    # LLM Providers
    OPENAI_API_KEY: str = ""
    LLM_PROVIDER: str = "none"
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_MAX_RETRIES: int = 1
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4-turbo-preview"
    OPENAI_TEMPERATURE: float = 0.7
    OPENAI_MAX_TOKENS: int = 2000

    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-3-opus-20240229"

    # Embeddings
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # Voice Services
    ELEVEN_LABS_API_KEY: str = ""
    ELEVEN_LABS_VOICE_ID: str = "default"
    ASSEMBLY_AI_API_KEY: str = ""

    # Voice Settings
    VAD_THRESHOLD: float = 0.5
    VAD_SAMPLE_RATE: int = 16000
    STT_LANGUAGE: str = "en-US"
    TTS_SPEED: float = 1.0
    VOICE_ENABLED: bool = True
    VOICE_MAX_AUDIO_CHUNK_BYTES: int = 64 * 1024
    VOICE_MAX_BUFFER_BYTES: int = 2 * 1024 * 1024
    VOICE_MAX_UTTERANCE_SECONDS: int = 120
    VOICE_WS_MAX_MESSAGE_BYTES: int = 128 * 1024
    VOICE_WS_IDLE_TIMEOUT_SECONDS: int = 300
    VAD_PROVIDER: str = "fake"
    STT_PROVIDER: str = "fake"
    STT_MODEL: str = "local-fake"
    STT_TIMEOUT_SECONDS: float = 20.0
    STT_MAX_RETRIES: int = 1
    TTS_PROVIDER: str = "fake"
    TTS_MODEL: str = "local-fake"
    TTS_TIMEOUT_SECONDS: float = 20.0
    TTS_MAX_RETRIES: int = 1
    VOICE_TELEMETRY_ENABLED: bool = True

    # RAG Configuration
    VECTOR_STORE_PROVIDER: str = "supabase"
    VECTOR_STORE_DIMENSION: int = 1536
    VECTOR_SIMILARITY_THRESHOLD: float = 0.7
    RETRIEVAL_TOP_K: int = 5
    RETRIEVAL_STRATEGY: str = "hybrid"
    RERANKER_TOP_K: int = 3
    RERANKER_THRESHOLD: float = 0.5

    # Interview Configuration
    DEFAULT_INTERVIEW_DURATION: int = 1800  # 30 minutes
    DEFAULT_NUM_QUESTIONS: int = 8
    DEFAULT_DIFFICULTY: str = "medium"
    TURN_PROCESSING_LEASE_SECONDS: int = 300
    MIN_CONFIDENCE_THRESHOLD: float = 0.6
    FAIRNESS_CHECK_ENABLED: bool = True

    # Observability
    SENTRY_DSN: Optional[str] = None
    LANGWATCH_API_KEY: Optional[str] = None
    LANGWATCH_ENABLED: bool = True

    # Feature Flags
    ENABLE_VOICE_MODE: bool = True
    ENABLE_VIDEO_MODE: bool = False
    ENABLE_BROWSER_AGENT: bool = True
    ENABLE_RAG_RETRIEVAL: bool = True
    ENABLE_STREAMING: bool = True

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CELERY_TASK_TRACK_STARTED: bool = True
    CELERY_TASK_TIME_LIMIT: int = 3600

    # Storage
    STORAGE_TYPE: str = "local"
    STORAGE_PATH: str = "./storage"

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()


settings = get_settings()
