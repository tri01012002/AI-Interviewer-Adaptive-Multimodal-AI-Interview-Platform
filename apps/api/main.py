"""
AI Interviewer - Main FastAPI Application
"""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from infrastructure.redis_store import RedisClient
from services.database import init_db
from services.support_access_audit import audit_platform_support_access
from services.user_store import UserStore
from utils import get_logger, setup_logging

# Initialize logging
setup_logging()
logger = get_logger(__name__)


def ensure_dev_admin_bootstrap() -> None:
    """Create the expected local dev/test admin if bootstrapping is enabled."""
    if settings.ENVIRONMENT not in {"development", "test"} or not settings.ENABLE_DEV_ADMIN_BOOTSTRAP:
        return
    if not settings.BOOTSTRAP_ADMIN_EMAIL or not settings.BOOTSTRAP_ADMIN_PASSWORD:
        raise RuntimeError("Development admin bootstrap requires explicit credentials")
    UserStore.ensure_default_admin(settings.BOOTSTRAP_ADMIN_EMAIL, settings.BOOTSTRAP_ADMIN_PASSWORD)


def validate_production_security() -> None:
    if settings.ENVIRONMENT != "production":
        return
    if (
        settings.SECRET_KEY == "your-secret-key-change-in-production"
        or len(settings.SECRET_KEY) < 32
    ):
        raise RuntimeError(
            "Production requires an explicitly configured SECRET_KEY of at least 32 characters"
        )
    if settings.ENABLE_DEV_ADMIN_BOOTSTRAP:
        raise RuntimeError("ENABLE_DEV_ADMIN_BOOTSTRAP must be disabled in production")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager"""

    # Startup
    validate_production_security()
    if settings.ENVIRONMENT in {"development", "test"}:
        init_db()
        ensure_dev_admin_bootstrap()
    logger.info(
        "Starting AI Interviewer",
        extra={
            "environment": settings.ENVIRONMENT,
            "version": settings.APP_VERSION,
        },
    )
    app.state.redis_client = RedisClient(
        settings.REDIS_URL,
        namespace="ai",
        socket_timeout=settings.REDIS_TIMEOUT_SECONDS,
    )
    if app.state.redis_client._client is not None:
        app.state.redis_client.ping()
    
    yield
    
    # Shutdown
    if hasattr(app.state, "redis_client"):
        app.state.redis_client.close()
    logger.info("Shutting down AI Interviewer")


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    description="Adaptive Multimodal AI Interview Platform",
    version=settings.APP_VERSION,
    lifespan=lifespan,
    debug=settings.DEBUG,
)

# ==========================================
# Middleware
# ==========================================

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)

# Gzip compression
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def platform_support_audit_middleware(request: Request, call_next):
    return await audit_platform_support_access(request, call_next)

# ==========================================
# Exception Handlers
# ==========================================


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    """Handle validation errors"""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": exc.errors(),
            "status_code": 422,
        },
    )


# ==========================================
# Health Check Endpoints
# ==========================================


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health/live", tags=["Health"])
async def health_live():
    """Liveness probe: process level health only."""
    return {"alive": True, "service": settings.APP_NAME}


@app.get("/health/ready", tags=["Health"])
async def health_ready():
    """Readiness probe for required runtime dependencies."""
    ready = True
    checks: dict[str, bool | str] = {"database": True, "redis": True}
    engine = None
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        ready = False
        checks["database"] = False
        logger.warning(
            "Database readiness check failed",
            extra={"failure_type": type(exc).__name__},
        )
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exc:
                ready = False
                checks["database"] = False
                logger.warning(
                    "Database readiness client cleanup failed",
                    extra={"failure_type": type(exc).__name__},
                )

    client = None
    try:
        import redis

        client = redis.Redis.from_url(
            settings.REDIS_URL,
            socket_timeout=settings.REDIS_TIMEOUT_SECONDS,
        )
        client.ping()
    except Exception as exc:
        ready = False
        checks["redis"] = False
        logger.warning(
            "Redis readiness check failed",
            extra={"failure_type": type(exc).__name__},
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                ready = False
                checks["redis"] = False
                logger.warning(
                    "Redis readiness client cleanup failed",
                    extra={"failure_type": type(exc).__name__},
                )

    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"ready": ready, "checks": checks},
    )


@app.get("/readiness", tags=["Health"])
async def readiness_check():
    """Backward-compatible readiness alias."""
    return await health_ready()


@app.get("/liveness", tags=["Health"])
async def liveness_check():
    """Backward-compatible liveness alias."""
    return await health_live()


# ==========================================
# API Routes (to be implemented)
# ==========================================

from apps.api.v1.routes.account_linking import router as account_linking_router
from apps.api.v1.routes.auth import router as auth_router
from apps.api.v1.routes.candidate import router as candidate_router
from apps.api.v1.routes.interview import router as interview_router
from apps.api.v1.routes.organizations import router as organizations_router
from apps.api.websocket.interview import router as websocket_router

app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(account_linking_router, prefix=settings.API_PREFIX)
app.include_router(candidate_router, prefix=settings.API_PREFIX)
app.include_router(interview_router, prefix=settings.API_PREFIX)
app.include_router(organizations_router, prefix=settings.API_PREFIX)
app.include_router(websocket_router)

app.mount("/web", StaticFiles(directory="apps/web/src"), name="web")

# ==========================================
# WebSocket Routes (to be implemented)
# ==========================================

# TODO: Implement WebSocket endpoints for real-time interviews


# ==========================================
# Startup/Shutdown Events
# ==========================================


@app.on_event("startup")
async def startup_event():
    """Application startup event"""
    logger.info("Application startup complete")


@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown event"""
    logger.info("Application shutdown complete")


# ==========================================
# Root Endpoints
# ==========================================


@app.get("/", tags=["Root"])
async def root():
    """Root endpoint"""
    return {
        "message": "Welcome to AI Interviewer",
        "docs": "/docs",
        "health": "/health",
        "version": settings.APP_VERSION,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
