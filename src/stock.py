"""
Free stock photos and clips from Pexels, used when a post has no image of its
own and no AI image provider is configured (or IMAGE_SOURCE=stock).

Key: PEXELS_API_KEY from https://www.pexels.com/api/ (free: about 200 requests
an hour, 20,000 a month at the time of writing). Pexels asks that photos are
credited where practical; the photographer and link are stored in the post's
media_credit field so you can add them if you want.
"""

import logging
from pathlib import Path

import requests

from src.security.secrets import get_secret

log = logging.getLogger("stock")
PEXELS_PHOTOS = "https://api.pexels.com/v1/search"
PEXELS_VIDEOS = "https://api.pexels.com/videos/search"
TIMEOUT = 60


class StockError(Exception):
    pass


def _get(url: str, params: dict, user_id) -> dict:
    key = get_secret("PEXELS_API_KEY", user_id)
    try:
        resp = requests.get(url, params=params, headers={"Authorization": key}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise StockError(f"Could not reach Pexels: {exc}") from exc
    if resp.status_code == 429:
        raise StockError("Pexels rate limit reached (200/hour). Try again later.")
    if resp.status_code in (401, 403):
        raise StockError("Pexels rejected the API key. Check PEXELS_API_KEY.")
    if resp.status_code >= 400:
        raise StockError(f"Pexels error (HTTP {resp.status_code}).")
    try:
        return resp.json()
    except ValueError:
        raise StockError("Pexels returned something unreadable.")


def search_photo(query: str, user_id=None, orientation: str = "square") -> dict:
    """Best matching photo: {"url": download url, "credit": "Photo by X on Pexels", "link": ...}."""
    data = _get(PEXELS_PHOTOS, {"query": query, "per_page": 5, "orientation": orientation}, user_id)
    photos = data.get("photos") or []
    if not photos:
        raise StockError(f"Pexels has no photo for '{query}'.")
    p = photos[0]
    src = p.get("src") or {}
    return {"url": src.get("large2x") or src.get("large") or src.get("original"),
            "credit": f"Photo by {p.get('photographer', 'unknown')} on Pexels", "link": p.get("url"),
            "id": p.get("id")}


def search_video(query: str, user_id=None, max_seconds: int = 30) -> dict:
    """Best matching short vertical clip: {"url", "credit", "link", "duration"}."""
    data = _get(PEXELS_VIDEOS, {"query": query, "per_page": 10, "orientation": "portrait"}, user_id)
    videos = [v for v in (data.get("videos") or []) if (v.get("duration") or 0) <= max_seconds]
    if not videos:
        raise StockError(f"Pexels has no short clip for '{query}'.")
    v = videos[0]
    files = sorted((f for f in v.get("video_files", []) if f.get("link")),
                   key=lambda f: (f.get("height") or 0), reverse=True)
    best = next((f for f in files if (f.get("height") or 0) <= 1920), files[0] if files else None)
    if not best:
        raise StockError("Pexels clip has no downloadable file.")
    return {"url": best["link"], "credit": f"Video by {(v.get('user') or {}).get('name', 'unknown')} on Pexels",
            "link": v.get("url"), "duration": v.get("duration"), "id": v.get("id")}


def download(url: str, dest: Path) -> Path:
    try:
        resp = requests.get(url, timeout=TIMEOUT * 3, stream=True)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            for chunk in resp.iter_content(1024 * 256):
                out.write(chunk)
    except requests.RequestException as exc:
        raise StockError(f"Download failed: {exc}") from exc
    return dest
