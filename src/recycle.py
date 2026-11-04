"""
Evergreen recycling: a post marked evergreen is copied back into the approval
queue `recycle_days` days after it was published, so good content runs again.

    python3 -m src.recycle            # pipeline step: create due copies (all users)
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta

from config.db import get_connection, init_db

log = logging.getLogger("recycle")
DEFAULT_DAYS = 30


def due_posts(user_id: int | None = None) -> list[dict]:
    scope, params = (" AND p.user_id = ?", [user_id]) if user_id else ("", [])
    now = datetime.now()
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            f"""
            SELECT p.* FROM posts p
            WHERE p.status = 'published' AND COALESCE(p.evergreen, 0) = 1 AND p.published_at IS NOT NULL
              AND (p.recycled_at IS NULL OR p.recycled_at < p.published_at){scope}
            """, params)]
    out = []
    for p in rows:
        days = int(p.get("recycle_days") or DEFAULT_DAYS)
        when = datetime.strptime(p["published_at"][:19], "%Y-%m-%d %H:%M:%S") + timedelta(days=days)
        if when <= now:
            out.append(p)
    return out


def recycle_post(post: dict) -> int:
    """Create a fresh draft from a published evergreen post. Returns the new post id."""
    with get_connection() as conn:
        new_id = conn.execute(
            """
            INSERT INTO posts (user_id, topic, caption, hashtags, image_prompt, status, media_type, source,
                               image_path, jpeg_path, image_url, video_path, video_url, hosted_at, host_ref,
                               platforms, page_id, upload_name, media_credit, evergreen, recycle_days, recycled_from)
            SELECT user_id, topic, caption, hashtags, image_prompt, 'draft', media_type, source,
                   image_path, jpeg_path, image_url, video_path, video_url, hosted_at, host_ref,
                   platforms, page_id, upload_name, media_credit, evergreen, recycle_days, id
            FROM posts WHERE id = ?
            """, (post["id"],)).lastrowid
        conn.execute("UPDATE posts SET recycled_at = ? WHERE id = ?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), post["id"]))
    log.info("  recycled post #%s -> new draft #%s", post["id"], new_id)
    return new_id


def run_step(user_id: int | None = None) -> int:
    made = [recycle_post(p) for p in due_posts(user_id)]
    if made:
        log.info("Recycled %d evergreen post(s) into drafts: %s", len(made), made)
    else:
        log.info("No evergreen posts due for recycling.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Recycle evergreen posts.")
    parser.add_argument("--user", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    init_db()
    return run_step(args.user)


if __name__ == "__main__":
    sys.exit(main())
