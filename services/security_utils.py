"""Shared password hashing and verification helpers."""

from __future__ import annotations

import hashlib
import os


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + password_hash.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    salt_hex, hash_hex = stored_hash.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    expected_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return expected_hash.hex() == hash_hex
