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


def m003_platforms(conn: Connection) -> None:
    # Comma-separated platforms chosen for the post, e.g. "facebook,instagram".
    # Empty/NULL means "every platform the publisher is asked for".
    add_column_if_missing(conn, "posts", "platforms", "TEXT")


def m004_pages(conn: Connection) -> None:
    # Connected Facebook Pages (with their Instagram account) per user.
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS pages (
            id {_id(conn)},
            user_id INTEGER NOT NULL,
            name TEXT,
            fb_page_id TEXT NOT NULL,
            ig_account_id TEXT,
            ig_username TEXT,
            token_encrypted TEXT NOT NULL,
            created_at TEXT {_now_default(conn)},
            UNIQUE (user_id, fb_page_id)
        )""")
    add_column_if_missing(conn, "posts", "page_id", "INTEGER")


def m005_library(conn: Connection) -> None:
    # The media library: photos and clips the user drops in data/library/user_<id>/.
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS assets (
            id {_id(conn)},
            user_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            fingerprint TEXT,
            size_bytes INTEGER,
            width INTEGER,
            height INTEGER,
            duration REAL,
            description TEXT,
            caption TEXT,
            hashtags TEXT,
            analysis TEXT,
            analyzed_at TEXT,
            analysis_error TEXT,
            times_used INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            excluded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT {_now_default(conn)},
            UNIQUE (user_id, path)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_user ON assets(user_id, kind, excluded)")
    # Brand profile + automatic planning settings on the user.
    for name, definition in (("brand_name", "TEXT"), ("brand_brief", "TEXT"), ("brand_language", "TEXT"),
                             ("posts_per_day", "INTEGER DEFAULT 0"), ("auto_platforms", "TEXT"),
                             ("montage_enabled", "INTEGER DEFAULT 1"), ("last_plan_date", "TEXT")):
        add_column_if_missing(conn, "users", name, definition)
    add_column_if_missing(conn, "posts", "asset_ids", "TEXT")


def m006_queue_metrics(conn: Connection) -> None:
    # Posting slots, first-comment hashtags, evergreen recycling, notifications, stock credits.
    for name, definition in (("posting_slots", "TEXT"), ("hashtags_as_comment", "INTEGER DEFAULT 0"),
                             ("timezone", "TEXT"), ("notify_telegram", "INTEGER DEFAULT 1")):
        add_column_if_missing(conn, "users", name, definition)
    for name, definition in (("evergreen", "INTEGER DEFAULT 0"), ("recycle_days", "INTEGER"),
                             ("recycled_from", "INTEGER"), ("recycled_at", "TEXT"), ("media_credit", "TEXT"),
                             ("fb_comment_id", "TEXT"), ("ig_comment_id", "TEXT")):
        add_column_if_missing(conn, "posts", name, definition)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS post_metrics (
            id {_id(conn)},
            post_id INTEGER NOT NULL,
            user_id INTEGER,
            platform TEXT NOT NULL,
            fetched_at TEXT,
            views INTEGER, reach INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saved INTEGER,
            engagement INTEGER,
            raw TEXT,
            UNIQUE (post_id, platform)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_user ON post_metrics(user_id, platform)")


def m007_more_platforms(conn: Connection) -> None:
    for name, definition in (("threads_post_id", "TEXT"), ("youtube_video_id", "TEXT"),
                             ("subtitled_path", "TEXT"), ("transcript", "TEXT")):
        add_column_if_missing(conn, "posts", name, definition)
    for name, definition in (("content_pillars", "TEXT"), ("auto_subtitles", "INTEGER DEFAULT 0")):
        add_column_if_missing(conn, "users", name, definition)


def m008_ops(conn: Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )""")
    add_column_if_missing(conn, "posts", "publish_attempts", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "posts", "also_story", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "posts", "ig_story_id", "TEXT")
    add_column_if_missing(conn, "pages", "token_checked_at", "TEXT")
    add_column_if_missing(conn, "pages", "token_status", "TEXT")
    add_column_if_missing(conn, "pages", "token_expires_at", "TEXT")


def m009_review_links(conn: Connection) -> None:
    """Client review links: a client approves or asks for changes from a private
    link, without an account."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS review_links (
            id {_id(conn)},
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            label TEXT,
            page_id INTEGER,
            created_at TEXT,
            expires_at TEXT,
            revoked INTEGER DEFAULT 0,
            last_used_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_links_user ON review_links (user_id)")
    add_column_if_missing(conn, "posts", "client_decision", "TEXT")
    add_column_if_missing(conn, "posts", "client_note", "TEXT")
    add_column_if_missing(conn, "posts", "client_decided_at", "TEXT")


def m010_carousels(conn: Connection) -> None:
    """Carousel posts: several photos in one Instagram/Facebook post."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS post_media (
            id {_id(conn)},
            post_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            image_path TEXT,
            jpeg_path TEXT,
            image_url TEXT,
            host_ref TEXT,
            hosted_at TEXT,
            asset_id INTEGER,
            credit TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_media_post ON post_media (post_id, position)")


MIGRATIONS = [
    (1, "baseline", m001_baseline),
    (2, "users_tenancy_uploads_billing", m002_tenancy),
    (3, "post_platforms", m003_platforms),
    (4, "connected_pages", m004_pages),
    (5, "media_library_and_brand", m005_library),
    (6, "queue_slots_metrics_recycling", m006_queue_metrics),
    (7, "threads_youtube_subtitles_pillars", m007_more_platforms),
    (8, "ops_heartbeat_attempts_story_tokencheck", m008_ops),
    (9, "client_review_links", m009_review_links),
    (10, "carousel_post_media", m010_carousels),
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
