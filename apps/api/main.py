"""
AI Interviewer - Main FastAPI Application
"""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from services.database import init_db
from services.user_store import UserStore
from utils import get_logger, setup_logging

# Initialize logging
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager"""
    
    # Startup
    init_db()
    UserStore.ensure_default_admin()
    logger.info(
        "Starting AI Interviewer",
        environment=settings.ENVIRONMENT,
        version=settings.APP_VERSION,
    )
    
    yield
    
    # Shutdown
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


@app.get("/readiness", tags=["Health"])
async def readiness_check():
    """Readiness check endpoint"""
    # TODO: Check database, cache, external services
    return {"ready": True}


@app.get("/liveness", tags=["Health"])
async def liveness_check():
    """Liveness check endpoint"""
    return {"alive": True}


# ==========================================
# API Routes (to be implemented)
# ==========================================

from apps.api.v1.routes.auth import router as auth_router
from apps.api.v1.routes.candidate import router as candidate_router
from apps.api.v1.routes.interview import router as interview_router
from apps.api.websocket.interview import router as websocket_router

app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(candidate_router, prefix=settings.API_PREFIX)
app.include_router(interview_router, prefix=settings.API_PREFIX)
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
