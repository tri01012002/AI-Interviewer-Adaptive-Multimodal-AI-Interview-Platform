"""Private local storage for candidate CVs in development environments."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from config import settings


class CandidateCVStorage:
    @staticmethod
    def _root() -> Path:
        if settings.STORAGE_TYPE != "local":
            raise RuntimeError(
                "Candidate CV storage currently supports only STORAGE_TYPE=local"
            )
        configured = Path(settings.CV_STORAGE_PATH)
        if not configured.is_absolute():
            configured = Path(__file__).resolve().parents[1] / configured
        if configured.is_symlink():
            raise OSError("CV storage root must not be a symbolic link")
        configured.mkdir(parents=True, exist_ok=True)
        root = configured.resolve(strict=True)
        if os.name != "nt":
            root.chmod(0o700)
        return root

    @classmethod
    def write(cls, content: bytes, extension: str) -> str:
        if extension not in {".pdf", ".docx"}:
            raise ValueError("Unsupported CV storage extension")
        storage_key = f"{uuid4().hex}{extension}"
        destination = cls._root() / storage_key
        if destination.parent != cls._root():
            raise OSError("Invalid private CV storage location")
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stored_file:
            stored_file.write(content)
        return storage_key

    @classmethod
    def read(cls, storage_key: str) -> bytes:
        if Path(storage_key).name != storage_key or not storage_key.endswith(
            (".pdf", ".docx")
        ):
            raise OSError("Invalid private CV storage key")
        path = cls._root() / storage_key
        if path.parent != cls._root() or path.is_symlink():
            raise OSError("Invalid private CV storage location")
        return path.read_bytes()

    @classmethod
    def delete(cls, storage_key: str) -> None:
        if Path(storage_key).name != storage_key:
            raise OSError("Invalid private CV storage key")
        path = cls._root() / storage_key
        if path.parent != cls._root() or path.is_symlink():
            raise OSError("Invalid private CV storage location")
        path.unlink(missing_ok=True)
