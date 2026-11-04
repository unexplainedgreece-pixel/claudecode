"""
Notifications through a Telegram bot, so you hear about posts waiting for
approval, published posts and failures without watching the dashboard.

Setup (two minutes):
  1. In Telegram, talk to @BotFather, send /newbot, follow the prompts, copy the token.
  2. Open a chat with your new bot and send it any message.
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}.
  4. On API settings save TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
"""

import logging

import requests

from config.db import get_connection
from src.security.secrets import ConfigError, get_secret

log = logging.getLogger("notify")
TIMEOUT = 20


def enabled(user_id: int | None) -> bool:
    if not user_id:
        return False
    with get_connection() as conn:
        row = conn.execute("SELECT notify_telegram FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["notify_telegram"])


def send(user_id: int | None, text: str) -> bool:
    """Send a message. Returns False (and logs) instead of raising: notifications never break a run."""
    if not enabled(user_id):
        return False
    try:
        token = get_secret("TELEGRAM_BOT_TOKEN", user_id)
        chat = get_secret("TELEGRAM_CHAT_ID", user_id)
    except ConfigError:
        return False
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True},
                             timeout=TIMEOUT)
        if resp.status_code != 200:
            log.warning("Telegram refused the message (HTTP %s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.warning("Telegram unreachable: %s", exc)
        return False


def drafts_waiting(user_id: int, count: int, base_url: str = "http://127.0.0.1:8000") -> bool:
    return send(user_id, f"📝 {count} new post(s) are waiting for your approval.\n{base_url}/queue")


def published(user_id: int, post, platforms: list[str]) -> bool:
    link = f"https://www.facebook.com/{post['fb_post_id']}" if post.get("fb_post_id") else ""
    return send(user_id, f"✅ Published: {post.get('topic') or 'post'} → {', '.join(platforms)}\n{link}".strip())


def failed(user_id: int, post, error: str) -> bool:
    return send(user_id, f"⚠️ Could not publish \"{post.get('topic') or 'post'}\":\n{error[:600]}")
