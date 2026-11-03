"""
Database configuration for the social media scheduler.

Handles the SQLite connection and creates / upgrades all tables.
Import `get_connection()` and `init_db()` from anywhere in the project.

Post status flow:
    draft -> approved -> published
    draft -> scheduled (approved with a time) -> published
    draft -> rejected
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

POST_STATUSES = ("draft", "approved", "scheduled", "published", "rejected")

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
    image_path         TEXT,
    jpeg_path          TEXT,
    hosted_at          TEXT,
    host_ref           TEXT,
    video_url          TEXT,
    fb_post_id         TEXT,
    ig_post_id         TEXT,
    tiktok_publish_id  TEXT,
    last_error         TEXT,
    published_at       TEXT,
    approved_at        TEXT,
    claimed_at         TEXT,
    created_at         TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

# Columns added after the first version of the table. init_db() adds any that
# are missing, so an older scheduler.db keeps working without being recreated.
EXTRA_COLUMNS = {
    "image_url": "TEXT",
    "image_path": "TEXT",
    "jpeg_path": "TEXT",
    "hosted_at": "TEXT",
    "host_ref": "TEXT",
    "video_url": "TEXT",
    "fb_post_id": "TEXT",
    "ig_post_id": "TEXT",
    "tiktok_publish_id": "TEXT",
    "last_error": "TEXT",
    "published_at": "TEXT",
    "approved_at": "TEXT",
    "claimed_at": "TEXT",
    "created_at": "TEXT",
}

# Indexes for the queries the dashboard and publisher run most.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)",
    "CREATE INDEX IF NOT EXISTS idx_posts_scheduled ON posts(status, scheduled_time)",
    "CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_error ON posts(last_error) WHERE last_error IS NOT NULL",
)

# How long a single connection waits for a lock before giving up.
BUSY_TIMEOUT_SECONDS = 30

# Encrypted API tokens entered through the dashboard (see src/security).
CREATE_CREDENTIALS_TABLE = """
CREATE TABLE IF NOT EXISTS credentials (
    name             TEXT PRIMARY KEY,
    value_encrypted  TEXT NOT NULL,
    updated_at       TEXT
);
"""

# One row per orchestrator run, with the captured log (see src/main.py).
CREATE_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT,
    finished_at  TEXT,
    steps        TEXT,
    ok           INTEGER,
    summary      TEXT,
    log          TEXT
);
"""


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
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False)
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
    """Create the database and all tables, and add any missing columns."""
    with get_connection() as conn:
        # WAL lets readers and one writer work at the same time instead of
        # blocking each other (the dashboard reads while the loop writes).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(CREATE_POSTS_TABLE)
        conn.execute(CREATE_CREDENTIALS_TABLE)
        conn.execute(CREATE_RUNS_TABLE)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
        for name, col_type in EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {col_type}")
        for sql in INDEXES:
            conn.execute(sql)


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at: {DB_PATH}")
