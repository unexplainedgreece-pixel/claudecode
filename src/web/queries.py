"""
Read/write helpers the dashboard uses. All SQL for the web UI lives here so
app.py stays about routes and templates.
"""

import calendar as cal
from datetime import datetime

from config.db import POST_STATUSES, get_connection

EDITABLE_FIELDS = ("topic", "caption", "hashtags", "image_prompt", "scheduled_time",
                   "image_url", "video_url")


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> dict:
    with get_connection() as conn:
        by_status = {s: 0 for s in POST_STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM posts GROUP BY status"):
            by_status[r["status"]] = r["n"]
        failed = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE last_error IS NOT NULL AND last_error != '' "
            "AND status != 'published'").fetchone()[0]
        awaiting_image = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status = 'draft' AND (image_path IS NULL OR image_path = '')"
        ).fetchone()[0]
        awaiting_hosting = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE image_path IS NOT NULL AND image_path != '' "
            "AND (hosted_at IS NULL OR hosted_at = '') AND status != 'published'").fetchone()[0]
        due = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status = 'approved' OR "
            "(status = 'scheduled' AND scheduled_time <= ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M"),)).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        last_run = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1").fetchone()
        runs_failed = conn.execute("SELECT COUNT(*) FROM pipeline_runs WHERE ok = 0").fetchone()[0]
    return {
        **by_status,
        "failed": failed,
        "awaiting_image": awaiting_image,
        "awaiting_hosting": awaiting_hosting,
        "due_now": due,
        "total": total,
        "last_run": dict(last_run) if last_run else None,
        "runs_failed": runs_failed,
    }


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

def list_posts(status: str | None = None, limit: int = 200, search: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM posts", []
    where = []
    if status:
        where.append("status = ?")
        params.append(status)
    if search:
        where.append("(topic LIKE ? OR caption LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        return _rows(conn.execute(sql, params))


def get_post(post_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return dict(row) if row else None


def create_draft(topic: str, caption: str = "", hashtags: str = "", image_prompt: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO posts (topic, caption, hashtags, image_prompt, status) "
            "VALUES (?, ?, ?, ?, 'draft')", (topic, caption, hashtags, image_prompt))
        return cur.lastrowid


def update_post(post_id: int, **fields) -> None:
    clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()
             if k in EDITABLE_FIELDS}
    if not clean:
        return
    if "scheduled_time" in clean:
        clean["scheduled_time"] = normalize_datetime(clean["scheduled_time"])
    sets = ", ".join(f"{k} = ?" for k in clean)
    with get_connection() as conn:
        conn.execute(f"UPDATE posts SET {sets} WHERE id = ?", (*clean.values(), post_id))


def delete_post(post_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))


def normalize_datetime(value: str | None) -> str | None:
    """Accept 'YYYY-MM-DDTHH:MM' (HTML datetime-local) or 'YYYY-MM-DD HH:MM'."""
    value = (value or "").strip()
    if not value:
        return None
    value = value.replace("T", " ")[:16]
    datetime.strptime(value, "%Y-%m-%d %H:%M")  # raises ValueError if malformed
    return value


def set_status(post_id: int, status: str, scheduled_time: str | None = None) -> None:
    """
    Move a post through the workflow.
      approved  : publish on the next pipeline run (or at scheduled_time if given,
                  which stores it as 'scheduled' instead)
      draft     : un-approve
      rejected  : never publish
    """
    if status not in POST_STATUSES or status == "published":
        raise ValueError(f"Cannot set status to {status!r}")
    scheduled = normalize_datetime(scheduled_time)
    if status == "approved" and scheduled:
        status = "approved" if scheduled <= datetime.now().strftime("%Y-%m-%d %H:%M") else "scheduled"
    if status == "scheduled" and not scheduled:
        raise ValueError("A scheduled post needs a scheduled_time.")

    with get_connection() as conn:
        if status in ("approved", "scheduled"):
            conn.execute(
                "UPDATE posts SET status = ?, scheduled_time = ?, approved_at = ?, last_error = NULL "
                "WHERE id = ?", (status, scheduled, _now(), post_id))
        else:
            conn.execute("UPDATE posts SET status = ?, approved_at = NULL WHERE id = ?",
                         (status, post_id))


def clear_error(post_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = NULL WHERE id = ?", (post_id,))


def queue() -> dict:
    """Posts grouped for the approval page."""
    with get_connection() as conn:
        drafts = _rows(conn.execute(
            "SELECT * FROM posts WHERE status = 'draft' "
            "ORDER BY (hosted_at IS NOT NULL AND hosted_at != '') DESC, id DESC"))
        waiting = _rows(conn.execute(
            "SELECT * FROM posts WHERE status IN ('approved', 'scheduled') "
            "ORDER BY scheduled_time IS NULL DESC, scheduled_time, id"))
        rejected = _rows(conn.execute(
            "SELECT * FROM posts WHERE status = 'rejected' ORDER BY id DESC LIMIT 20"))
    for p in drafts:
        p["ready"] = bool(p["hosted_at"])
    return {"drafts": drafts, "waiting": waiting, "rejected": rejected}


# ---------------------------------------------------------------------------
# Errors and runs
# ---------------------------------------------------------------------------

def list_errors(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        return _rows(conn.execute(
            "SELECT id, topic, status, last_error, image_url, hosted_at, fb_post_id, ig_post_id, "
            "tiktok_publish_id FROM posts WHERE last_error IS NOT NULL AND last_error != '' "
            "ORDER BY id DESC LIMIT ?", (limit,)))


def list_runs(limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        return _rows(conn.execute(
            "SELECT id, started_at, finished_at, steps, ok, summary FROM pipeline_runs "
            "ORDER BY id DESC LIMIT ?", (limit,)))


def get_run(run_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def calendar_month(year: int, month: int) -> dict:
    """
    A month grid (weeks of 7 day-cells, Monday first) with the posts on each day.
    Scheduled/approved posts appear on scheduled_time, published posts on published_at.
    """
    first = f"{year:04d}-{month:02d}-01"
    last_day = cal.monthrange(year, month)[1]
    last = f"{year:04d}-{month:02d}-{last_day:02d} 23:59"
    with get_connection() as conn:
        rows = _rows(conn.execute(
            """
            SELECT id, topic, status, scheduled_time, published_at, image_url, jpeg_path,
                   CASE WHEN status = 'published' THEN published_at ELSE scheduled_time END AS day_key
            FROM posts
            WHERE day_key >= ? AND day_key <= ?
            ORDER BY day_key
            """, (first, last)))

    by_day: dict[int, list] = {}
    for r in rows:
        day = int(r["day_key"][8:10])
        r["time"] = r["day_key"][11:16]
        by_day.setdefault(day, []).append(r)

    today = datetime.now()
    weeks = []
    for week in cal.monthcalendar(year, month):
        weeks.append([
            {"day": d, "posts": by_day.get(d, []),
             "today": d and (today.year, today.month, today.day) == (year, month, d)}
            for d in week
        ])
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return {
        "year": year, "month": month, "month_name": cal.month_name[month],
        "weeks": weeks, "count": len(rows),
        "prev": {"year": prev_y, "month": prev_m}, "next": {"year": next_y, "month": next_m},
    }
