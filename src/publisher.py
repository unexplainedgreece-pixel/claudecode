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
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from config.db import get_connection, init_db
from src.security.secrets import ConfigError, get_secret
from src import pages as pages_store

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("publisher")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")
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
IG_POLL_ERROR_TOLERANCE = 3   # consecutive failed status checks before giving up (Meta had a
                              # May 2026 incident where status checks failed while the media was fine)
IG_PUBLISH_RETRIES = 3        # media_publish retries on "not ready" / temporary errors
IG_PUBLISH_RETRY_WAIT = 10    # seconds between those retries

FB_REELS_UPLOAD_URL = f"https://rupload.facebook.com/video-upload/{META_GRAPH_VERSION}"
FB_REEL_POLL_TRIES = 40       # Facebook Reels processing checks (3 s apart)
# Vertical videos go out as Facebook Reels (more reach than a feed video). Set to 0 to disable.
FB_VERTICAL_AS_REEL = os.getenv("FB_VERTICAL_AS_REEL", "1") not in ("0", "false", "no")

# Caption limits the platforms enforce (checked before any API call is made).
CAPTION_LIMITS = {
    "instagram": {"chars": 2200, "hashtags": 30, "mentions": 20},
    "threads": {"chars": 500},
    "tiktok": {"chars": 2200},
    "facebook": {"chars": 63206},
}

ALL_PLATFORMS = ("facebook", "instagram", "tiktok", "threads", "youtube")
CAROUSEL_PLATFORMS = ("facebook", "instagram")
THREADS_API_URL = "https://graph.threads.net/v1.0"
THREADS_PUBLISH_WAIT = 30     # Meta recommends waiting before publishing a Threads container
THREADS_POLL_TRIES = 20

# A claim older than this is treated as abandoned (worker crashed mid-post).
CLAIM_STALE_MINUTES = 10
MAX_PUBLISH_ATTEMPTS = 5      # after this many failed runs a post waits for a human (Publish again)

# Which .env variables each platform needs.
REQUIRED_ENV = {
    "facebook": ("META_ACCESS_TOKEN", "META_PAGE_ID"),
    "instagram": ("META_ACCESS_TOKEN", "META_INSTAGRAM_ACCOUNT_ID"),
    "tiktok": ("TIKTOK_ACCESS_TOKEN",),
    "threads": ("THREADS_ACCESS_TOKEN",),
    "youtube": ("YT_CLIENT_ID", "YT_CLIENT_SECRET"),
}

# Which database column stores the platform's post ID.
ID_COLUMN = {
    "facebook": "fb_post_id",
    "instagram": "ig_post_id",
    "tiktok": "tiktok_publish_id",
    "threads": "threads_post_id",
    "youtube": "youtube_video_id",
}


class PublishError(Exception):
    """Raised when a platform refuses or fails to publish a post.

    `code`/`subcode` carry the platform's error numbers when known, and
    `transient` says whether a retry a little later has a real chance.
    """

    def __init__(self, message: str, code=None, subcode=None, transient: bool = False):
        super().__init__(message)
        self.code, self.subcode, self.transient = code, subcode, transient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def meta_credentials(post) -> dict:
    """
    {"token", "page_id", "ig_id"} for this post: from its connected Page if it has
    one, otherwise from the META_* settings. Raises ConfigError if nothing is set.
    """
    page_ref = post["page_id"] if "page_id" in post.keys() else None
    if page_ref:
        page = pages_store.get_page(page_ref)
        if page is None:
            raise ConfigError("The Facebook Page chosen for this post is no longer connected. "
                              "Open the post and pick another Page.")
        return {"token": page["token"], "page_id": page["fb_page_id"], "ig_id": page["ig_account_id"],
                "name": page["name"]}
    uid = post["user_id"]
    return {"token": get_secret("META_ACCESS_TOKEN", uid), "page_id": get_secret("META_PAGE_ID", uid),
            "ig_id": _optional_secret("META_INSTAGRAM_ACCOUNT_ID", uid), "name": None}


def _optional_secret(name: str, user_id) -> str | None:
    try:
        return get_secret(name, user_id)
    except ConfigError:
        return None


def check_platform_config(platform: str, user_id: int | None = None, post=None) -> None:
    """Raise ConfigError when the credentials for this platform are missing."""
    if platform == "tiktok":
        get_secret("TIKTOK_ACCESS_TOKEN", user_id)
        return
    if platform == "threads":
        get_secret("THREADS_ACCESS_TOKEN", user_id)
        return
    if platform == "youtube":
        from src import youtube
        get_secret("YT_CLIENT_ID", user_id)
        get_secret("YT_CLIENT_SECRET", user_id)
        if not user_id or not youtube.connection(user_id):
            raise ConfigError("YouTube is not connected. Use 'Connect YouTube' on the API settings page.")
        return
    if post is not None and ("page_id" in post.keys()) and post["page_id"]:
        creds = meta_credentials(post)
    elif user_id and pages_store.list_pages(user_id):
        page = pages_store.list_pages(user_id)[0]
        creds = {"ig_id": page["ig_account_id"]}
    else:
        for var in REQUIRED_ENV[platform]:
            get_secret(var, user_id)
        return
    if platform == "instagram" and not creds.get("ig_id"):
        raise ConfigError("This Facebook Page has no Instagram business account linked to it.")


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
    """
    The platforms to publish this post to: its own choice, limited to what was
    requested. A post with no choice goes to every requested platform the user
    has keys for (so an unconfigured Threads or YouTube never blocks it).
    """
    if ("media_type" in post.keys() and post["media_type"] == "carousel"):
        requested = [p for p in requested if p in CAROUSEL_PLATFORMS]      # photo carousels: FB + IG only
    own = parse_platforms(post["platforms"]) if "platforms" in post.keys() else []
    if own:
        return [p for p in requested if p in own]
    uid = post["user_id"] if "user_id" in post.keys() else None
    configured = configured_platforms(uid)
    return [p for p in requested if p in configured] or list(requested)


def hashtags_in_comment(post) -> bool:
    """User setting: keep the caption clean and post hashtags as the first comment."""
    uid = post["user_id"] if "user_id" in post.keys() else None
    if not uid:
        return False
    with get_connection() as conn:
        row = conn.execute("SELECT hashtags_as_comment FROM users WHERE id = ?", (uid,)).fetchone()
    return bool(row and row["hashtags_as_comment"])


def full_caption(post) -> str:
    """Caption followed by hashtags on a new line, unless hashtags go in the first comment."""
    caption = (post["caption"] or "").strip()
    hashtags = (post["hashtags"] or "").strip()
    if not hashtags or hashtags_in_comment(post):
        return caption
    return f"{caption}\n\n{hashtags}".strip()


def caption_issues(post, platforms=None) -> list[str]:
    """Plain-language reasons a platform would reject this caption (empty list = fine).

    Checked before publishing so a too-long caption or 31 hashtags fails fast with
    a clear message instead of a cryptic Meta error after the media was processed.
    """
    issues = []
    text = full_caption(post)
    hashtags_all = (post["hashtags"] or "")
    n_tags = len(re.findall(r"#\w+", text + " " + (hashtags_all if hashtags_in_comment(post) else "")))
    n_mentions = len(re.findall(r"(?<!\w)@\w", text))
    for platform in (platforms or parse_platforms(post["platforms"] if "platforms" in post.keys() else "") or ALL_PLATFORMS):
        lim = CAPTION_LIMITS.get(platform)
        if not lim:
            continue
        name = platform.capitalize()
        if len(text) > lim["chars"]:
            issues.append(f"{name}: caption is {len(text)} characters, the limit is {lim['chars']}.")
        if "hashtags" in lim and n_tags > lim["hashtags"]:
            issues.append(f"{name}: {n_tags} hashtags, the limit is {lim['hashtags']}.")
        if "mentions" in lim and n_mentions > lim["mentions"]:
            issues.append(f"{name}: {n_mentions} @mentions, the limit is {lim['mentions']}.")
    return issues


def alt_text(post) -> str:
    """Short description of the image for screen readers (Instagram alt_text,
    Facebook alt_text_custom): the title plus the first sentence of the caption."""
    topic = (post["topic"] or "").strip()
    clean = re.sub(r"\s+", " ", re.sub(r"#\w+", "", post["caption"] or "")).replace(" .", ".").strip()
    caption = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0].strip()
    text = topic if not caption else (f"{topic}. {caption}" if topic and not caption.lower().startswith(topic.lower()) else caption)
    return text[:500]


def is_ai_media(post) -> bool:
    """True when the post's picture came out of an AI image generator (the image
    step records that in media_credit). Meta asks for AI content to be labelled."""
    credit = (post["media_credit"] if "media_credit" in post.keys() else "") or ""
    return credit.startswith("AI image")


def _check_caption(post, platform: str) -> None:
    problems = caption_issues(post, [platform])
    if problems:
        raise PublishError(" ".join(problems) + " Shorten the caption or hashtags and approve again.")


def post_first_comment(post, platform: str, remote_id: str) -> str | None:
    """Add the hashtags as the first comment on a fresh Facebook post or Instagram media."""
    hashtags = (post["hashtags"] or "").strip()
    if not hashtags or not hashtags_in_comment(post):
        return None
    creds = meta_credentials(post)
    payload = meta_request("POST", f"{META_GRAPH_URL}/{remote_id}/comments",
                           data={"message": hashtags, "access_token": creds["token"]})
    return str(payload.get("id") or "")


# Error numbers Meta documents as worth retrying a moment later.
META_TRANSIENT_CODES = {1, 2, 4, 17, 24, 32, 341, 613}         # unknown/temporary/rate limit
META_TRANSIENT_SUBCODES = {33, 2207008, 2207027, 2207051}       # auth glitch, temporary, media not ready
META_HINTS = {
    190: "The access token has expired or was revoked. Reconnect the Page in Settings > Connected Pages.",
    10: "The app lacks a permission for this action. Reconnect the Page and tick every requested permission.",
    200: "The app lacks a permission for this action. Reconnect the Page and tick every requested permission.",
    352: "Instagram does not accept this video format. Export as MP4 (H.264 video, AAC audio).",
    2207052: "Instagram could not download the media. The link must be public and stay online.",
    2207057: "Instagram's daily publishing limit for this account was reached. It resets after 24 hours.",
    2207006: "Instagram could not fetch the media. Check that the link opens in a private browser window.",
    2207026: "Instagram rejected the video format or length. Reels: MP4, 3 s to 15 min, 9:16 works best.",
    2207003: "Instagram's processing timed out. Try a smaller or shorter video.",
    2207020: "Instagram rejected the caption: too long or too many hashtags (max 30).",
    2207032: "Instagram could not create the container. Check the media link and the account type (must be Business or Creator).",
    4: "Meta's rate limit was hit. The post will be retried on a later cycle.",
    17: "Meta's rate limit was hit. The post will be retried on a later cycle.",
    32: "Meta's rate limit for this Page was hit. The post will be retried on a later cycle.",
    613: "Meta's rate limit was hit. The post will be retried on a later cycle.",
}


def meta_error(payload: dict, platform: str = "Meta") -> PublishError:
    """Build a PublishError (with code, subcode, transient flag and a plain-language hint)
    out of a Meta Graph API error response."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):                   # e.g. {"error": "invalid_token"} or a bare list/null body
        err = {"message": str(err) if err else str(payload)[:300]}
    code, subcode = err.get("code"), err.get("error_subcode")
    message = err.get("error_user_msg") or err.get("message") or "unknown error"
    text = f"{platform} API error {code if code is not None else '?'}"
    if subcode:
        text += f"/{subcode}"
    text += f": {message}"
    hint = META_HINTS.get(subcode) or META_HINTS.get(code)
    if hint and hint not in text:
        text += f" -- {hint}"
    transient = code in META_TRANSIENT_CODES or subcode in META_TRANSIENT_SUBCODES
    return PublishError(text, code=code, subcode=subcode, transient=transient)


def meta_error_message(payload: dict) -> str:
    """Pull the human-readable message out of a Meta Graph API error response."""
    return str(meta_error(payload))


def _poll_container(container_id: str, token: str, tries: int, what: str = "media") -> None:
    """Wait until an Instagram media container is FINISHED.

    Follows Meta's guidance: IN_PROGRESS keeps waiting, ERROR/EXPIRED stop, and a
    few failed status checks in a row are tolerated (the check itself can glitch
    while the media is fine).
    """
    failures = 0
    for _ in range(tries):
        try:
            status = meta_request("GET", f"{META_GRAPH_URL}/{container_id}",
                                  params={"fields": "status_code,status", "access_token": token})
        except PublishError as exc:
            failures += 1
            if failures >= IG_POLL_ERROR_TOLERANCE or not (exc.transient or exc.code == 100):
                raise
            log.warning("  instagram status check failed (%s/%s), retrying: %s", failures, IG_POLL_ERROR_TOLERANCE, exc)
            time.sleep(IG_CONTAINER_POLL_WAIT)
            continue
        failures = 0
        code = status.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code == "ERROR":
            raise PublishError(f"Instagram could not process the {what}: {status.get('status') or 'no details'}")
        if code == "EXPIRED":
            raise PublishError(f"The Instagram {what} container expired before it was published (containers live 24 h).",
                               transient=True)
        time.sleep(IG_CONTAINER_POLL_WAIT)
    raise PublishError(f"Instagram {what} processing timed out. It will be retried on a later cycle.", transient=True)


def _media_publish(ig_id: str, container_id: str, token: str) -> dict:
    """Publish a finished container, retrying the documented 'not ready yet' /
    temporary errors (codes 24 and 9007/2207027) a few times before giving up."""
    for attempt in range(1, IG_PUBLISH_RETRIES + 1):
        try:
            return meta_request("POST", f"{META_GRAPH_URL}/{ig_id}/media_publish",
                                data={"creation_id": container_id, "access_token": token})
        except PublishError as exc:
            retry = exc.code in (24, 9007, 1, 2) or exc.subcode in (2207008, 2207027)
            if attempt == IG_PUBLISH_RETRIES or not retry:
                raise
            log.warning("  instagram media_publish not ready (try %s/%s): %s", attempt, IG_PUBLISH_RETRIES, exc)
            time.sleep(IG_PUBLISH_RETRY_WAIT)
    raise PublishError("Instagram media_publish failed")   # unreachable; keeps type checkers calm


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

    if resp.status_code >= 400 or not isinstance(payload, dict) or "error" in payload:
        raise meta_error(payload)
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
    creds = meta_credentials(post)
    token, page_id = creds["token"], creds["page_id"]
    message = full_caption(post)
    if "media_type" in post.keys() and post["media_type"] == "carousel":
        return publish_facebook_carousel(post, page_id, token, message)
    local_video = _local_file(post, "video_path")
    local_image = _local_file(post, "jpeg_path", "image_path")
    files = None
    timeout = REQUEST_TIMEOUT

    if (post["video_url"] or local_video) and _is_vertical_video(post, local_video):
        return publish_facebook_reel(post, page_id, token, message, local_video)

    if post["video_url"]:
        url = f"{META_GRAPH_URL}/{page_id}/videos"
        data = {"file_url": post["video_url"], "description": message, "access_token": token}
        log.info("  facebook  sending video link %s", post["video_url"])
    elif local_video:
        url = f"{META_GRAPH_URL}/{page_id}/videos"
        data = {"description": message, "access_token": token}
        files = {"source": (local_video.name, open(local_video, "rb"), "video/mp4")}
        timeout = UPLOAD_TIMEOUT
        log.info("  facebook  uploading video file %s (%.1f MB)", local_video.name,
                 local_video.stat().st_size / 1024 / 1024)
    elif post["image_url"]:
        url = f"{META_GRAPH_URL}/{page_id}/photos"
        data = {"url": post["image_url"], "message": message, "access_token": token}
        if alt_text(post):
            data["alt_text_custom"] = alt_text(post)
        log.info("  facebook  sending photo link %s", post["image_url"])
    elif local_image:
        url = f"{META_GRAPH_URL}/{page_id}/photos"
        data = {"message": message, "access_token": token}
        if alt_text(post):
            data["alt_text_custom"] = alt_text(post)
        files = {"source": (local_image.name, open(local_image, "rb"), "image/jpeg")}
        timeout = UPLOAD_TIMEOUT
        log.info("  facebook  uploading photo file %s", local_image.name)
    else:
        if not message.strip():
            raise PublishError("Nothing to post: no video, no image and an empty caption.")
        if post["media_type"] == "video" or post["video_path"]:
            raise PublishError(f"Video file not found on disk: {post['video_path'] or '(no path)'}. "
                               "Not posting text instead.")
        url = f"{META_GRAPH_URL}/{page_id}/feed"
        data = {"message": message, "access_token": token}
        log.info("  facebook  sending text-only post")

    try:
        payload = meta_request("POST", url, data=data, files=files, timeout=timeout)
    finally:
        if files:
            files["source"][1].close()
    post_id = payload.get("post_id") or payload.get("id")
    if not post_id:
        raise PublishError(f"Facebook returned no post id: {payload}")
    return str(post_id)


def publish_facebook_carousel(post, page_id: str, token: str, message: str) -> str:
    """Several photos in one Facebook post: upload each photo unpublished, then
    one feed post that attaches them all."""
    from src import carousel
    slides = carousel.items(post["id"])
    if len(slides) < carousel.MIN_ITEMS:
        raise PublishError("This carousel has fewer than 2 photos.")
    media_ids = []
    for slide in slides:
        data = {"published": "false", "access_token": token}
        if alt_text(post):
            data["alt_text_custom"] = alt_text(post)
        files = None
        local = next((Path(slide[k]) for k in ("jpeg_path", "image_path") if slide.get(k) and Path(slide[k]).exists()), None)
        if slide.get("image_url"):
            data["url"] = slide["image_url"]
        elif local:
            files = {"source": (local.name, open(local, "rb"), "image/jpeg")}
        else:
            raise PublishError(f"Photo {slide['position'] + 1} of the carousel is missing (no file, no link).")
        try:
            payload = meta_request("POST", f"{META_GRAPH_URL}/{page_id}/photos", data=data, files=files,
                                   timeout=UPLOAD_TIMEOUT if files else REQUEST_TIMEOUT)
        finally:
            if files:
                files["source"][1].close()
        if not payload.get("id"):
            raise PublishError(f"Facebook returned no photo id for slide {slide['position'] + 1}: {payload}")
        media_ids.append(str(payload["id"]))
    data = {"message": message, "access_token": token}
    for i, mid in enumerate(media_ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
    log.info("  facebook  posting %d photos as one post", len(media_ids))
    payload = meta_request("POST", f"{META_GRAPH_URL}/{page_id}/feed", data=data)
    post_id = payload.get("id") or payload.get("post_id")
    if not post_id:
        raise PublishError(f"Facebook returned no post id: {payload}")
    return str(post_id)


def publish_instagram_carousel(post, ig_id: str, token: str) -> str:
    """Instagram carousel: one container per photo (is_carousel_item), then a
    CAROUSEL container with the children, then publish."""
    from src import carousel
    urls = carousel.hosted_urls(post["id"])
    slides = carousel.items(post["id"])
    if len(slides) < carousel.MIN_ITEMS:
        raise PublishError("This carousel has fewer than 2 photos.")
    if len(urls) < len(slides):
        raise PublishError("Instagram needs a public link for every photo of the carousel. "
                           "Set up hosting (MEDIA_HOST=r2) so the upload step can create them.")
    children = []
    for i, url in enumerate(urls):
        data = {"image_url": url, "is_carousel_item": "true", "access_token": token}
        if alt_text(post):
            data["alt_text"] = alt_text(post)
        child = meta_request("POST", f"{META_GRAPH_URL}/{ig_id}/media", data=data).get("id")
        if not child:
            raise PublishError(f"Instagram returned no container for photo {i + 1}.")
        children.append(str(child))
    for child in children:
        _poll_container(child, token, IG_CONTAINER_POLL_TRIES, "photo")
    data = {"media_type": "CAROUSEL", "children": ",".join(children), "caption": full_caption(post),
            "access_token": token}
    if is_ai_media(post):
        data["is_ai_generated"] = "true"
    parent = meta_request("POST", f"{META_GRAPH_URL}/{ig_id}/media", data=data).get("id")
    if not parent:
        raise PublishError("Instagram returned no carousel container id.")
    _poll_container(parent, token, IG_CONTAINER_POLL_TRIES, "carousel")
    payload = _media_publish(ig_id, parent, token)
    media_id = payload.get("id")
    if not media_id:
        raise PublishError(f"Instagram returned no media id: {payload}")
    log.info("  instagram carousel published (%d photos, id %s)", len(children), media_id)
    return str(media_id)


def _is_vertical_video(post, local_video) -> bool:
    """True when the video is taller than wide (ffprobe on the local file)."""
    if not FB_VERTICAL_AS_REEL:
        return False
    path = local_video or _local_file(post, "video_path")
    if not path:
        return False
    from src.library import probe_video
    meta = probe_video(Path(path))
    return bool(meta.get("width") and meta.get("height") and meta["height"] > meta["width"])


def publish_facebook_reel(post, page_id: str, token: str, message: str, local_video=None) -> str:
    """Publish a video as a Facebook Reel (Reels Publishing API): start a session,
    upload the bytes (or hand over the hosted link), finish with PUBLISHED, then
    wait until Facebook has processed it."""
    start = meta_request("POST", f"{META_GRAPH_URL}/{page_id}/video_reels",
                         data={"upload_phase": "start", "access_token": token})
    video_id = start.get("video_id")
    if not video_id:
        raise PublishError(f"Facebook returned no video id for the reel: {start}")

    headers = {"Authorization": f"OAuth {token}", "offset": "0"}
    if local_video:
        size = Path(local_video).stat().st_size
        headers["file_size"] = str(size)
        log.info("  facebook  uploading reel %s (%.1f MB)", Path(local_video).name, size / 1024 / 1024)
        with open(local_video, "rb") as fh:
            upload = meta_request("POST", f"{FB_REELS_UPLOAD_URL}/{video_id}", headers=headers,
                                  data=fh, timeout=UPLOAD_TIMEOUT)
    else:
        headers["file_url"] = post["video_url"]
        log.info("  facebook  sending reel link %s", post["video_url"])
        upload = meta_request("POST", f"{FB_REELS_UPLOAD_URL}/{video_id}", headers=headers, timeout=UPLOAD_TIMEOUT)
    if not upload.get("success", True):
        raise PublishError(f"Facebook did not accept the reel upload: {upload}")

    finish = meta_request("POST", f"{META_GRAPH_URL}/{page_id}/video_reels",
                          data={"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
                                "description": message, "access_token": token})
    if not finish.get("success", True):
        raise PublishError(f"Facebook refused to publish the reel: {finish}")

    for _ in range(FB_REEL_POLL_TRIES):
        try:
            status = meta_request("GET", f"{META_GRAPH_URL}/{video_id}",
                                  params={"fields": "status", "access_token": token}).get("status")
        except PublishError as exc:                       # the reel is in; a flaky status check is not fatal
            log.warning("  facebook  reel status check failed: %s", exc)
            break
        if not isinstance(status, dict):
            break

        def _phase(name):
            value = status.get(name)
            return value if isinstance(value, dict) else {}
        if status.get("video_status") == "error" or _phase("processing_phase").get("status") == "error":
            err = _phase("processing_phase").get("error") or _phase("uploading_phase").get("error") or {}
            detail = err.get("message") if isinstance(err, dict) else err
            raise PublishError(f"Facebook could not process the reel: {detail or status}")
        if _phase("publishing_phase").get("status") == "complete" or status.get("video_status") == "ready":
            break
        time.sleep(IG_CONTAINER_POLL_WAIT)
    log.info("  facebook  reel published (video id %s)", video_id)
    return str(video_id)


def _maybe_story(post) -> None:
    """Also post the media as an Instagram Story when the post asks for it (best effort)."""
    if not ("also_story" in post.keys() and post["also_story"]) or ("ig_story_id" in post.keys() and post["ig_story_id"]):
        return
    if not (post["image_url"] or post["video_url"]):
        log.info("  story     skipped: needs a hosted image or video link")
        return
    try:
        creds = meta_credentials(post)
        if not creds.get("ig_id"):
            return
        data = {"media_type": "STORIES", "access_token": creds["token"]}
        data["video_url" if post["video_url"] else "image_url"] = post["video_url"] or post["image_url"]
        container = meta_request("POST", f"{META_GRAPH_URL}/{creds['ig_id']}/media", data=data).get("id")
        _poll_container(container, creds["token"], IG_REEL_POLL_TRIES if post["video_url"] else IG_CONTAINER_POLL_TRIES, "story")
        story = _media_publish(creds["ig_id"], container, creds["token"]).get("id")
        with get_connection() as conn:
            conn.execute("UPDATE posts SET ig_story_id = ? WHERE id = ?", (str(story), post["id"]))
        log.info("  story     posted to Instagram (id %s)", story)
    except (PublishError, ConfigError) as exc:
        log.warning("  story     failed (post itself is fine): %s", exc)


def publish_instagram(post) -> str:
    """
    Publish to Instagram: a Reel if the post has video_url, otherwise an image.
    Both use the same flow: create a media container, wait, then publish.
    """
    _check_caption(post, "instagram")
    is_reel = bool(post["video_url"])
    is_carousel = "media_type" in post.keys() and post["media_type"] == "carousel"
    if not is_reel and not post["image_url"] and not is_carousel:
        hint = ("Instagram only accepts media from a public web address. Set up hosting "
                "(MEDIA_HOST=r2, see the key wizard) so the upload step can create one.")
        if _local_file(post, "video_path"):
            raise PublishError(f"Instagram needs a hosted video link for this video. {hint}")
        if _local_file(post, "jpeg_path", "image_path"):
            raise PublishError(f"Instagram needs a hosted image link for this image. {hint}")
        raise PublishError("Instagram needs image_url (a public JPEG link) or video_url. Post has neither.")

    creds = meta_credentials(post)
    token, ig_id = creds["token"], creds["ig_id"]
    if not ig_id:
        raise PublishError("This Facebook Page has no Instagram business account linked to it.")
    if "media_type" in post.keys() and post["media_type"] == "carousel":
        return publish_instagram_carousel(post, ig_id, token)

    # Step 1: create the media container.
    if is_reel:
        data = {"media_type": "REELS", "video_url": post["video_url"],
                "caption": full_caption(post), "share_to_feed": "true", "access_token": token}
    else:
        data = {"image_url": post["image_url"], "caption": full_caption(post), "access_token": token}
        if alt_text(post):
            data["alt_text"] = alt_text(post)
    if is_ai_media(post):
        data["is_ai_generated"] = "true"      # Instagram's AI info label (Content Publishing API, June 2026)
    payload = meta_request("POST", f"{META_GRAPH_URL}/{ig_id}/media", data=data)
    container_id = payload.get("id")
    if not container_id:
        raise PublishError(f"Instagram returned no container id: {payload}")

    # Step 2: wait until Instagram has fetched and processed the media.
    _poll_container(container_id, token, IG_REEL_POLL_TRIES if is_reel else IG_CONTAINER_POLL_TRIES,
                    "reel" if is_reel else "image")

    # Step 3: publish the container (retrying "not ready yet" a few times).
    payload = _media_publish(ig_id, container_id, token)
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


def _threads_request(method: str, path: str, token: str, **params) -> dict:
    params["access_token"] = token
    try:
        resp = requests.request(method, f"{THREADS_API_URL}/{path}", params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise PublishError(f"Network error talking to Threads: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise PublishError(f"Threads returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400 or not isinstance(payload, dict) or "error" in payload:
        raise meta_error(payload, platform="Threads")
    return payload


def publish_threads(post) -> str:
    """Publish to Threads: text, or image/video from a hosted URL (Threads fetches it)."""
    _check_caption(post, "threads")
    token = get_secret("THREADS_ACCESS_TOKEN", post["user_id"])
    me = _threads_request("GET", "me", token, fields="id,username")
    user = me.get("id")
    if not user:
        raise PublishError("Threads did not return the account id for this token.")
    text = full_caption(post)[:500]
    if post["video_url"]:
        params = {"media_type": "VIDEO", "video_url": post["video_url"], "text": text}
    elif post["image_url"]:
        params = {"media_type": "IMAGE", "image_url": post["image_url"], "text": text}
    else:
        if _local_file(post, "video_path") or _local_file(post, "jpeg_path", "image_path"):
            raise PublishError("Threads needs a hosted link for media (set up hosting, MEDIA_HOST=r2). "
                               "Text-only posts work without it.")
        if not text.strip():
            raise PublishError("Nothing to post to Threads: no media link and an empty caption.")
        params = {"media_type": "TEXT", "text": text}
    container = _threads_request("POST", f"{user}/threads", token, **params).get("id")
    if not container:
        raise PublishError("Threads returned no container id.")
    if params["media_type"] != "TEXT":
        failures = 0
        for _ in range(THREADS_POLL_TRIES):
            try:
                info = _threads_request("GET", container, token, fields="status,error_message")
            except PublishError as exc:
                failures += 1
                if failures >= IG_POLL_ERROR_TOLERANCE or not (exc.transient or exc.code == 100):
                    raise
                time.sleep(IG_CONTAINER_POLL_WAIT)
                continue
            failures, status = 0, info.get("status")
            if status in ("FINISHED", "PUBLISHED"):
                break
            if status == "ERROR":
                raise PublishError(f"Threads could not process the media: {info.get('error_message') or 'no details'}")
            if status == "EXPIRED":
                raise PublishError("The Threads container expired before it was published.", transient=True)
            time.sleep(IG_CONTAINER_POLL_WAIT)
        else:
            raise PublishError("Threads media processing timed out. It will be retried on a later cycle.", transient=True)
    published = _threads_request("POST", f"{user}/threads_publish", token, creation_id=container)
    post_id = published.get("id")
    if not post_id:
        raise PublishError(f"Threads returned no post id: {published}")
    return str(post_id)


def publish_youtube(post) -> str:
    """Upload the post's video as a YouTube Short."""
    from src import youtube
    video = _local_file(post, "video_path")
    if not video:
        raise PublishError("YouTube needs the video file on this computer (video_path). Image posts cannot go to YouTube.")
    hashtags = (post["hashtags"] or "").split()
    try:
        return youtube.upload_short(post["user_id"], video, post["topic"] or (post["caption"] or "")[:60],
                                    full_caption(post), hashtags,
                                    privacy=os.getenv("YT_PRIVACY", "public"))
    except youtube.YouTubeError as exc:
        raise PublishError(str(exc)) from exc


PUBLISHERS = {
    "facebook": publish_facebook,
    "instagram": publish_instagram,
    "tiktok": publish_tiktok,
    "threads": publish_threads,
    "youtube": publish_youtube,
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


def delete_facebook_post(post, fb_post_id: str) -> None:
    """Remove a post from the Facebook Page (Instagram and TikTok have no delete API)."""
    token = meta_credentials(post)["token"]
    meta_request("DELETE", f"{META_GRAPH_URL}/{fb_post_id}", params={"access_token": token})


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
    attempts = (post["publish_attempts"] if "publish_attempts" in post.keys() else 0) or 0
    if not dry_run and attempts >= MAX_PUBLISH_ATTEMPTS:
        log.warning("  Post #%s failed %d times; not retrying automatically. Fix the cause and use Publish again.",
                    pid, attempts)
        return False
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
            check_platform_config(platform, post["user_id"], post)
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
        if platform in ("facebook", "instagram"):
            try:
                comment_id = post_first_comment(post, platform, platform_post_id)
                if comment_id:
                    with get_connection() as conn:
                        conn.execute(f"UPDATE posts SET {'fb' if platform == 'facebook' else 'ig'}_comment_id = ? "
                                     "WHERE id = ?", (comment_id, pid))
                    log.info("  %-9s hashtags posted as first comment", platform)
            except (PublishError, ConfigError) as exc:
                log.warning("  %-9s published, but the hashtag comment failed: %s", platform, exc)

    if dry_run:
        return False

    if errors:
        attempts += 1
        gave_up = attempts >= MAX_PUBLISH_ATTEMPTS
        message = " | ".join(errors) + (f" (failed {attempts} times; automatic retries stopped, use Publish again)"
                                        if gave_up else "")
        save_error(pid, message)
        with get_connection() as conn:
            conn.execute("UPDATE posts SET publish_attempts = ? WHERE id = ?", (attempts, pid))
        log.warning("  Post #%s NOT marked published (attempt %d/%d).", pid, attempts, MAX_PUBLISH_ATTEMPTS)
        from src import notify
        if attempts == 1 or gave_up:
            notify.failed(post["user_id"], dict(post), message)
        return False

    _maybe_story(post)
    mark_published(pid)
    from src import notify
    notify.published(post["user_id"], dict(post), platforms)
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
