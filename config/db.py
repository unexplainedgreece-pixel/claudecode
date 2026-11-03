"""
Database configuration for the social media scheduler.

Handles the SQLite connection and creates / upgrades the `posts` table.
Import `get_connection()` and `init_db()` from anywhere in the project.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Project root = one level above this `config/` folder.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Allow overriding the database path with an environment variable.
DB_PATH = Path(os.getenv("SCHEDULER_DB_PATH", DATA_DIR / "scheduler.db"))

CREATE_POSTS_TABLE = """
CREATE TABLE IF NOT EXISTS posts (
    id                 INTEGER PRIMARY KEY,
    topic              TEXT,
    caption            TEXT,
    hashtags           TEXT,
    image_prompt       TEXT,
    status             TEXT DEFAULT 'draft',
    scheduled_time     TEXT,
    image_url          TEXT,
    video_url          TEXT,
    fb_post_id         TEXT,
    ig_post_id         TEXT,
    tiktok_publish_id  TEXT,
    last_error         TEXT,
    published_at       TEXT
);
"""

# Columns added after the first version of the table. init_db() adds any that
# are missing, so an older scheduler.db keeps working without being recreated.
EXTRA_COLUMNS = {
    "image_url": "TEXT",
    "video_url": "TEXT",
    "fb_post_id": "TEXT",
    "ig_post_id": "TEXT",
    "tiktok_publish_id": "TEXT",
    "last_error": "TEXT",
    "published_at": "TEXT",
}


@contextmanager
def get_connection():
    """
    Open a connection to the SQLite database for use in a `with` block:

        with get_connection() as conn:
            conn.execute(...)

    Changes are committed when the block ends normally, rolled back if an
    error is raised, and the connection is always closed afterwards.
    Rows are returned as sqlite3.Row objects, so columns can be accessed
    by name (row["caption"]) as well as by index.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the database and the `posts` table, and add any missing columns."""
    with get_connection() as conn:
        conn.execute(CREATE_POSTS_TABLE)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
        for name, col_type in EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {col_type}")


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")
