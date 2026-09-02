#!/usr/bin/env python
"""Check what tables exist in the database."""

import sqlite3
from pathlib import Path

db_path = Path("storage/ai_interviewer.db")

if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    print(f"Existing tables in {db_path}: {tables}")
else:
    print(f"Database file does not exist: {db_path}")
