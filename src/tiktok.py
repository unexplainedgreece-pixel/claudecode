"""
TikTok: Login Kit (OAuth with PKCE, desktop-app style) + Content Posting API.

Setup (once):
  1. https://developers.tiktok.com -> Manage apps -> Connect an app. Platform: Desktop.
  2. Add the products Login Kit and Content Posting API. Scopes: user.info.basic,
     video.upload, video.publish.
  3. Redirect URI (Login Kit): http://127.0.0.1:8000/settings/tiktok/callback
  4. Copy Client key -> TIKTOK_CLIENT_KEY, Client secret -> TIKTOK_CLIENT_SECRET on
     API settings, then "Connect TikTok". The refresh token (valid a year) is stored
     encrypted; the 24-hour access token is refreshed by itself.

Posting modes (TIKTOK_POST_MODE in .env):
  inbox  (default) the video lands in the creator's TikTok inbox as a draft and
         is published from the TikTok app with one tap. Works for unaudited apps.
  direct publishes straight away through Direct Post. Until TikTok audits the app
         every post is private (SELF_ONLY), and at most 5 users may post per day.

Videos are uploaded as a file (FILE_UPLOAD), so no domain verification is needed.
Photo posts still need PULL_FROM_URL from a verified domain and keep the old path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as pysecrets
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

from config.db import get_connection
from src.security import crypto
from src.security.secrets import ConfigError, get_secret

log = logging.getLogger("tiktok")
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
# video.publish (direct posting) only exists after TikTok reviews the app; asking for a
# scope the app does not have makes the login fail, so it is requested only in direct mode.
SCOPES = "user.info.basic,video.upload" + (",video.publish" if os.getenv("TIKTOK_POST_MODE", "inbox").strip().lower() == "direct" else "")
TIMEOUT = 60
UPLOAD_TIMEOUT = 1800
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
POST_MODE = os.getenv("TIKTOK_POST_MODE", "inbox").strip().lower()      # inbox | direct
PRIVACY = os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY").strip().upper()
CONNECTION_NAME = "TT_CONNECTION"


class TikTokError(Exception):
    pass


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def redirect_uri(base_url: str) -> str:
    return base_url.rstrip("/") + "/settings/tiktok/callback"


def new_verifier() -> str:
    return pysecrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    """TikTok's desktop PKCE: hex-encoded SHA-256 of the verifier (not base64url)."""
    return hashlib.sha256(verifier.encode()).hexdigest()


def auth_url(user_id: int, base_url: str, state: str, verifier: str) -> str:
    params = {"client_key": get_secret("TIKTOK_CLIENT_KEY", user_id), "scope": SCOPES, "response_type": "code",
              "redirect_uri": redirect_uri(base_url), "state": state,
              "code_challenge": code_challenge(verifier), "code_challenge_method": "S256"}
    return AUTH_URL + "?" + urlencode(params)


def _token_request(data: dict) -> dict:
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=TIMEOUT,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    except requests.RequestException as exc:
        raise TikTokError(f"Could not reach TikTok: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise TikTokError(f"TikTok returned something unreadable (HTTP {resp.status_code}).")
    if resp.status_code >= 400 or "error" in payload or not payload.get("access_token"):
        raise TikTokError(f"TikTok refused: {payload.get('error_description') or payload.get('error') or payload}")
    return payload


def _store(user_id: int, record: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO credentials (user_id, name, value_encrypted, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET value_encrypted = excluded.value_encrypted, "
            "updated_at = excluded.updated_at",
            (user_id, CONNECTION_NAME, crypto.encrypt(json.dumps(record)), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def _record_from_tokens(payload: dict, old: dict | None = None) -> dict:
    now = datetime.now()
    record = dict(old or {})
    record.update({
        "access_token": payload["access_token"],
        "access_expires_at": (now + timedelta(seconds=int(payload.get("expires_in") or 86400))).strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_token": payload.get("refresh_token") or record.get("refresh_token"),
        "refresh_expires_at": (now + timedelta(seconds=int(payload.get("refresh_expires_in") or 31536000))).strftime("%Y-%m-%d %H:%M:%S"),
        "open_id": payload.get("open_id") or record.get("open_id"),
        "scope": payload.get("scope") or record.get("scope"),
    })
    return record


def exchange_code(user_id: int, code: str, base_url: str, verifier: str) -> dict:
    payload = _token_request({"client_key": get_secret("TIKTOK_CLIENT_KEY", user_id),
                              "client_secret": get_secret("TIKTOK_CLIENT_SECRET", user_id),
                              "code": code, "grant_type": "authorization_code",
                              "redirect_uri": redirect_uri(base_url), "code_verifier": verifier})
    if not payload.get("refresh_token"):
        raise TikTokError("TikTok did not return a refresh token. Remove the app under TikTok > Settings > "
                          "Security > Manage app permissions and connect again.")
    record = _record_from_tokens(payload)
    record["connected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record["display_name"] = _display_name(record["access_token"])
    _store(user_id, record)
    return record


def _display_name(access_token: str) -> str | None:
    try:
        resp = requests.get(f"{API}/user/info/", params={"fields": "open_id,display_name"},
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT)
        return ((resp.json().get("data") or {}).get("user") or {}).get("display_name")
    except (requests.RequestException, ValueError, AttributeError):
        return None


def connection(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value_encrypted FROM credentials WHERE user_id = ? AND name = ?",
                           (user_id, CONNECTION_NAME)).fetchone()
    if not row:
        return None
    try:
        return json.loads(crypto.decrypt(row["value_encrypted"]))
    except (crypto.CryptoError, ValueError):
        return None


def disconnect(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM credentials WHERE user_id = ? AND name = ?", (user_id, CONNECTION_NAME))


def access_token(user_id: int) -> str:
    """A valid access token, refreshed when it is about to expire (TikTok's last 24 h)."""
    info = connection(user_id)
    if not info:
        raise ConfigError("TikTok is not connected. Use 'Connect TikTok' on the API settings page.")
    soon = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    if info.get("access_expires_at", "") > soon:
        return info["access_token"]
    if info.get("refresh_expires_at", "") <= datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        raise ConfigError("TikTok's yearly refresh token has expired. Use 'Connect TikTok' again on the API settings page.")
    payload = _token_request({"client_key": get_secret("TIKTOK_CLIENT_KEY", user_id),
                              "client_secret": get_secret("TIKTOK_CLIENT_SECRET", user_id),
                              "grant_type": "refresh_token", "refresh_token": info["refresh_token"]})
    record = _record_from_tokens(payload, info)
    _store(user_id, record)
    log.info("TikTok access token refreshed for user %s", user_id)
    return record["access_token"]


# ---------------------------------------------------------------------------
# Content Posting API
# ---------------------------------------------------------------------------

def _api(path: str, token: str, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    try:
        resp = requests.post(f"{API}{path}", headers=headers, json=body or {}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise TikTokError(f"Network error talking to TikTok: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise TikTokError(f"TikTok returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
    if resp.status_code >= 400 or err.get("code", "ok") != "ok":
        code = err.get("code", resp.status_code)
        hint = TIKTOK_HINTS.get(code, "")
        raise TikTokError(f"TikTok API error {code}: {err.get('message', payload)}{' -- ' + hint if hint else ''}")
    return payload.get("data") or {}


TIKTOK_HINTS = {
    "access_token_invalid": "Reconnect TikTok on the API settings page.",
    "scope_not_authorized": "The app lacks the video.upload / video.publish scope. Add it in the TikTok developer portal and reconnect.",
    "unaudited_client_can_only_post_to_private_accounts": "Until TikTok audits the app, only private accounts can receive direct posts. Use TIKTOK_POST_MODE=inbox or set the TikTok account to private.",
    "spam_risk_too_many_posts": "TikTok's daily posting limit was hit. Try again tomorrow.",
    "spam_risk_user_banned_from_posting": "TikTok has blocked this account from posting through the API.",
    "reached_active_user_cap": "Unaudited apps may only post for 5 users per day.",
    "rate_limit_exceeded": "TikTok's rate limit was hit; the post will be retried later.",
}


def creator_info(token: str) -> dict:
    """Who we are posting as, which privacy levels they allow and the max video length."""
    return _api("/post/publish/creator_info/query/", token)


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) following TikTok's rules: whole file in one
    chunk when it fits in 64 MB, otherwise 64 MB chunks with the remainder merged
    into the last one."""
    if size <= MAX_CHUNK:
        return size, 1
    count = size // MAX_CHUNK
    return MAX_CHUNK, count


def _put_chunks(upload_url: str, video_path: Path, size: int, chunk_size: int, count: int) -> None:
    with open(video_path, "rb") as fh:
        for i in range(count):
            start = i * chunk_size
            length = chunk_size if i < count - 1 else size - start
            data = fh.read(length)
            headers = {"Content-Type": "video/mp4", "Content-Length": str(length),
                       "Content-Range": f"bytes {start}-{start + length - 1}/{size}"}
            try:
                resp = requests.put(upload_url, data=data, headers=headers, timeout=UPLOAD_TIMEOUT)
            except requests.RequestException as exc:
                raise TikTokError(f"Upload to TikTok failed on chunk {i + 1}/{count}: {exc}") from exc
            if resp.status_code not in (200, 201, 206):
                raise TikTokError(f"TikTok refused chunk {i + 1}/{count} (HTTP {resp.status_code}): {resp.text[:200]}")


def upload_video(user_id: int, video_path: Path, caption: str, mode: str | None = None) -> str:
    """Upload a local video. mode 'inbox' -> draft in the TikTok app; 'direct' -> posted.
    Returns TikTok's publish_id."""
    mode = (mode or POST_MODE)
    if not video_path.exists():
        raise TikTokError(f"Video file not found: {video_path}")
    token = access_token(user_id)
    size = video_path.stat().st_size
    chunk_size, count = chunk_plan(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}
    if mode == "direct":
        info = creator_info(token)
        options = info.get("privacy_level_options") or []
        privacy = PRIVACY if PRIVACY in options else (options[0] if options else PRIVACY)
        limit = info.get("max_video_post_duration_sec")
        body = {"post_info": {"title": caption[:2200], "privacy_level": privacy, "disable_comment": False,
                              "disable_duet": False, "disable_stitch": False},
                "source_info": source}
        data = _api("/post/publish/video/init/", token, body)
        log.info("  tiktok    direct post as %s (privacy %s%s)", info.get("creator_username") or "creator", privacy,
                 f", max {limit}s" if limit else "")
    else:
        data = _api("/post/publish/inbox/video/init/", token, {"source_info": source})
        log.info("  tiktok    uploading to the TikTok inbox (publish it from the app)")
    publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
    if not publish_id or not upload_url:
        raise TikTokError(f"TikTok returned no upload target: {data}")
    _put_chunks(upload_url, video_path, size, chunk_size, count)
    status = _api("/post/publish/status/fetch/", token, {"publish_id": publish_id})
    if status.get("status") == "FAILED":
        raise TikTokError(f"TikTok rejected the video: {status.get('fail_reason') or status}")
    return str(publish_id)
