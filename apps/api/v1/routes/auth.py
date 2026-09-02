"""Authentication routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.auth_service import authenticate_user, get_current_user, issue_token, register_user, security

router = APIRouter()


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=3)


class RegisterRequest(LoginRequest):
    role: str = "candidate"


@router.post("/auth/register", tags=["Auth"])
async def register(payload: RegisterRequest):
    if not payload.email or "@" not in payload.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is required")
    if payload.password == "" or len(payload.password) < 3:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password is invalid")
    user = register_user(payload.email, payload.password, payload.role)
    return {"message": "User registered successfully", "user": {"email": user["email"], "role": user["role"]}}


@router.post("/auth/login", tags=["Auth"])
async def login(payload: LoginRequest):
    if not payload.email or "@" not in payload.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is required")
    if payload.password == "" or len(payload.password) < 3:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password is invalid")

    user = authenticate_user(payload.email, payload.password)
    return {
        "access_token": issue_token(user["email"], user["role"]),
        "token_type": "bearer",
        "user": {"email": user["email"], "role": user["role"]},
    }


@router.get("/auth/me", tags=["Auth"])
async def auth_me(credentials=Depends(security)):
    user = get_current_user(credentials)
    return {"authenticated": True, "user": {"email": user.get("sub"), "role": user.get("role", "admin")}}
