"""
Single place every script reads API tokens from.

Lookup order for get_secret("META_ACCESS_TOKEN"):
    1. the .env file / environment variable (if set and not a placeholder)
    2. the encrypted `credentials` table, entered through the dashboard
    3. otherwise ConfigError with a message that names the variable

Values written to the database are always AES-256-GCM encrypted first
(see src/security/crypto.py). Plain-text tokens are never written anywhere.
"""

import os
from datetime import datetime

from config.db import get_connection
from src.security import crypto

# Everything the pipeline can need. Shown on the dashboard settings page.
KNOWN_SECRETS = {
    "OPENAI_API_KEY": "OpenAI (captions, images, speech)",
    "ELEVENLABS_API_KEY": "ElevenLabs text-to-speech (optional)",
    "META_ACCESS_TOKEN": "Meta page access token (Facebook + Instagram)",
    "META_PAGE_ID": "Facebook Page ID",
    "META_INSTAGRAM_ACCOUNT_ID": "Instagram Business account ID",
    "TIKTOK_ACCESS_TOKEN": "TikTok access token (expires every 24h)",
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


def _from_db(name: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value_encrypted FROM credentials WHERE name = ?",
                           (name,)).fetchone()
    if row is None:
        return None
    try:
        return crypto.decrypt(row["value_encrypted"])
    except crypto.CryptoError as exc:
        raise ConfigError(f"{name} is stored encrypted but cannot be read: {exc}") from exc


def get_secret(name: str) -> str:
    """Return the value for `name` from .env or the encrypted store, or raise ConfigError."""
    value = _from_env(name)
    if value is None:
        value = _from_db(name)
    if value is None:
        raise ConfigError(
            f"{name} is not set. Add it to .env or enter it on the dashboard Settings page."
        )
    return value


def set_secret(name: str, value: str) -> None:
    """Encrypt and store a value. Raises CryptoError if APP_SECRET_KEY is missing."""
    if is_placeholder(value):
        raise ValueError("Refusing to store an empty or placeholder value.")
    encrypted = crypto.encrypt(value.strip())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO credentials (name, value_encrypted, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value_encrypted = excluded.value_encrypted, "
            "updated_at = excluded.updated_at",
            (name, encrypted, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def delete_secret(name: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM credentials WHERE name = ?", (name,))


def mask(value: str) -> str:
    """Show only the last 4 characters: 'EAAB...k9Qz'."""
    if len(value) <= 4:
        return "****"
    return value[:3] + "..." + value[-4:] if len(value) > 10 else "****" + value[-4:]


def describe_secrets() -> list[dict]:
    """For the settings page: where each known secret comes from, masked."""
    with get_connection() as conn:
        db_rows = {r["name"]: r["updated_at"] for r in conn.execute(
            "SELECT name, updated_at FROM credentials")}
    out = []
    for name, label in KNOWN_SECRETS.items():
        env_value = _from_env(name)
        if env_value is not None:
            out.append({"name": name, "label": label, "source": "env",
                        "masked": mask(env_value), "updated_at": None})
        elif name in db_rows:
            try:
                masked = mask(_from_db(name) or "")
            except ConfigError:
                masked = "(cannot decrypt)"
            out.append({"name": name, "label": label, "source": "db",
                        "masked": masked, "updated_at": db_rows[name]})
        else:
            out.append({"name": name, "label": label, "source": None,
                        "masked": "", "updated_at": None})
    return out
