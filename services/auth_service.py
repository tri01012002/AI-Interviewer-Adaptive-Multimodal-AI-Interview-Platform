"""Authentication utilities for API access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings
from services.security_utils import verify_password
from services.user_store import UserStore

security = HTTPBearer(auto_error=False)


def register_user(email: str, password: str, role: str = "admin") -> dict[str, Any]:
    return UserStore.create(email=email, password=password, role="candidate")


def authenticate_user(email: str, password: str) -> dict[str, Any]:
    user = UserStore.get_by_email(email)
    if user is None or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return user


def issue_token(email: str, role: str = "admin") -> str:
    payload = {
        "sub": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc


def get_current_user(credentials: HTTPAuthorizationCredentials | None) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return decode_token(credentials.credentials)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict[str, Any]:
    """FastAPI dependency that rejects unauthenticated resource requests."""
    claims = get_current_user(credentials)
    user = UserStore.get_by_email(str(claims.get("sub", "")))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return {**claims, "id": user["id"], "role": user["role"]}
