"""Minimal persistence layer for interview state using SQLite."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import settings


class InterviewStore:
    """Store interview states in a SQLite database as JSON documents."""

    def __init__(self, db_path: str | None = None) -> None:
        default_path = Path(settings.STORAGE_PATH) / "interviews.db"
        self.db_path = Path(db_path or str(default_path))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS interviews (
                    interview_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    position TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def create(self, candidate_id: str, position: str, mode: str, state: dict[str, Any]) -> str:
        interview_id = str(uuid4())
        created_at = updated_at = state.get("created_at") or __import__("datetime").datetime.utcnow().isoformat()
        state["interview_id"] = interview_id
        state["created_at"] = created_at
        state["updated_at"] = updated_at
        state["mode"] = mode

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO interviews (interview_id, candidate_id, position, mode, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (interview_id, candidate_id, position, mode, json.dumps(state), created_at, updated_at),
            )
            connection.commit()
        return interview_id

    def get(self, interview_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT state FROM interviews WHERE interview_id = ?",
                (interview_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def save(self, interview_id: str, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = __import__("datetime").datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE interviews SET state = ?, updated_at = ? WHERE interview_id = ?",
                (json.dumps(state), state["updated_at"], interview_id),
            )
            connection.commit()
        return state

    def delete(self, interview_id: str) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DELETE FROM interviews WHERE interview_id = ?", (interview_id,))
            connection.commit()

    def list(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT state FROM interviews ORDER BY created_at DESC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
