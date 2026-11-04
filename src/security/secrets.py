"""
Single place every script reads API tokens from, per user.

Lookup order for get_secret("META_ACCESS_TOKEN", user_id):
    1. the user's own encrypted value (entered on their Settings page)
    2. the .env file / environment variable (platform default, e.g. the
       operator's OpenAI key that the SaaS pays for)
    3. a platform-level encrypted value (user_id 0, set by the operator)
    4. otherwise ConfigError naming the variable

Values written to the database are always AES-256-GCM encrypted first
(see src/security/crypto.py). Plain-text tokens are never written anywhere.
A user can only ever read or write rows with their own user_id.
"""

import os
from datetime import datetime

from config.db import get_connection
from src.security import crypto

PLATFORM_USER_ID = 0

# Everything the pipeline can need. Shown on the dashboard settings page.
KNOWN_SECRETS = {
    "GEMINI_API_KEY": "Google Gemini (captions, photo/video understanding; free tier)",
    "OPENAI_API_KEY": "OpenAI (captions, images, speech; paid)",
    "CF_API_TOKEN": "Cloudflare API token with Workers AI (free image generation)",
    "GROQ_API_KEY": "Groq (free, fast captions; text only)",
    "XAI_API_KEY": "xAI Grok (captions, photo description, pictures; paid per use)",
    "PEXELS_API_KEY": "Pexels (free stock photos and clips)",
    "PIXABAY_API_KEY": "Pixabay (free stock photos and clips)",
    "UNSPLASH_ACCESS_KEY": "Unsplash (free photos; credit line added)",
    "FLICKR_API_KEY": "Flickr (Creative Commons photos only; credit line added)",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token (notifications)",
    "TELEGRAM_CHAT_ID": "Telegram chat id to notify",
    "THREADS_ACCESS_TOKEN": "Threads access token (threads_basic + threads_content_publish)",
    "YT_CLIENT_ID": "Google OAuth client id (YouTube Shorts)",
    "YT_CLIENT_SECRET": "Google OAuth client secret (YouTube Shorts)",
    "ELEVENLABS_API_KEY": "ElevenLabs text-to-speech (optional)",
    "META_ACCESS_TOKEN": "Meta page access token (Facebook + Instagram)",
    "META_PAGE_ID": "Facebook Page ID",
    "META_INSTAGRAM_ACCOUNT_ID": "Instagram Business account ID",
    "TIKTOK_CLIENT_KEY": "TikTok app client key (Login Kit)",
    "TIKTOK_CLIENT_SECRET": "TikTok app client secret",
    "TIKTOK_ACCESS_TOKEN": "TikTok access token (old manual way, expires every 24h)",
    "IMGUR_CLIENT_ID": "Imgur Client ID (image hosting)",
    "R2_ACCOUNT_ID": "Cloudflare R2 account ID",
    "R2_ACCESS_KEY_ID": "Cloudflare R2 access key",
    "R2_SECRET_ACCESS_KEY": "Cloudflare R2 secret key",
    "R2_BUCKET": "Cloudflare R2 bucket name",
    "R2_PUBLIC_URL": "Cloudflare R2 public URL",
}


class ConfigError(Exception):
    """Raised when a required token or ID is missing or still a placeholder."""


def is_placeholder(value: str | None) -> bool:
    value = (value or "").strip()
    return not value or value.startswith("your_") or value.endswith("_here")


def _from_env(name: str) -> str | None:
    value = os.getenv(name)
    return None if is_placeholder(value) else value.strip()


def _from_db(name: str, user_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value_encrypted FROM credentials WHERE user_id = ? AND name = ?",
                           (user_id, name)).fetchone()
    if row is None:
        return None
    try:
        return crypto.decrypt(row["value_encrypted"])
    except crypto.CryptoError as exc:
        raise ConfigError(f"{name} is stored encrypted but cannot be read: {exc}") from exc


def get_secret(name: str, user_id: int | None = None) -> str:
    """Return the value for `name` for this user, or raise ConfigError."""
    value = _from_db(name, user_id) if user_id else None
    if value is None:
        value = _from_env(name)
    if value is None:
        value = _from_db(name, PLATFORM_USER_ID)
    if value is None:
        raise ConfigError(
            f"{name} is not set. Add it on the dashboard Settings page (or to .env).")
    return value


def set_secret(name: str, value: str, user_id: int = PLATFORM_USER_ID) -> None:
    """Encrypt and store a value for one user. Raises CryptoError if APP_SECRET_KEY is missing."""
    if is_placeholder(value):
        raise ValueError("Refusing to store an empty or placeholder value.")
    encrypted = crypto.encrypt(value.strip())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO credentials (user_id, name, value_encrypted, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET value_encrypted = excluded.value_encrypted, "
            "updated_at = excluded.updated_at",
            (user_id, name, encrypted, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def delete_secret(name: str, user_id: int = PLATFORM_USER_ID) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM credentials WHERE user_id = ? AND name = ?", (user_id, name))


def mask(value: str) -> str:
    """Show only the last 4 characters: 'EAAB...k9Qz'."""
    if len(value) <= 4:
        return "****"
    return value[:3] + "..." + value[-4:] if len(value) > 10 else "****" + value[-4:]


def describe_secrets(user_id: int | None = None) -> list[dict]:
    """
    For the settings page: where each known secret comes from, masked.
    source: 'yours' (this user's value), 'platform' (.env or operator default), or None.
    """
    with get_connection() as conn:
        mine = {r["name"]: r["updated_at"] for r in conn.execute(
            "SELECT name, updated_at FROM credentials WHERE user_id = ?", (user_id or -1,))}
    out = []
    for name, label in KNOWN_SECRETS.items():
        entry = {"name": name, "label": label, "source": None, "masked": "", "updated_at": None}
        if user_id and name in mine:
            try:
                entry.update(source="yours", masked=mask(_from_db(name, user_id) or ""),
                             updated_at=mine[name])
            except ConfigError:
                entry.update(source="yours", masked="(cannot decrypt)", updated_at=mine[name])
        else:
            platform = _from_env(name)
            if platform is None:
                try:
                    platform = _from_db(name, PLATFORM_USER_ID)
                except ConfigError:
                    platform = None
            if platform is not None:
                entry.update(source="platform", masked=mask(platform))
        out.append(entry)
    return out
