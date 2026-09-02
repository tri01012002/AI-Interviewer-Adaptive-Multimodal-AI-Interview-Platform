"""User store for auth and role management."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from services.database import SessionLocal, UserRecord
from services.security_utils import hash_password


class UserStore:
    @classmethod
    def create(cls, email: str, password: str, role: str = "admin") -> dict[str, Any]:
        with SessionLocal() as session:
            existing = session.execute(select(UserRecord).where(UserRecord.email == email)).scalar_one_or_none()
            if existing is not None:
                return {"email": existing.email, "role": existing.role, "password_hash": existing.password_hash}
            user = UserRecord(email=email.lower(), password_hash=hash_password(password), role=role)
            session.add(user)
            session.commit()
            session.refresh(user)
            return {"id": user.id, "email": user.email, "role": user.role, "password_hash": user.password_hash}

    @classmethod
    def get_by_email(cls, email: str) -> dict[str, Any] | None:
        with SessionLocal() as session:
            user = session.execute(select(UserRecord).where(UserRecord.email == email.lower())).scalar_one_or_none()
            if user is None:
                return None
            return {"id": user.id, "email": user.email, "role": user.role, "password_hash": user.password_hash}

    @classmethod
    def ensure_default_admin(cls, email: str = "admin@example.com", password: str = "secret123") -> dict[str, Any]:
        user = cls.get_by_email(email)
        if user is not None:
            return user
        return cls.create(email=email, password=password, role="admin")
