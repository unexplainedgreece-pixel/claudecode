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
    if brand.get("brand_kind") == "page":
        # Real places from the bank, facts from Wikipedia: the model only writes, never invents.
        # Extra candidates, because a place without a free photo is skipped (real pictures only).
        from src import topic_bank
        picks = topic_bank.pick(user_id, count * 4)
        topics = [(name, ref) for name, ref in picks]
    else:
        suggestions = ideas.suggest_topics(brand, user_id, count=max(count * 2, 4), today=today, avoid=avoid)
        names = [s["topic"] for s in suggestions if s["topic"] not in avoid][:count]
        if len(names) < count:                                # the model repeated itself: fill with the rest
            for s in suggestions:
                if s["topic"] not in names:
                    names.append(s["topic"])
                if len(names) >= count:
                    break
        topics = [(n, None) for n in names[:count]]
    made = []
    page_mode = brand.get("brand_kind") == "page"
    for topic, ref in topics:
        if len(made) >= count:
            break
        facts, source_url, lead_image, photo = "", "", "", None
        if ref:
            from src import topic_bank
            summary = topic_bank.wiki_summary(ref)
            facts, source_url = summary.get("extract", ""), summary.get("url", "")
            lead_image = summary.get("image", "")
            if not facts:
                log.warning("  no Wikipedia summary for %s; the writer is told to stay vague", ref)
                facts = f"No verified facts available for {topic}. Describe it in general terms only, no numbers."
            if page_mode and (conf.get("ai_picture_source") or "photo") == "photo":
                photo = _secure_photo(topic, lead_image, user_id)
                if photo is None:
                    log.info("  no free photo for %s; skipping it (real pictures only)", topic)
                    continue
        try:
            post = generator.generate_post(topic, "instagram", brand.get("brand_name") or "", user_id, facts=facts)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad answer must not stop the day
            log.warning("  AI post for '%s' failed: %s", topic, exc)
            continue
        heading = post["topic"]
        if ref and post.get("title") and any("\u0370" <= ch <= "\u03ff" for ch in post["title"]):
            heading = f"{post['title'].strip()} · {topic}"          # Greek title first, English name after
        pid = queries.create_draft(heading, post["caption"], post["hashtags"], post.get("image_prompt", ""),
                                   media_type="image", user_id=user_id, source="ai", platforms=platforms,
                                   page_id=pages.default_page_id(user_id))
        title = (post.get("title") or "").strip()[:80]
        source = "ai" if (conf.get("ai_picture_source") or "photo") == "ai" else "photo"
        query = (post.get("image_query") or "").strip() or None
        if ref:
            query = topic                        # the bank's English name finds the right photo on Commons
        with get_connection() as conn:
            conn.execute("UPDATE posts SET title_overlay = ?, image_query = ?, image_source = ?, source_ref = ? WHERE id = ?",
                         (title or None, query, source, ref, pid))
            if ref and lead_image and not photo:
                conn.execute("UPDATE posts SET image_url = NULL, host_ref = ? WHERE id = ?", (f"wiki-image:{lead_image}", pid))
        if photo:
            _store_photo(pid, photo, title, conf)
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


def plan_now(user_id: int, count: int) -> dict:
    """The dashboard button: write today's batch now and fetch the pictures, recorded
    on the Logs page like any pipeline run."""
    from src import main as pipeline
    from datetime import datetime as _dt
    capture = pipeline._LogCapture()
    logging.getLogger().addHandler(capture)
    started = _dt.now()
    ok = True
    try:
        log.info("Making %d AI post(s) now for user %s", count, user_id)
        made = plan(user_id, count=count)
        log.info("%d post(s) written: %s", len(made), ", ".join(f"#{p}" for p in made) or "none")
    except Exception as exc:  # noqa: BLE001
        ok = False
        log.error("AI posts now failed: %s", exc)
        made = []
    finally:
        logging.getLogger().removeHandler(capture)
    summary = f"ai_plan={'ok' if ok else 'exit 1'} ({len(made)} post(s))"
    pipeline.record_run(started, _dt.now(), ["ai_plan"], ok, summary, capture.buffer.getvalue(), user_id)
    if made:
        pipeline.run_pipeline(steps=("images",), user_id=user_id)
    return {"ok": ok, "made": made}


def _secure_photo(topic: str, lead_image: str, user_id: int):
    """(bytes, credit) for a real, free photo of this place, or None."""
    from src import image_generator
    for attempt in ((lambda: image_generator.fetch_lead_image(lead_image)) if lead_image else None,
                    lambda: image_generator.fetch_photo(topic, user_id)):
        if attempt is None:
            continue
        try:
            return attempt()
        except image_generator.ImageGenError as exc:
            log.debug("  photo attempt failed for %s: %s", topic, exc)
    return None


def _store_photo(pid: int, photo, title: str, conf: dict) -> None:
    """Save the secured photo as the post's picture, reshaped and titled."""
    from src import image_generator
    from src.overlay import fit_for_social, render_title
    data, credit = photo
    path = image_generator.save_local_copy(pid, data)
    try:
        fit_for_social(path)
        if title and conf.get("ai_title_overlay", 1) in (None, 1):
            render_title(path, title)
    except Exception as exc:  # noqa: BLE001
        log.warning("  picture processing failed for post #%s: %s", pid, exc)
    image_generator.save_image(pid, path, credit)


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
            from src.ai.providers import AIError
            if isinstance(exc, AIError):          # busy model, quota, bad answer: plain line, next cycle retries
                log.warning("User %s: AI posts postponed to the next cycle: %s", uid, exc)
            else:
                log.exception("User %s: AI planning crashed: %s", uid, exc)
            failed += 1
    return 1 if failed else 0
