"""
Connected Facebook Pages (each with its Instagram business account).

A user pastes one extended *user* token from the Graph API Explorer; the tool
asks Meta for every Page that token can manage, each Page's own access token
and its linked Instagram account, and stores them (token encrypted). Posts
then carry a page_id, and the publisher uses that Page's credentials.

Falls back to the single META_* settings when a post has no page.
"""

import logging
import os
from datetime import datetime

import requests

from config.db import get_connection
from src.security import crypto

log = logging.getLogger("pages")
GRAPH = "https://graph.facebook.com/" + os.getenv("META_GRAPH_VERSION", "v25.0")
TIMEOUT = 30


class PagesError(Exception):
    """Meta refused the token, or returned nothing usable. Message is user-safe."""


def _graph_get(path: str, token: str, fields: str) -> dict:
    params = {"access_token": token}
    if fields:
        params["fields"] = fields
    if path == "debug_token":
        params["input_token"] = token
    try:
        resp = requests.get(f"{GRAPH}/{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise PagesError(f"Could not reach Facebook: {exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        raise PagesError(f"Facebook returned something unreadable (HTTP {resp.status_code}).")
    if "error" in payload:
        err = payload["error"]
        raise PagesError(f"Facebook said: {err.get('message', err)} (code {err.get('code', '?')})")
    return payload


def fetch_pages(user_token: str) -> list[dict]:
    """Every Page the token can manage, with page token and Instagram account."""
    payload = _graph_get("me/accounts", user_token,
                         "id,name,access_token,instagram_business_account{id,username}")
    pages = []
    for item in payload.get("data", []):
        ig = item.get("instagram_business_account") or {}
        pages.append({"fb_page_id": item["id"], "name": item.get("name", item["id"]),
                      "token": item["access_token"], "ig_account_id": ig.get("id"),
                      "ig_username": ig.get("username")})
    if not pages:
        raise PagesError("That token can't manage any Page. Generate it with pages_show_list and "
                         "pages_manage_posts, and tick your Page in the popup.")
    return pages


def connect(user_id: int, user_token: str) -> list[dict]:
    """Fetch and store all Pages for this user. Returns what was stored."""
    user_token = (user_token or "").strip()
    if not user_token.startswith("EAA"):
        raise PagesError("That does not look like a Meta access token (they start with EAA).")
    pages = fetch_pages(user_token)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        for p in pages:
            conn.execute(
                "INSERT INTO pages (user_id, name, fb_page_id, ig_account_id, ig_username, token_encrypted, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, fb_page_id) DO UPDATE SET name = excluded.name, "
                "ig_account_id = excluded.ig_account_id, ig_username = excluded.ig_username, "
                "token_encrypted = excluded.token_encrypted",
                (user_id, p["name"], p["fb_page_id"], p["ig_account_id"], p["ig_username"],
                 crypto.encrypt(p["token"]), now))
    log.info("User %s connected %d page(s): %s", user_id, len(pages), ", ".join(p["name"] for p in pages))
    return list_pages(user_id)


def list_pages(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, user_id, name, fb_page_id, ig_account_id, ig_username, created_at FROM pages "
            "WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_page(page_id: int, user_id: int | None = None) -> dict | None:
    """One connected page with its decrypted token, or None."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    if not row or (user_id is not None and row["user_id"] != user_id):
        return None
    page = dict(row)
    page["token"] = crypto.decrypt(page.pop("token_encrypted"))
    return page


def delete_page(page_id: int, user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM pages WHERE id = ? AND user_id = ?", (page_id, user_id))
        conn.execute("UPDATE posts SET page_id = NULL WHERE page_id = ? AND user_id = ?", (page_id, user_id))


def check_token(page: dict) -> dict:
    """Ask Meta whether this Page token still works and when it expires. Records the result."""
    status, expires = "ok", None
    try:
        payload = _graph_get("debug_token", page["token"], "")   # fields ignored by this endpoint
    except PagesError:
        payload = {}
    data = payload.get("data") or {}
    if data:
        if not data.get("is_valid", True):
            status = "invalid"
        exp = data.get("expires_at") or 0
        if exp:
            expires = datetime.fromtimestamp(int(exp)).strftime("%Y-%m-%d %H:%M:%S")
        dae = data.get("data_access_expires_at") or 0
        if dae and (not exp or int(dae) < int(exp)):
            expires = datetime.fromtimestamp(int(dae)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        try:
            _graph_get("me", page["token"], "id")
        except PagesError as exc:
            status = "invalid"
            log.warning("Page %s token invalid: %s", page["name"], exc)
    with get_connection() as conn:
        conn.execute("UPDATE pages SET token_checked_at = ?, token_status = ?, token_expires_at = ? WHERE id = ?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status, expires, page["id"]))
    return {"status": status, "expires_at": expires}


def check_all_tokens() -> dict:
    with get_connection() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM pages")]
    results = {"ok": 0, "invalid": 0}
    for pid in ids:
        page = get_page(pid)
        if not page:
            continue
        try:
            r = check_token(page)
            results[r["status"]] = results.get(r["status"], 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Token check crashed for page %s: %s", pid, exc)
    return results


def default_page_id(user_id: int) -> int | None:
    pages = list_pages(user_id)
    return pages[0]["id"] if pages else None
