"""
Multi-platform publisher: posts drafts and due scheduled posts to
Facebook, Instagram and TikTok, then marks them 'published'.

Usage (from the project root):
    python3 -m src.publisher                       # publish everything that is due
    python3 -m src.publisher --dry-run             # show what would be posted, call no APIs
    python3 -m src.publisher --id 3                # publish one specific post
    python3 -m src.publisher --platforms facebook,instagram

Which posts are picked up:
    * status = 'approved'   (approved on the dashboard, publish as soon as possible)
    * status = 'scheduled' AND scheduled_time <= now   (format: "YYYY-MM-DD HH:MM")
    Drafts are NEVER published automatically. Approve them first.

How success is tracked:
    Each platform's post ID is saved to the database the moment that platform
    succeeds (fb_post_id, ig_post_id, tiktok_publish_id). If a later platform
    fails, the post keeps its status, the error is stored in last_error, and the
    next run retries ONLY the platforms that have not succeeded yet. A post
    becomes 'published' only when every requested platform has an ID.

Media requirements (these are platform rules, not ours):
    * Facebook:  video_url -> video post; image_url -> photo post. Without a hosted
                 URL the local file (video_path / jpeg_path / image_path) is uploaded
                 directly, so Facebook needs no hosting at all. Neither -> text post.
    * Instagram: video_url -> Reel; else image_url REQUIRED (public https JPEG).
    * TikTok:    video_url (preferred) or image_url REQUIRED, public URL on a
                 domain verified in the TikTok developer portal.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from config.db import get_connection, init_db
from src.security.secrets import ConfigError, get_secret

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("publisher")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v23.0")
META_GRAPH_URL = f"https://graph.facebook.com/{META_GRAPH_VERSION}"
TIKTOK_API_URL = "https://open.tiktokapis.com/v2"

# Unaudited TikTok apps may ONLY post with SELF_ONLY (private). Change to
# PUBLIC_TO_EVERYONE in .env once TikTok has approved the app.
TIKTOK_PRIVACY_LEVEL = os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")

REQUEST_TIMEOUT = 30          # seconds per HTTP call
UPLOAD_TIMEOUT = 900          # seconds for a direct file upload to Facebook
IG_CONTAINER_POLL_TRIES = 10  # how many times to check an Instagram image upload
IG_REEL_POLL_TRIES = 40       # reels take longer to process
IG_CONTAINER_POLL_WAIT = 3    # seconds between checks

ALL_PLATFORMS = ("facebook", "instagram", "tiktok")

# A claim older than this is treated as abandoned (worker crashed mid-post).
CLAIM_STALE_MINUTES = 10

# Which .env variables each platform needs.
REQUIRED_ENV = {
    "facebook": ("META_ACCESS_TOKEN", "META_PAGE_ID"),
    "instagram": ("META_ACCESS_TOKEN", "META_INSTAGRAM_ACCOUNT_ID"),
    "tiktok": ("TIKTOK_ACCESS_TOKEN",),
}

# Which database column stores the platform's post ID.
ID_COLUMN = {
    "facebook": "fb_post_id",
    "instagram": "ig_post_id",
    "tiktok": "tiktok_publish_id",
}


class PublishError(Exception):
    """Raised when a platform refuses or fails to publish a post."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_platform_config(platform: str, user_id: int | None = None) -> None:
    """Raise ConfigError naming the first missing variable for this platform."""
    for var in REQUIRED_ENV[platform]:
        get_secret(var, user_id)


def configured_platforms(user_id: int | None = None) -> list[str]:
    """Platforms whose credentials are all present for this user."""
    out = []
    for platform in ALL_PLATFORMS:
        try:
            check_platform_config(platform, user_id)
            out.append(platform)
        except ConfigError:
            pass
    return out


def parse_platforms(value) -> list[str]:
    """'facebook, tiktok' or ['facebook', 'tiktok'] -> ['facebook', 'tiktok'] (known, ordered, unique)."""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    chosen = {str(p).strip().lower() for p in items}
    return [p for p in ALL_PLATFORMS if p in chosen]


def post_platforms(post, requested) -> list[str]:
    """The platforms to publish this post to: its own choice, limited to what was requested."""
    own = parse_platforms(post["platforms"]) if "platforms" in post.keys() else []
    return [p for p in requested if p in own] if own else list(requested)


def full_caption(post) -> str:
    """Caption followed by hashtags on a new line (hashtags may be empty)."""
    caption = (post["caption"] or "").strip()
    hashtags = (post["hashtags"] or "").strip()
    return f"{caption}\n\n{hashtags}".strip() if hashtags else caption


def meta_error_message(payload: dict) -> str:
    """Pull the human-readable message out of a Meta Graph API error response."""
    err = payload.get("error") or {}
    code = err.get("code", "?")
    return f"Meta API error {code}: {err.get('message', 'unknown error')}"


def meta_request(method: str, url: str, **kwargs) -> dict:
    """Send a request to the Meta Graph API and return the JSON body or raise."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    try:
        resp = requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise PublishError(f"Network error talking to Meta: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        raise PublishError(f"Meta returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")

    if resp.status_code >= 400 or "error" in payload:
        raise PublishError(meta_error_message(payload))
    return payload


def tiktok_request(url: str, token: str, body: dict) -> dict:
    """Send a POST to the TikTok Content Posting API and return `data` or raise."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise PublishError(f"Network error talking to TikTok: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        raise PublishError(f"TikTok returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")

    err = payload.get("error") or {}
    if resp.status_code >= 400 or err.get("code", "ok") != "ok":
        raise PublishError(f"TikTok API error {err.get('code', resp.status_code)}: "
                           f"{err.get('message', 'unknown error')}")
    return payload.get("data") or {}


# ---------------------------------------------------------------------------
# Platform publishers. Each returns the platform's post ID or raises.
# ---------------------------------------------------------------------------

def _local_file(post, *keys) -> Path | None:
    for key in keys:
        value = post[key] if key in post.keys() else None
        if value and Path(value).exists():
            return Path(value)
    return None


def publish_facebook(post) -> str:
    """
    Publish to the Facebook Page. Order of preference:
    hosted video -> local video file -> hosted image -> local image file -> text only.
    Local files are uploaded straight to Facebook, so no hosting is needed for it.
    """
    token = get_secret("META_ACCESS_TOKEN", post["user_id"])
    page_id = get_secret("META_PAGE_ID", post["user_id"])
    message = full_caption(post)
    local_video = _local_file(post, "video_path")
    local_image = _local_file(post, "jpeg_path", "image_path")
    files = None
    timeout = REQUEST_TIMEOUT

    if post["video_url"]:
        url = f"{META_GRAPH_URL}/{page_id}/videos"
        data = {"file_url": post["video_url"], "description": message, "access_token": token}
    elif local_video:
        url = f"{META_GRAPH_URL}/{page_id}/videos"
        data = {"description": message, "access_token": token}
        files = {"source": (local_video.name, open(local_video, "rb"), "video/mp4")}
        timeout = UPLOAD_TIMEOUT
    elif post["image_url"]:
        url = f"{META_GRAPH_URL}/{page_id}/photos"
        data = {"url": post["image_url"], "message": message, "access_token": token}
    elif local_image:
        url = f"{META_GRAPH_URL}/{page_id}/photos"
        data = {"message": message, "access_token": token}
        files = {"source": (local_image.name, open(local_image, "rb"), "image/jpeg")}
        timeout = UPLOAD_TIMEOUT
    else:
        if not message.strip():
            raise PublishError("Nothing to post: no video, no image and an empty caption.")
        url = f"{META_GRAPH_URL}/{page_id}/feed"
        data = {"message": message, "access_token": token}

    try:
        payload = meta_request("POST", url, data=data, files=files, timeout=timeout)
    finally:
        if files:
            files["source"][1].close()
    post_id = payload.get("post_id") or payload.get("id")
    if not post_id:
        raise PublishError(f"Facebook returned no post id: {payload}")
    return str(post_id)


def publish_instagram(post) -> str:
    """
    Publish to Instagram: a Reel if the post has video_url, otherwise an image.
    Both use the same flow: create a media container, wait, then publish.
    """
    is_reel = bool(post["video_url"])
    if not is_reel and not post["image_url"]:
        hint = ("Instagram only accepts media from a public web address. Set up hosting "
                "(MEDIA_HOST=r2, see the key wizard) so the upload step can create one.")
        if _local_file(post, "video_path"):
            raise PublishError(f"Instagram needs a hosted video link for this video. {hint}")
        if _local_file(post, "jpeg_path", "image_path"):
            raise PublishError(f"Instagram needs a hosted image link for this image. {hint}")
        raise PublishError("Instagram needs image_url (a public JPEG link) or video_url. Post has neither.")

    token = get_secret("META_ACCESS_TOKEN", post["user_id"])
    ig_id = get_secret("META_INSTAGRAM_ACCOUNT_ID", post["user_id"])

    # Step 1: create the media container.
    if is_reel:
        data = {"media_type": "REELS", "video_url": post["video_url"],
                "caption": full_caption(post), "share_to_feed": "true", "access_token": token}
    else:
        data = {"image_url": post["image_url"], "caption": full_caption(post), "access_token": token}
    payload = meta_request("POST", f"{META_GRAPH_URL}/{ig_id}/media", data=data)
    container_id = payload.get("id")
    if not container_id:
        raise PublishError(f"Instagram returned no container id: {payload}")

    # Step 2: wait until Instagram has fetched and processed the media.
    for _ in range(IG_REEL_POLL_TRIES if is_reel else IG_CONTAINER_POLL_TRIES):
        status = meta_request(
            "GET", f"{META_GRAPH_URL}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
        )
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise PublishError(f"Instagram could not process the media: {status.get('status')}")
        time.sleep(IG_CONTAINER_POLL_WAIT)
    else:
        raise PublishError("Instagram media processing timed out. Try again in a minute.")

    # Step 3: publish the container.
    payload = meta_request(
        "POST", f"{META_GRAPH_URL}/{ig_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
    )
    media_id = payload.get("id")
    if not media_id:
        raise PublishError(f"Instagram returned no media id: {payload}")
    return str(media_id)


def publish_tiktok(post) -> str:
    """Publish a video (preferred) or photo post to TikTok via Direct Post."""
    token = get_secret("TIKTOK_ACCESS_TOKEN", post["user_id"])
    caption = full_caption(post)
    title = (post["topic"] or caption)[:90]  # TikTok title limit is short

    if post["video_url"]:
        url = f"{TIKTOK_API_URL}/post/publish/video/init/"
        body = {
            "post_info": {
                "title": caption[:2200],
                "privacy_level": TIKTOK_PRIVACY_LEVEL,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {"source": "PULL_FROM_URL", "video_url": post["video_url"]},
        }
    elif post["image_url"]:
        url = f"{TIKTOK_API_URL}/post/publish/content/init/"
        body = {
            "post_info": {
                "title": title,
                "description": caption[:4000],
                "privacy_level": TIKTOK_PRIVACY_LEVEL,
                "disable_comment": False,
                "auto_add_music": True,
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "photo_cover_index": 0,
                "photo_images": [post["image_url"]],
            },
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        }
    else:
        raise PublishError("TikTok needs video_url or image_url. Post has neither.")

    data = tiktok_request(url, token, body)
    publish_id = data.get("publish_id")
    if not publish_id:
        raise PublishError(f"TikTok returned no publish_id: {data}")

    # TikTok processes uploads in the background. Check once so an obvious
    # failure (bad URL, unverified domain) is reported now rather than silently.
    status = tiktok_request(f"{TIKTOK_API_URL}/post/publish/status/fetch/", token,
                            {"publish_id": publish_id})
    if status.get("status") == "FAILED":
        raise PublishError(f"TikTok rejected the upload: {status.get('fail_reason', 'unknown')}")
    return str(publish_id)


PUBLISHERS = {
    "facebook": publish_facebook,
    "instagram": publish_instagram,
    "tiktok": publish_tiktok,
}

# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------

def fetch_due_posts(post_id: int | None = None, user_id: int | None = None) -> list:
    """Approved posts plus scheduled posts whose time has come. Or one post by id."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_connection() as conn:
        if post_id is not None:
            return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchall()
        scope, params = (" AND user_id = ?", (user_id,)) if user_id else ("", ())
        return conn.execute(
            f"""
            SELECT * FROM posts
            WHERE (status = 'approved'
                   OR (status = 'scheduled' AND scheduled_time IS NOT NULL
                       AND scheduled_time <= ?)){scope}
            ORDER BY id
            """,
            (now, *params),
        ).fetchall()


def claim_post(post_id: int):
    """
    Atomically mark a post as being published by this worker.

    Returns the fresh post row if the claim succeeded, or None if another
    worker holds it (or it was published/rejected meanwhile). This is what
    stops two publisher processes from posting the same content twice.
    """
    now = datetime.now()
    stale_before = (now - timedelta(minutes=CLAIM_STALE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE posts SET claimed_at = ?
            WHERE id = ? AND status NOT IN ('published', 'rejected')
              AND (claimed_at IS NULL OR claimed_at < ?)
            """,
            (now.strftime("%Y-%m-%d %H:%M:%S"), post_id, stale_before),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def release_post(post_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET claimed_at = NULL WHERE id = ?", (post_id,))


def save_platform_id(post_id: int, platform: str, platform_post_id: str) -> None:
    with get_connection() as conn:
        conn.execute(f"UPDATE posts SET {ID_COLUMN[platform]} = ? WHERE id = ?",
                     (platform_post_id, post_id))


def save_error(post_id: int, message: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = ? WHERE id = ?", (message, post_id))


def mark_published(post_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE posts SET status = 'published', last_error = NULL, published_at = ? "
            "WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), post_id),
        )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def publish_post(post, platforms, dry_run: bool = False) -> bool:
    """
    Push one post to each platform it has not reached yet.
    Returns True if the post is now published everywhere requested.
    """
    pid = post["id"]
    if not dry_run:
        post = claim_post(pid)
        if post is None:
            log.info("Post #%s is being handled by another worker or is already done. Skipping.", pid)
            return False
    log.info("Post #%s (%s)", pid, post["topic"])
    try:
        return _publish_claimed(post, platforms, dry_run)
    finally:
        if not dry_run:
            release_post(pid)


def _publish_claimed(post, platforms, dry_run: bool) -> bool:
    pid = post["id"]
    errors = []
    platforms = post_platforms(post, platforms)
    if not platforms:
        log.warning("  Post #%s: none of its platforms were requested this run. Skipping.", pid)
        return False
    log.info("  platforms: %s", ", ".join(platforms))

    for platform in platforms:
        if post[ID_COLUMN[platform]]:
            log.info("  %-9s already done (id %s), skipping", platform, post[ID_COLUMN[platform]])
            continue

        try:
            check_platform_config(platform, post["user_id"])
        except ConfigError as exc:
            log.error("  %-9s skipped: %s", platform, exc)
            errors.append(f"{platform}: {exc}")
            continue

        if dry_run:
            log.info("  %-9s would publish (dry run)", platform)
            continue

        try:
            platform_post_id = PUBLISHERS[platform](post)
        except PublishError as exc:
            log.error("  %-9s FAILED: %s", platform, exc)
            errors.append(f"{platform}: {exc}")
            continue
        except Exception as exc:  # anything unexpected: log it, never crash the loop
            log.exception("  %-9s crashed unexpectedly", platform)
            errors.append(f"{platform}: unexpected error: {exc}")
            continue

        save_platform_id(pid, platform, platform_post_id)
        log.info("  %-9s published (id %s)", platform, platform_post_id)

    if dry_run:
        return False

    if errors:
        save_error(pid, " | ".join(errors))
        log.warning("  Post #%s NOT marked published. Will retry remaining platforms next run.", pid)
        return False

    mark_published(pid)
    log.info("  Post #%s marked as published on %s.", pid, ", ".join(platforms))
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Publish due posts to social platforms.")
    parser.add_argument("--id", type=int, help="Publish only this post id")
    parser.add_argument("--platforms", default=",".join(ALL_PLATFORMS),
                        help="Comma-separated: facebook,instagram,tiktok (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be posted without calling any API")
    parser.add_argument("--user", type=int, default=None,
                        help="Only this user's posts (default: every user's)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    unknown = [p for p in platforms if p not in ALL_PLATFORMS]
    if unknown:
        log.error("Unknown platform(s): %s. Choose from %s", ", ".join(unknown), ", ".join(ALL_PLATFORMS))
        return 2

    init_db()
    posts = fetch_due_posts(args.id, args.user)
    if not posts:
        log.info("Nothing to publish. Approve a draft on the dashboard first.")
        return 0

    log.info("%d post(s) to process on: %s%s", len(posts), ", ".join(platforms),
             " [DRY RUN]" if args.dry_run else "")

    published = sum(publish_post(p, platforms, dry_run=args.dry_run) for p in posts)
    log.info("Done. %d of %d post(s) fully published.", published, len(posts))
    return 0 if published == len(posts) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
