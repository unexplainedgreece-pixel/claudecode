"""
YouTube Shorts publishing through the YouTube Data API v3.

Setup (once, about 10 minutes):
  1. https://console.cloud.google.com → create a project → "APIs & Services" →
     enable "YouTube Data API v3".
  2. "OAuth consent screen": External, add yourself as a test user.
  3. "Credentials" → "Create credentials" → "OAuth client ID" → type Web application,
     authorised redirect URI: http://127.0.0.1:8000/settings/youtube/callback
     Copy the Client ID and Client secret into YT_CLIENT_ID / YT_CLIENT_SECRET on API settings.
  4. API settings → "Connect YouTube" → approve in Google → done. The refresh
     token is stored encrypted per user.

Quota: Google gives 10,000 units a day and one upload costs 1,600, so about
six Shorts a day per project unless you ask Google for more.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

from config.db import get_connection
from src.security import crypto
from src.security.secrets import ConfigError, get_secret

log = logging.getLogger("youtube")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
TIMEOUT = 60
UPLOAD_TIMEOUT = 1800
CHUNK = 8 * 1024 * 1024


class YouTubeError(Exception):
    pass


def redirect_uri(base_url: str) -> str:
    return base_url.rstrip("/") + "/settings/youtube/callback"


def auth_url(user_id: int, base_url: str, state: str) -> str:
    params = {"client_id": get_secret("YT_CLIENT_ID", user_id), "redirect_uri": redirect_uri(base_url),
              "response_type": "code", "scope": SCOPES, "access_type": "offline", "prompt": "consent",
              "state": state}
    return AUTH_URL + "?" + urlencode(params)


def _token_request(data: dict) -> dict:
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise YouTubeError(f"Could not reach Google: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise YouTubeError(f"Google returned something unreadable (HTTP {resp.status_code}).")
    if resp.status_code >= 400 or "error" in payload:
        raise YouTubeError(f"Google refused: {payload.get('error_description') or payload.get('error') or payload}")
    return payload


def exchange_code(user_id: int, code: str, base_url: str) -> dict:
    """Turn the OAuth code into tokens, fetch the channel, store everything encrypted."""
    payload = _token_request({"code": code, "client_id": get_secret("YT_CLIENT_ID", user_id),
                              "client_secret": get_secret("YT_CLIENT_SECRET", user_id),
                              "redirect_uri": redirect_uri(base_url), "grant_type": "authorization_code"})
    refresh = payload.get("refresh_token")
    if not refresh:
        raise YouTubeError("Google did not return a refresh token. Remove the app at "
                           "https://myaccount.google.com/permissions and connect again.")
    access = payload.get("access_token")
    channel = _channel(access)
    record = {"refresh_token": refresh, "channel_id": channel.get("id"), "channel_title": channel.get("title"),
              "connected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO credentials (user_id, name, value_encrypted, updated_at) VALUES (?, 'YT_CONNECTION', ?, ?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET value_encrypted = excluded.value_encrypted, "
            "updated_at = excluded.updated_at",
            (user_id, crypto.encrypt(json.dumps(record)), record["connected_at"]))
    return record


def _channel(access_token: str) -> dict:
    try:
        resp = requests.get(CHANNEL_URL, params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT)
        items = resp.json().get("items") or []
    except (requests.RequestException, ValueError):
        return {}
    if not items:
        return {}
    return {"id": items[0].get("id"), "title": (items[0].get("snippet") or {}).get("title")}


def connection(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value_encrypted FROM credentials WHERE user_id = ? AND name = 'YT_CONNECTION'",
                           (user_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(crypto.decrypt(row["value_encrypted"]))
    except crypto.CryptoError:
        return None


def disconnect(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM credentials WHERE user_id = ? AND name = 'YT_CONNECTION'", (user_id,))


def access_token(user_id: int) -> str:
    conn_info = connection(user_id)
    if not conn_info:
        raise ConfigError("YouTube is not connected. Use 'Connect YouTube' on the API settings page.")
    payload = _token_request({"refresh_token": conn_info["refresh_token"], "client_id": get_secret("YT_CLIENT_ID", user_id),
                              "client_secret": get_secret("YT_CLIENT_SECRET", user_id), "grant_type": "refresh_token"})
    return payload["access_token"]


def upload_short(user_id: int, video_path: Path, title: str, description: str, tags: list[str],
                 privacy: str = "public") -> str:
    """Resumable upload. Returns the YouTube video id."""
    if not video_path.exists():
        raise YouTubeError(f"Video file not found: {video_path}")
    token = access_token(user_id)
    title = (title or "Short")[:95]
    if "#shorts" not in title.lower() and "#shorts" not in description.lower():
        description = (description + "\n\n#Shorts").strip()
    body = {"snippet": {"title": title, "description": description[:5000], "tags": [t.lstrip("#") for t in tags][:30],
                        "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    size = video_path.stat().st_size
    try:
        start = requests.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                                       "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/*"},
                              json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise YouTubeError(f"Could not reach YouTube: {exc}") from exc
    if start.status_code == 403:
        raise YouTubeError("YouTube refused the upload (quota used up for today, or the API is not enabled).")
    if start.status_code >= 400:
        raise YouTubeError(f"YouTube refused to start the upload (HTTP {start.status_code}): {start.text[:300]}")
    session = start.headers.get("Location")
    if not session:
        raise YouTubeError("YouTube gave no upload session.")
    with open(video_path, "rb") as fh:
        try:
            resp = requests.put(session, data=fh, headers={"Content-Length": str(size), "Content-Type": "video/*"},
                                timeout=UPLOAD_TIMEOUT)
        except requests.RequestException as exc:
            raise YouTubeError(f"Upload interrupted: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise YouTubeError(f"Upload failed (HTTP {resp.status_code}): {resp.text[:300]}")
    try:
        video_id = resp.json().get("id")
    except ValueError:
        video_id = None
    if not video_id:
        raise YouTubeError("YouTube returned no video id.")
    return video_id
