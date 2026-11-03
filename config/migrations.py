"""
Schema migrations, applied in order by init_db() on both SQLite and PostgreSQL.

Each migration is a function (conn) -> None that must be safe to re-run.
Applied versions are recorded in `schema_migrations`.

    python3 -m config.migrate status     # what is applied / pending
    python3 -m config.migrate upgrade    # apply pending (init_db does this too)
    python3 -m config.migrate sql        # print the DDL for the active backend
"""

from datetime import datetime

from config.db import Connection, add_column_if_missing, table_columns, table_exists


def _id(conn: Connection) -> str:
    return "SERIAL PRIMARY KEY" if conn.backend == "postgres" else "INTEGER PRIMARY KEY"


def _now_default(conn: Connection) -> str:
    if conn.backend == "postgres":
        return "DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')"
    return "DEFAULT (datetime('now', 'localtime'))"


# ---------------------------------------------------------------------------
# 001: everything the single-user tool had
# ---------------------------------------------------------------------------

POST_COLUMNS = {
    "topic": "TEXT", "caption": "TEXT", "hashtags": "TEXT", "image_prompt": "TEXT",
    "status": "TEXT DEFAULT 'draft'", "scheduled_time": "TEXT",
    "image_url": "TEXT", "image_path": "TEXT", "jpeg_path": "TEXT", "hosted_at": "TEXT",
    "host_ref": "TEXT", "video_url": "TEXT", "fb_post_id": "TEXT", "ig_post_id": "TEXT",
    "tiktok_publish_id": "TEXT", "last_error": "TEXT", "published_at": "TEXT",
    "approved_at": "TEXT", "claimed_at": "TEXT", "media_type": "TEXT DEFAULT 'image'",
    "audio_path": "TEXT", "video_path": "TEXT",
}

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)",
    "CREATE INDEX IF NOT EXISTS idx_posts_scheduled ON posts(status, scheduled_time)",
    "CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_posts_error ON posts(last_error) WHERE last_error IS NOT NULL",
)


def m001_baseline(conn: Connection) -> None:
    cols = ",\n    ".join(f"{name} {definition}" for name, definition in POST_COLUMNS.items())
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS posts (
            id {_id(conn)},
            {cols},
            created_at TEXT {_now_default(conn)}
        )""")
    for name, definition in POST_COLUMNS.items():        # older SQLite files
        add_column_if_missing(conn, "posts", name, definition)
    add_column_if_missing(conn, "posts", "created_at", "TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            name TEXT PRIMARY KEY,
            value_encrypted TEXT NOT NULL,
            updated_at TEXT
        )""")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id {_id(conn)},
            started_at TEXT, finished_at TEXT, steps TEXT, ok INTEGER, summary TEXT, log TEXT
        )""")
    for sql in INDEXES:
        conn.execute(sql)


# ---------------------------------------------------------------------------
# 002: users, tenancy, uploads, billing
# ---------------------------------------------------------------------------

def m002_tenancy(conn: Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {_id(conn)},
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            tier TEXT NOT NULL DEFAULT 'free',
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT {_now_default(conn)},
            last_login_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")

    add_column_if_missing(conn, "posts", "user_id", "INTEGER")
    add_column_if_missing(conn, "posts", "source", "TEXT DEFAULT 'ai'")
    add_column_if_missing(conn, "posts", "upload_name", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_user_created ON posts(user_id, created_at)")

    add_column_if_missing(conn, "pipeline_runs", "user_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON pipeline_runs(user_id, id)")

    # credentials: primary key becomes (user_id, name). user_id 0 = platform-wide
    # defaults set by the operator; rows from the single-user era move there.
    if "user_id" not in table_columns(conn, "credentials"):
        conn.execute("ALTER TABLE credentials RENAME TO credentials_v1")
        conn.execute("""
            CREATE TABLE credentials (
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                value_encrypted TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (user_id, name)
            )""")
        conn.execute("INSERT INTO credentials (user_id, name, value_encrypted, updated_at) "
                     "SELECT 0, name, value_encrypted, updated_at FROM credentials_v1")
        conn.execute("DROP TABLE credentials_v1")

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS billing_events (
            id {_id(conn)},
            user_id INTEGER,
            stripe_event_id TEXT UNIQUE,
            event_type TEXT,
            tier TEXT,
            received_at TEXT {_now_default(conn)}
        )""")


MIGRATIONS = [
    (1, "baseline", m001_baseline),
    (2, "users_tenancy_uploads_billing", m002_tenancy),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _ensure_table(conn: Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT,
            applied_at TEXT
        )""")


def applied_versions(conn: Connection) -> set[int]:
    _ensure_table(conn)
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}


def apply_pending(conn: Connection) -> list[int]:
    """Apply every migration not yet recorded. Returns the versions applied."""
    done = applied_versions(conn)
    # A database created before this runner existed already has the baseline.
    if 1 not in done and table_exists(conn, "posts"):
        m001_baseline(conn)   # idempotent: fills in any columns an old file lacks
        conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                     (1, "baseline", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        done.add(1)
    applied = []
    for version, name, fn in MIGRATIONS:
        if version in done:
            continue
        fn(conn)
        conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                     (version, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        applied.append(version)
    return applied
