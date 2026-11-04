"""
Database layer: SQLite by default, PostgreSQL when DATABASE_URL is set.

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (pid,)).fetchone()
        new_id = conn.execute("INSERT INTO posts (topic) VALUES (?)", ("x",)).lastrowid

Write SQL once, with '?' placeholders, and it runs on both backends:
  * placeholders are translated to %s for PostgreSQL
  * rows come back as plain dicts on both backends
  * INSERTs into tables with an `id` column get `RETURNING id` on PostgreSQL
    so `.lastrowid` works everywhere
Schema changes live in config/migrations.py and are applied by init_db().

Backends:
  SQLite      the default. File at SCHEDULER_DB_PATH (data/scheduler.db).
              WAL mode + 30 s busy timeout; fine for one machine.
  PostgreSQL  set DATABASE_URL=postgresql://user:pass@host:5432/dbname
              (see docker-compose.yml). For several machines or many users.

Post status flow:
    draft -> approved -> published
    draft -> scheduled (approved with a time) -> published
    draft -> rejected
"""

import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

# Project root = one level above this `config/` folder.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DB_PATH = Path(os.getenv("SCHEDULER_DB_PATH", DATA_DIR / "scheduler.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BACKEND = "postgres" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"

POST_STATUSES = ("draft", "approved", "scheduled", "published", "rejected")
# image: a photo post. video: a vertical TikTok / Reel compiled from the image + speech.
MEDIA_TYPES = ("image", "video")
# ai: made by the pipeline. upload: a finished video the user dropped in.
POST_SOURCES = ("ai", "upload", "library")

# How long a SQLite connection waits for a lock before giving up.
BUSY_TIMEOUT_SECONDS = 30

# Tables whose INSERTs should return the new id as .lastrowid.
TABLES_WITH_ID = ("posts", "pipeline_runs", "users", "pages", "billing_events", "assets", "post_metrics",
                  "review_links")
# ai: made by the pipeline. upload: a finished file the user dropped in. library: made from the user's media library.
_INSERT_TABLE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Thin wrappers so call sites never see which backend is underneath
# ---------------------------------------------------------------------------

class Cursor:
    def __init__(self, raw, backend: str):
        self._raw = raw
        self._backend = backend
        self.lastrowid = None

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    def fetchone(self):
        row = self._raw.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._raw.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, raw, backend: str):
        self._raw = raw
        self.backend = backend

    @staticmethod
    def _translate(sql: str, backend: str, params) -> tuple[str, object]:
        """
        Accept '?' positional or ':name' named placeholders (the sqlite3 style)
        and return SQL + params in the form the active backend wants.
        """
        if isinstance(params, Mapping):
            if backend == "postgres":
                for key in params:
                    sql = re.sub(rf"(?<![:\w]):{key}\b", f"%({key})s", sql)
            return sql, dict(params)
        if backend == "postgres":
            sql = sql.replace("?", "%s")
        return sql, tuple(params)

    def execute(self, sql: str, params=()) -> Cursor:
        table = _INSERT_TABLE.match(sql)
        wants_id = (self.backend == "postgres" and table and table.group(1).lower() in TABLES_WITH_ID
                    and "returning" not in sql.lower())
        if wants_id:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        sql, params = self._translate(sql, self.backend, params)
        raw = self._raw.execute(sql, params)
        cur = Cursor(raw, self.backend)
        if wants_id:
            cur.lastrowid = raw.fetchone()["id"]
        elif self.backend == "sqlite":
            cur.lastrowid = raw.lastrowid
        return cur

    def executemany(self, sql: str, seq) -> None:
        seq = list(seq)
        if not seq:
            return
        sql, _ = self._translate(sql, self.backend, seq[0])
        self._raw.executemany(sql, [dict(p) if isinstance(p, Mapping) else tuple(p) for p in seq])

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001  (sqlite: unfinalised cursors); the handle is dropped anyway
            pass


def _connect_raw():
    if BACKEND == "postgres":
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_connection():
    """
    Open a connection for use in a `with` block. Changes are committed when
    the block ends normally, rolled back if an error is raised, and the
    connection is always closed afterwards. Rows are plain dicts.
    """
    conn = Connection(_connect_raw(), BACKEND)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema helpers used by the migrations
# ---------------------------------------------------------------------------

def table_columns(conn: Connection, table: str) -> set[str]:
    if conn.backend == "postgres":
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns WHERE table_name = ?",
            (table,)).fetchall()
    else:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def table_exists(conn: Connection, table: str) -> bool:
    if conn.backend == "postgres":
        row = conn.execute("SELECT 1 AS x FROM information_schema.tables WHERE table_name = ?",
                           (table,)).fetchone()
    else:
        row = conn.execute("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (table,)).fetchone()
    return row is not None


def add_column_if_missing(conn: Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    """Create the database if needed and apply any pending migrations."""
    from config.migrations import apply_pending
    with get_connection() as conn:
        if conn.backend == "sqlite":
            # WAL lets readers and one writer work at the same time instead of
            # blocking each other (the dashboard reads while the loop writes).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        apply_pending(conn)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))   # allow `python3 config/db.py` from the project root
    init_db()
    print(f"Database initialized: {'PostgreSQL (' + DATABASE_URL.split('@')[-1] + ')' if BACKEND == 'postgres' else DB_PATH}")
