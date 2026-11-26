"""Interactively create the first platform administrator on a PostgreSQL deployment."""

from __future__ import annotations

import getpass
import re
import sys
from datetime import UTC, datetime

from sqlalchemy import select, text

from services.database import SessionLocal, UserRecord
from services.security_utils import hash_password

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BOOTSTRAP_LOCK_ID = 811_240_915


def main() -> int:
    email = input("Platform administrator email: ").strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        print("A valid email address is required.", file=sys.stderr)
        return 2
    password = getpass.getpass("Password (minimum 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < 12 or password != confirmation:
        print("Passwords must match and contain at least 12 characters.", file=sys.stderr)
        return 2
    if input("Confirm you have verified control of this mailbox (yes/no): ").strip().lower() != "yes":
        print("Mailbox verification was not confirmed; no administrator was created.", file=sys.stderr)
        return 2

    with SessionLocal.begin() as session:
        if session.get_bind().dialect.name != "postgresql":
            print("Initial administrator provisioning requires PostgreSQL.", file=sys.stderr)
            return 2
        session.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": BOOTSTRAP_LOCK_ID})
        if session.execute(
            select(UserRecord.id).where(UserRecord.role == "admin").limit(1)
        ).scalar_one_or_none() is not None:
            print("A platform administrator already exists; refusing to create another.", file=sys.stderr)
            return 2
        existing = session.execute(
            select(UserRecord.id).where(UserRecord.email == email)
        ).scalar_one_or_none()
        if existing is not None:
            print("That email already belongs to an account; no role was changed.", file=sys.stderr)
            return 2
        session.add(
            UserRecord(
                email=email,
                password_hash=hash_password(password),
                role="admin",
                email_verified_at=datetime.now(UTC),
            )
        )
    print("Initial platform administrator created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
