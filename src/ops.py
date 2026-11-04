"""
Operational odds and ends that keep a non-technical user out of trouble:

- heartbeat: the worker loop records when it last ran; the dashboard warns
  when it has not been seen for a while ("is the worker running?")
- backups: a daily copy of the SQLite database into data/backups/ (14 kept)
- token health: checks every connected Facebook Page token with Meta and
  records when it expires, so the dashboard can warn before posting fails
"""

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from config.db import BACKEND, DATA_DIR, DB_PATH, get_connection

log = logging.getLogger("ops")
BACKUP_DIR = DATA_DIR / "backups"
KEEP_BACKUPS = 14
HEARTBEAT_STALE_MINUTES = 15


def set_state(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def get_state(key: str) -> tuple[str | None, str | None]:
    with get_connection() as conn:
        row = conn.execute("SELECT value, updated_at FROM app_state WHERE key = ?", (key,)).fetchone()
    return (row["value"], row["updated_at"]) if row else (None, None)


def heartbeat(what: str = "loop") -> None:
    set_state("worker_heartbeat", what)


def worker_status() -> dict:
    """{'seen': datetime|None, 'minutes_ago': int|None, 'stale': bool}."""
    _, when = get_state("worker_heartbeat")
    if not when:
        return {"seen": None, "minutes_ago": None, "stale": True}
    seen = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
    minutes = int((datetime.now() - seen).total_seconds() // 60)
    return {"seen": seen, "minutes_ago": minutes, "stale": minutes > HEARTBEAT_STALE_MINUTES}


def backup_database() -> Path | None:
    """Copy the SQLite file (WAL checkpointed first). PostgreSQL users back up with pg_dump."""
    if BACKEND != "sqlite" or not Path(DB_PATH).exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    stamp = datetime.now().strftime("%Y%m%d")
    dest = BACKUP_DIR / f"scheduler-{stamp}.db"
    if dest.exists():
        return dest
    shutil.copy2(DB_PATH, dest)
    old = sorted(BACKUP_DIR.glob("scheduler-*.db"))[:-KEEP_BACKUPS]
    for path in old:
        path.unlink(missing_ok=True)
    log.info("Database backed up to %s", dest)
    return dest


def run_daily() -> int:
    """Pipeline step: once a day, back up and check page tokens."""
    today = datetime.now().strftime("%Y-%m-%d")
    last, _ = get_state("ops_last_run")
    if last == today:
        return 0
    try:
        backup_database()
    except Exception as exc:  # noqa: BLE001
        log.warning("Backup failed: %s", exc)
    try:
        from src import pages
        pages.check_all_tokens()
    except Exception as exc:  # noqa: BLE001
        log.warning("Token check failed: %s", exc)
    set_state("ops_last_run", today)
    return 0


def expiring_pages(user_id: int, within_days: int = 10) -> list[dict]:
    """Pages whose token is invalid or expires soon, for the dashboard warning."""
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, token_status, token_expires_at FROM pages WHERE user_id = ?", (user_id,))]
    soon = (datetime.now() + timedelta(days=within_days)).strftime("%Y-%m-%d")
    return [r for r in rows if r["token_status"] == "invalid"
            or (r["token_expires_at"] and r["token_expires_at"][:10] <= soon)]
