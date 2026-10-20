"""
AI Interviewer - Main application configuration
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )
    """Application settings"""

    # Application
    APP_NAME: str = "AI Interviewer"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = Field(default="development", alias="ENVIRONMENT")
    DEBUG: bool = False
    LOG_LEVEL: str = "info"

    # API
    API_HOST: str = "127.0.0.1"
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
    REDIS_TIMEOUT_SECONDS: float = 2.0
    REDIS_SESSION_TTL_SECONDS: int = 300
    REDIS_LOCK_TTL_SECONDS: int = 30
    REDIS_LOCK_ACQUIRE_TIMEOUT_SECONDS: float = 2.0

    # PostgreSQL / production pool settings
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    # Security / rate limiting
    AUTH_RATE_LIMIT: int = 10
    INTERVIEW_RATE_LIMIT: int = 20
    VOICE_RATE_LIMIT: int = 60
    LLM_JOB_RATE_LIMIT: int = 5

    # Observability
    OTEL_SERVICE_NAME: str = "ai-interviewer"
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

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
    LLM_PROVIDER: str = "none"
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_MAX_RETRIES: int = 1
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4-turbo-preview"

    OPENAI_TEMPERATURE: float = 0.7
    OPENAI_MAX_TOKENS: int = 2000

    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-3-opus-20240229"

    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "qwen/qwen3-32b"


    # Embeddings
    EMBEDDING_PROVIDER: str = "none"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536
    EMBEDDING_TIMEOUT_SECONDS: float = 10.0

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
    VECTOR_STORE_PROVIDER: str = "sqlalchemy_json"
    VECTOR_STORE_DIMENSION: int = 1536
    VECTOR_SIMILARITY_THRESHOLD: float = 0.7
    RETRIEVAL_TOP_K: int = 5
    RETRIEVAL_STRATEGY: str = "hybrid"
    RERANKER_TOP_K: int = 3
    RERANKER_THRESHOLD: float = 0.5
    RAG_DEMO_CORPUS_VERSION: str = "demo-v1"

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

    # Recruitment campaign database dispatcher
    CAMPAIGN_WORKER_POLL_SECONDS: float = Field(default=2.0, gt=0, le=60)
    CAMPAIGN_WORKER_CONCURRENCY: int = Field(default=2, ge=1, le=32)
    CAMPAIGN_WORKER_RETRY_BASE_SECONDS: int = Field(default=5, ge=1, le=3600)
    CAMPAIGN_WORKER_RETRY_MAX_SECONDS: int = Field(default=300, ge=1, le=86400)
    CAMPAIGN_WORKER_LEASE_SECONDS: int = Field(default=120, ge=10, le=3600)

    # Email delivery is simulated unless an operator explicitly selects SMTP.
    EMAIL_PROVIDER: str = "offline_simulation"
    EMAIL_SMTP_HOST: str = ""
    EMAIL_SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    EMAIL_SMTP_USERNAME: str = ""
    EMAIL_SMTP_PASSWORD: SecretStr = SecretStr("")
    EMAIL_SMTP_FROM: str = ""
    EMAIL_SMTP_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=30)

    # Storage
    STORAGE_TYPE: str = "local"
    STORAGE_PATH: str = "./storage"
    CV_STORAGE_PATH: str = "storage/private_cv"
    CV_MAX_UPLOAD_BYTES: int = Field(default=5 * 1024 * 1024, gt=0, le=25 * 1024 * 1024)



@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()


settings = get_settings()
