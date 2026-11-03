"""
Usage limits per subscription tier, enforced before anything expensive runs.

    from src.billing.limits import enforce, LimitError
    enforce(user, "create_post")   # raises LimitError with a user-safe message
    enforce(user, "upload")
    enforce(user, "reel")

`usage(user_id)` returns this month's counts for the billing page.
In FastAPI, `require_feature("upload")` wraps the same check as a dependency.
"""

from datetime import datetime

from fastapi import Depends, HTTPException, Request

from config.db import get_connection
from src.billing.tiers import tier_info
from src.web.auth import current_user


class LimitError(Exception):
    """The user's tier does not allow this action right now."""

    def __init__(self, message: str, code: str, tier: str):
        super().__init__(message)
        self.code, self.tier = code, tier


def month_start() -> str:
    return datetime.now().strftime("%Y-%m-01 00:00:00")


def usage(user_id: int) -> dict:
    """Counts for the current calendar month."""
    with get_connection() as conn:
        base = "SELECT COUNT(*) AS n FROM posts WHERE user_id = ? AND created_at >= ?"
        params = (user_id, month_start())
        posts = conn.execute(base, params).fetchone()["n"]
        uploads = conn.execute(base + " AND source = 'upload'", params).fetchone()["n"]
        reels = conn.execute(base + " AND source = 'ai' AND media_type = 'video'", params).fetchone()["n"]
    return {"posts": posts, "uploads": uploads, "reels": reels, "since": month_start()}


def check(user: dict, action: str) -> dict:
    """Return usage if allowed, else raise LimitError. Actions: create_post, upload, reel."""
    tier = user.get("tier") or "free"
    info = tier_info(tier)
    used = usage(user["id"])
    limit = info["posts_per_month"]

    if action in ("create_post", "upload", "reel") and limit is not None and used["posts"] >= limit:
        raise LimitError(
            f"You have used {used['posts']} of {limit} posts this month on the {info['label']} plan. "
            "Upgrade to keep posting.", "posts_limit", tier)
    if action == "upload" and not info["uploads"]:
        raise LimitError(f"Custom video uploads are not included in the {info['label']} plan. "
                         "Upgrade to Pro or Enterprise.", "no_uploads", tier)
    if action == "reel" and not info["reels"]:
        raise LimitError(f"AI video reels are not included in the {info['label']} plan. "
                         "Upgrade to Enterprise.", "no_reels", tier)
    return used


def enforce(user: dict, action: str) -> dict:
    return check(user, action)


def remaining_posts(user: dict) -> int | None:
    limit = tier_info(user.get("tier"))["posts_per_month"]
    return None if limit is None else max(0, limit - usage(user["id"])["posts"])


def require_feature(action: str):
    """FastAPI dependency: 402 Payment Required with a clear message when over the limit."""
    def dependency(request: Request, user: dict = Depends(current_user)) -> dict:
        try:
            check(user, action)
        except LimitError as exc:
            raise HTTPException(402, {"message": str(exc), "code": exc.code, "tier": exc.tier,
                                      "upgrade_url": "/billing"})
        return user
    return dependency
