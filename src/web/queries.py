"""
Read/write helpers the dashboard uses. All SQL for the web UI lives here so
app.py stays about routes and templates.
"""

import calendar as cal
from datetime import datetime

from config.db import MEDIA_TYPES, POST_SOURCES, POST_STATUSES, get_connection

EDITABLE_FIELDS = ("topic", "caption", "hashtags", "image_prompt", "scheduled_time",
                   "image_url", "video_url", "media_type", "platforms")


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _count(conn, where: str, params=()) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM posts WHERE {where}", params).fetchone()["n"]


def _scope(user_id: int | None, column: str = "user_id") -> tuple[str, tuple]:
    """SQL fragment + params restricting to one user. None = no restriction (operator/CLI)."""
    return (f" AND {column} = ?", (user_id,)) if user_id is not None else ("", ())


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats(user_id: int | None = None) -> dict:
    sc, sp = _scope(user_id)
    with get_connection() as conn:
        by_status = {s: 0 for s in POST_STATUSES}
        for r in conn.execute(f"SELECT status, COUNT(*) AS n FROM posts WHERE 1 = 1{sc} GROUP BY status", sp):
            by_status[r["status"]] = r["n"]
        failed = _count(conn, f"last_error IS NOT NULL AND last_error != '' AND status != 'published'{sc}", sp)
        awaiting_image = _count(conn, f"status = 'draft' AND (image_path IS NULL OR image_path = '') "
                                      f"AND COALESCE(source, 'ai') = 'ai'{sc}", sp)
        videos_pending = _count(conn, f"media_type = 'video' AND (video_path IS NULL OR video_path = '') "
                                      f"AND status NOT IN ('published', 'rejected'){sc}", sp)
        awaiting_hosting = _count(conn, f"((image_path IS NOT NULL AND image_path != '') OR "
                                        f"(video_path IS NOT NULL AND video_path != '')) "
                                        f"AND (hosted_at IS NULL OR hosted_at = '') AND status != 'published'{sc}", sp)
        due = _count(conn, f"(status = 'approved' OR (status = 'scheduled' AND scheduled_time <= ?)){sc}",
                     (datetime.now().strftime("%Y-%m-%d %H:%M"), *sp))
        total = _count(conn, f"1 = 1{sc}", sp)
        last_run = conn.execute(
            f"SELECT * FROM pipeline_runs WHERE 1 = 1{sc} ORDER BY id DESC LIMIT 1", sp).fetchone()
        runs_failed = conn.execute(
            f"SELECT COUNT(*) AS n FROM pipeline_runs WHERE ok = 0{sc}", sp).fetchone()["n"]
    return {
        **by_status,
        "failed": failed,
        "awaiting_image": awaiting_image,
        "awaiting_hosting": awaiting_hosting,
        "videos_pending": videos_pending,
        "due_now": due,
        "total": total,
        "last_run": dict(last_run) if last_run else None,
        "runs_failed": runs_failed,
    }


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

def list_posts(status: str | None = None, limit: int = 200, search: str | None = None,
               user_id: int | None = None) -> list[dict]:
    sql, params = "SELECT * FROM posts", []
    where = []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
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


def get_post(post_id: int, user_id: int | None = None) -> dict | None:
    """The post, or None if it does not exist or belongs to someone else."""
    sc, sp = _scope(user_id)
    with get_connection() as conn:
        row = conn.execute(f"SELECT * FROM posts WHERE id = ?{sc}", (post_id, *sp)).fetchone()
    return dict(row) if row else None


def platforms_text(value) -> str | None:
    """Normalise a platform choice (list or comma string) to 'facebook,instagram' or None."""
    from src.publisher import parse_platforms
    chosen = parse_platforms(value)
    return ",".join(chosen) if chosen else None


def create_draft(topic: str, caption: str = "", hashtags: str = "", image_prompt: str = "",
                 media_type: str = "image", user_id: int | None = None, source: str = "ai",
                 video_path: str | None = None, upload_name: str | None = None,
                 platforms=None) -> int:
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"media_type must be one of {MEDIA_TYPES}")
    if source not in POST_SOURCES:
        raise ValueError(f"source must be one of {POST_SOURCES}")
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO posts (topic, caption, hashtags, image_prompt, status, media_type, user_id, "
            "source, video_path, upload_name, platforms) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
            (topic, caption, hashtags, image_prompt, media_type, user_id, source, video_path, upload_name,
             platforms_text(platforms)))
        return cur.lastrowid


def update_post(post_id: int, **fields) -> None:
    clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()
             if k in EDITABLE_FIELDS}
    if not clean:
        return
    if "scheduled_time" in clean:
        clean["scheduled_time"] = normalize_datetime(clean["scheduled_time"])
    if "media_type" in clean and clean["media_type"] not in MEDIA_TYPES:
        raise ValueError(f"media_type must be one of {MEDIA_TYPES}")
    if "platforms" in clean:
        clean["platforms"] = platforms_text(clean["platforms"])
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


def set_status(post_id: int, status: str, scheduled_time: str | None = None, platforms=None) -> None:
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
            if platforms is not None:
                conn.execute("UPDATE posts SET platforms = ? WHERE id = ?",
                             (platforms_text(platforms), post_id))
        else:
            conn.execute("UPDATE posts SET status = ?, approved_at = NULL WHERE id = ?",
                         (status, post_id))


def clear_error(post_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = NULL WHERE id = ?", (post_id,))


QUEUE_PAGE_SIZE = 25


def queue(page: int = 1, per_page: int = QUEUE_PAGE_SIZE, user_id: int | None = None) -> dict:
    """Posts grouped for the approval page, paged so thousands of drafts stay fast."""
    page = max(1, page)
    offset = (page - 1) * per_page
    sc, sp = _scope(user_id)
    with get_connection() as conn:
        total_drafts = _count(conn, f"status = 'draft'{sc}", sp)
        total_waiting = _count(conn, f"status IN ('approved', 'scheduled'){sc}", sp)
        drafts = _rows(conn.execute(
            f"SELECT * FROM posts WHERE status = 'draft'{sc} "
            "ORDER BY (hosted_at IS NOT NULL AND hosted_at != '') DESC, id DESC LIMIT ? OFFSET ?",
            (*sp, per_page, offset)))
        waiting = _rows(conn.execute(
            f"SELECT * FROM posts WHERE status IN ('approved', 'scheduled'){sc} "
            "ORDER BY scheduled_time IS NULL DESC, scheduled_time, id LIMIT ? OFFSET ?",
            (*sp, per_page, offset)))
        rejected = _rows(conn.execute(
            f"SELECT * FROM posts WHERE status = 'rejected'{sc} ORDER BY id DESC LIMIT 20", sp))
    for p in drafts:
        p["ready"] = bool(p["hosted_at"])
    pages = max(1, -(-max(total_drafts, total_waiting) // per_page))
    return {"drafts": drafts, "waiting": waiting, "rejected": rejected,
            "total_drafts": total_drafts, "total_waiting": total_waiting,
            "page": min(page, pages), "pages": pages, "per_page": per_page}


# ---------------------------------------------------------------------------
# Errors and runs
# ---------------------------------------------------------------------------

def list_errors(limit: int = 100, user_id: int | None = None) -> list[dict]:
    sc, sp = _scope(user_id)
    with get_connection() as conn:
        return _rows(conn.execute(
            "SELECT id, topic, status, last_error, image_url, hosted_at, fb_post_id, ig_post_id, "
            f"tiktok_publish_id FROM posts WHERE last_error IS NOT NULL AND last_error != ''{sc} "
            "ORDER BY id DESC LIMIT ?", (*sp, limit)))


def list_runs(limit: int = 50, user_id: int | None = None) -> list[dict]:
    sc, sp = _scope(user_id)
    with get_connection() as conn:
        return _rows(conn.execute(
            f"SELECT id, started_at, finished_at, steps, ok, summary FROM pipeline_runs WHERE 1 = 1{sc} "
            "ORDER BY id DESC LIMIT ?", (*sp, limit)))


def get_run(run_id: int, user_id: int | None = None) -> dict | None:
    sc, sp = _scope(user_id)
    with get_connection() as conn:
        row = conn.execute(f"SELECT * FROM pipeline_runs WHERE id = ?{sc}", (run_id, *sp)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

CALENDAR_MAX_PER_DAY = 6


def calendar_month(year: int, month: int, user_id: int | None = None) -> dict:
    """
    A month grid (weeks of 7 day-cells, Monday first) with the posts on each day.
    Scheduled/approved posts appear on scheduled_time, published posts on published_at.
    """
    first = f"{year:04d}-{month:02d}-01"
    last_day = cal.monthrange(year, month)[1]
    last = f"{year:04d}-{month:02d}-{last_day:02d} 23:59"
    sc, sp = _scope(user_id)
    with get_connection() as conn:
        rows = _rows(conn.execute(
            f"""
            SELECT * FROM (
                SELECT id, topic, status, scheduled_time, published_at, image_url, jpeg_path, user_id,
                       CASE WHEN status = 'published' THEN published_at ELSE scheduled_time END AS day_key
                FROM posts
            ) AS t
            WHERE day_key >= ? AND day_key <= ?{sc}
            ORDER BY day_key
            """, (first, last, *sp)))

    by_day: dict[int, list] = {}
    for r in rows:
        day = int(r["day_key"][8:10])
        r["time"] = r["day_key"][11:16]
        by_day.setdefault(day, []).append(r)
    # Keep each cell readable: show the first few, count the rest.
    overflow = {d: max(0, len(v) - CALENDAR_MAX_PER_DAY) for d, v in by_day.items()}
    by_day = {d: v[:CALENDAR_MAX_PER_DAY] for d, v in by_day.items()}

    today = datetime.now()
    weeks = []
    for week in cal.monthcalendar(year, month):
        weeks.append([
            {"day": d, "posts": by_day.get(d, []), "more": overflow.get(d, 0),
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
