"""
Daily AI posts: for users who set "AI posts per day" on their Profile, the
worker creates that many posts each morning from the brand brief and content
pillars (topic -> caption + hashtags + image prompt), the image step paints
the picture and draws the title on it, and the posts go to the approval
queue, or straight into the schedule when auto-approve is on.

Pictures made by a model cannot go to TikTok/YouTube (video only), so these
posts target the image channels among the user's automatic platforms.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from config.db import get_connection
from src import generator, ideas, library, pages, slots
from src.security.secrets import ConfigError
from src.web import queries

log = logging.getLogger("ai_plan")
IMAGE_PLATFORMS = ("facebook", "instagram", "threads")
RECENT_TOPICS = 40


def settings(user_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT ai_posts_per_day, ai_auto_approve, ai_title_overlay, last_ai_plan_date, posting_slots, "
            "ai_picture_source FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else {}


def due_users(today: date | None = None) -> list[int]:
    today = (today or date.today()).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM users WHERE COALESCE(ai_posts_per_day, 0) > 0 "
            "AND (last_ai_plan_date IS NULL OR last_ai_plan_date < ?) ORDER BY id", (today,)).fetchall()
    return [r["id"] for r in rows]


def recent_topics(user_id: int) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT topic FROM posts WHERE user_id = ? AND topic IS NOT NULL ORDER BY id DESC LIMIT ?",
                            (user_id, RECENT_TOPICS)).fetchall()
    return [r["topic"] for r in rows if r["topic"]]


def plan(user_id: int, count: int | None = None, today: date | None = None) -> list[int]:
    """Create today's AI posts for one user. Returns the new post ids."""
    today = today or date.today()
    conf = settings(user_id)
    count = count if count is not None else int(conf.get("ai_posts_per_day") or 0)
    if count <= 0:
        return []
    brand = library.brand_profile(user_id)
    from src.publisher import configured_platforms, parse_platforms
    chosen = parse_platforms(brand.get("auto_platforms") or "") or configured_platforms(user_id)
    platforms = [p for p in chosen if p in IMAGE_PLATFORMS] or list(IMAGE_PLATFORMS[:2])   # never TikTok/YouTube for pictures
    avoid = recent_topics(user_id)
    suggestions = ideas.suggest_topics(brand, user_id, count=max(count * 2, 4), today=today, avoid=avoid)
    topics = [s["topic"] for s in suggestions if s["topic"] not in avoid][:count]
    if len(topics) < count:                                   # the model repeated itself: fill with pillar hints
        for s in suggestions:
            if s["topic"] not in topics:
                topics.append(s["topic"])
            if len(topics) >= count:
                break
    made = []
    for topic in topics[:count]:
        try:
            post = generator.generate_post(topic, "instagram", brand.get("brand_name") or "", user_id)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad answer must not stop the day
            log.warning("  AI post for '%s' failed: %s", topic, exc)
            continue
        pid = queries.create_draft(post["topic"], post["caption"], post["hashtags"], post.get("image_prompt", ""),
                                   media_type="image", user_id=user_id, source="ai", platforms=platforms,
                                   page_id=pages.default_page_id(user_id))
        title = (post.get("title") or "").strip()[:80]
        source = "ai" if (conf.get("ai_picture_source") or "photo") == "ai" else "photo"
        with get_connection() as conn:
            conn.execute("UPDATE posts SET title_overlay = ?, image_query = ?, image_source = ? WHERE id = ?",
                         (title or None, (post.get("image_query") or "").strip() or None, source, pid))
        if conf.get("ai_auto_approve"):
            try:                                 # spread over the posting times (defaults when none are set)
                when = slots.next_free_slot(user_id, conf.get("posting_slots"))
            except slots.SlotsError:
                when = None
            queries.set_status(pid, "approved", scheduled_time=when)
        made.append(pid)
        log.info("  AI post #%s: %s%s", pid, post["topic"], " (auto-approved)" if conf.get("ai_auto_approve") else "")
    with get_connection() as conn:
        conn.execute("UPDATE users SET last_ai_plan_date = ? WHERE id = ?", (today.isoformat(), user_id))
    return made


def run_step(user_id: int | None = None) -> int:
    """Pipeline step: plan for every user whose AI posts are due today."""
    users = due_users()
    if user_id is not None:
        users = [u for u in users if u == user_id]
    if not users:
        log.info("No AI posts due today.")
        return 0
    failed = 0
    for uid in users:
        try:
            made = plan(uid)
            log.info("User %s: %d AI post(s) created.", uid, len(made))
        except ConfigError as exc:
            log.error("User %s: AI posts skipped: %s", uid, exc)
            failed += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("User %s: AI planning crashed: %s", uid, exc)
            failed += 1
    return 1 if failed else 0
