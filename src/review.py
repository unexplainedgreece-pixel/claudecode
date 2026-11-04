"""
Client review links.

An agency owner creates a private link for a client. The client opens it (no
account, no login), sees the posts waiting for approval for their Page, and
either approves a post (it joins the publishing queue) or asks for changes
with a note. The owner sees the note on the post card and gets a Telegram
message when notifications are on.

The link token is the secret: 32 random url-safe bytes, revocable, optionally
expiring. Links only ever show posts of the owner who created them, and only
for the chosen Page when one is set.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from config.db import get_connection
from src.web import queries

log = logging.getLogger("review")
MAX_NOTE = 1000


class ReviewError(Exception):
    """User-safe message."""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_link(user_id: int, label: str = "", page_id=None, days: int | None = None) -> dict:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S") if days else None
    page = int(page_id) if page_id not in (None, "", 0, "0") else None
    with get_connection() as conn:
        if page is not None:
            owned = conn.execute("SELECT id FROM pages WHERE id = ? AND user_id = ?", (page, user_id)).fetchone()
            if not owned:
                raise ReviewError("That Page is not one of your connected Pages.")
        cur = conn.execute(
            "INSERT INTO review_links (user_id, token, label, page_id, created_at, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)", (user_id, token, (label or "").strip()[:80] or "Client review", page, _now(), expires))
        link_id = cur.lastrowid
    return get_link(link_id, user_id)


def get_link(link_id: int, user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM review_links WHERE id = ? AND user_id = ?", (link_id, user_id)).fetchone()
    return dict(row) if row else None


def list_links(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT l.*, p.name AS page_name FROM review_links l LEFT JOIN pages p ON p.id = l.page_id "
            "WHERE l.user_id = ? ORDER BY l.revoked, l.id DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke(link_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("UPDATE review_links SET revoked = 1 WHERE id = ? AND user_id = ?", (link_id, user_id))
        return cur.rowcount > 0


def by_token(token: str) -> dict | None:
    """The live link for this token, or None when unknown, revoked or expired."""
    if not token or len(token) > 100:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT l.*, p.name AS page_name, u.display_name AS business "
            "FROM review_links l LEFT JOIN pages p ON p.id = l.page_id LEFT JOIN users u ON u.id = l.user_id "
            "WHERE l.token = ? AND l.revoked = 0", (token,)).fetchone()
        if not row:
            return None
        link = dict(row)
        if link.get("expires_at") and link["expires_at"] < _now():
            return None
        conn.execute("UPDATE review_links SET last_used_at = ? WHERE id = ?", (_now(), link["id"]))
    return link


def _covers(link: dict, post: dict | None) -> bool:
    if not post or post["user_id"] != link["user_id"]:
        return False
    if link.get("page_id") and post.get("page_id") not in (None, link["page_id"]):
        return False
    return True


def pending_posts(link: dict) -> list[dict]:
    """Posts waiting for approval that this link may show (newest first)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE user_id = ? AND status = 'draft' ORDER BY id DESC LIMIT 100",
            (link["user_id"],)).fetchall()
    return [dict(r) for r in rows if _covers(link, dict(r))]


def recent_decisions(link: dict, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE user_id = ? AND client_decision IS NOT NULL "
            "ORDER BY client_decided_at DESC LIMIT ?", (link["user_id"], limit)).fetchall()
    return [dict(r) for r in rows if _covers(link, dict(r))]


def post_for_link(link: dict, post_id: int) -> dict | None:
    post = queries.get_post(post_id, user_id=link["user_id"])
    return post if _covers(link, post) else None


def decide(link: dict, post_id: int, decision: str, note: str = "") -> dict:
    """Record the client's decision. 'approve' queues the post; 'changes' keeps it
    waiting with the client's note attached."""
    post = post_for_link(link, post_id)
    if not post:
        raise ReviewError("That post is not available on this link.")
    if post["status"] != "draft":
        raise ReviewError("That post has already been handled.")
    note = (note or "").strip()[:MAX_NOTE]
    if decision == "approve":
        queries.set_status(post_id, "approved", scheduled_time=post.get("scheduled_time"))
    elif decision == "changes":
        if not note:
            raise ReviewError("Please say what should change.")
    else:
        raise ReviewError("Unknown decision.")
    with get_connection() as conn:
        conn.execute("UPDATE posts SET client_decision = ?, client_note = ?, client_decided_at = ? WHERE id = ?",
                     ("approved" if decision == "approve" else "changes", note or None, _now(), post_id))
    try:
        from src import notify
        who = link.get("label") or "A client"
        if decision == "approve":
            notify.send(link["user_id"], f"✅ {who} approved post #{post_id}: {post['topic'] or ''}".strip())
        else:
            notify.send(link["user_id"], f"✏️ {who} asked for changes on post #{post_id}: {note}")
    except Exception as exc:  # noqa: BLE001 - notifications never break the review
        log.warning("review notification failed: %s", exc)
    return queries.get_post(post_id)
