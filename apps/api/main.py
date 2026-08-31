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

from config import settings
from utils import get_logger, setup_logging

# Initialize logging
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager"""
    
    # Startup
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

# TODO: Import and include routers
# from apps.api.v1.routes import interview, evaluation, candidate, admin
#
# app.include_router(interview.router, prefix=settings.API_PREFIX, tags=["Interview"])
# app.include_router(evaluation.router, prefix=settings.API_PREFIX, tags=["Evaluation"])
# app.include_router(candidate.router, prefix=settings.API_PREFIX, tags=["Candidate"])
# app.include_router(admin.router, prefix=settings.API_PREFIX, tags=["Admin"])

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
