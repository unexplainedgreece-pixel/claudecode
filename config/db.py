"""
Database configuration for the social media scheduler.

Handles the SQLite connection and creates the `posts` table if it does
not exist yet. Import `get_connection()` from anywhere in the project.
"""

import os
import sqlite3
from pathlib import Path

# Project root = one level above this `config/` folder.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Allow overriding the database path with an environment variable.
DB_PATH = Path(os.getenv("SCHEDULER_DB_PATH", DATA_DIR / "scheduler.db"))

CREATE_POSTS_TABLE = """
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY,
    topic           TEXT,
    caption         TEXT,
    hashtags        TEXT,
    image_prompt    TEXT,
    status          TEXT DEFAULT 'draft',
    scheduled_time  TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    """
    Open a connection to the SQLite database.

    Rows are returned as sqlite3.Row objects, so columns can be accessed
    by name (row["caption"]) as well as by index.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the database file and the `posts` table if they are missing."""
    with get_connection() as conn:
        conn.execute(CREATE_POSTS_TABLE)


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")
