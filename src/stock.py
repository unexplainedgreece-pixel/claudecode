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

from src.security.secrets import ConfigError, get_secret

log = logging.getLogger("stock")
PEXELS_PHOTOS = "https://api.pexels.com/v1/search"
PEXELS_VIDEOS = "https://api.pexels.com/videos/search"
PIXABAY_PHOTOS = "https://pixabay.com/api/"
PIXABAY_VIDEOS = "https://pixabay.com/api/videos/"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "SocialAutomation/1.0 (self-hosted social media tool; contact via dashboard owner)"
# Wikimedia licences we accept (free to reuse commercially). Everything else is skipped.
FREE_LICENSES = ("cc0", "public domain", "pd", "cc by", "cc-by", "attribution")
BLOCKED_LICENSE_WORDS = ("nc", "nd", "fair use", "non-commercial", "noncommercial", "gfdl only")
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


# ---------------------------------------------------------------------------
# Pixabay (free API key) and Wikimedia Commons (no key)
# ---------------------------------------------------------------------------

def _pixabay(url: str, params: dict, user_id) -> dict:
    params = {"key": get_secret("PIXABAY_API_KEY", user_id), "safesearch": "true", **params}
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise StockError(f"Could not reach Pixabay: {exc}") from exc
    if resp.status_code == 429:
        raise StockError("Pixabay rate limit reached. Try again later.")
    if resp.status_code >= 400:
        raise StockError(f"Pixabay error (HTTP {resp.status_code}): {resp.text[:120]}")
    try:
        return resp.json()
    except ValueError:
        raise StockError("Pixabay returned something unreadable.")


def search_pixabay_photo(query: str, user_id=None) -> dict:
    data = _pixabay(PIXABAY_PHOTOS, {"q": query[:100], "image_type": "photo", "per_page": 5, "min_width": 1000}, user_id)
    hits = data.get("hits") or []
    if not hits:
        raise StockError(f"Pixabay has no photo for '{query}'.")
    h = hits[0]
    return {"url": h.get("largeImageURL") or h.get("webformatURL"), "credit": f"Photo by {h.get('user', 'unknown')} on Pixabay",
            "link": h.get("pageURL"), "id": h.get("id"), "attribution_required": False, "source": "pixabay"}


def search_pixabay_video(query: str, user_id=None, max_seconds: int = 30) -> dict:
    data = _pixabay(PIXABAY_VIDEOS, {"q": query[:100], "per_page": 10}, user_id)
    hits = [h for h in (data.get("hits") or []) if (h.get("duration") or 0) <= max_seconds]
    if not hits:
        raise StockError(f"Pixabay has no short clip for '{query}'.")
    h = hits[0]
    files = h.get("videos") or {}
    best = files.get("large") or files.get("medium") or files.get("small") or {}
    if not best.get("url"):
        raise StockError("Pixabay clip has no downloadable file.")
    return {"url": best["url"], "credit": f"Video by {h.get('user', 'unknown')} on Pixabay", "link": h.get("pageURL"),
            "duration": h.get("duration"), "id": h.get("id"), "attribution_required": False, "source": "pixabay"}


def _license_ok(name: str) -> bool:
    n = (name or "").lower()
    if not n or any(bad in n for bad in BLOCKED_LICENSE_WORDS):
        return False
    return any(good in n for good in FREE_LICENSES)


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()


def search_wikimedia(query: str, min_width: int = 1000) -> dict:
    """Best free-licence photo on Wikimedia Commons for a real place or object.
    Returns {"url", "credit", "link", "license", "attribution_required", "source"}."""
    params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
              "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime",
              "iiurlwidth": 1600}
    try:
        resp = requests.get(WIKIMEDIA_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or {}
    except (requests.RequestException, ValueError) as exc:
        raise StockError(f"Could not search Wikimedia Commons: {exc}") from exc
    candidates = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        license_name = (meta.get("LicenseShortName") or {}).get("value") or ""
        if not _license_ok(license_name) or (info.get("mime") or "") not in ("image/jpeg", "image/png"):
            continue
        if (info.get("width") or 0) < min_width:
            continue
        artist = _strip_html((meta.get("Artist") or {}).get("value") or "")[:80] or "unknown"
        candidates.append({
            "url": info.get("thumburl") or info.get("url"),
            "link": info.get("descriptionurl") or info.get("descriptionshorturl"),
            "license": license_name,
            "credit": f"Photo: {artist} ({license_name}) via Wikimedia Commons",
            "attribution_required": not any(k in license_name.lower() for k in ("cc0", "public domain", "pd")),
            "source": "wikimedia", "width": info.get("width") or 0, "index": page.get("index", 99),
        })
    if not candidates:
        raise StockError(f"Wikimedia Commons has no free photo for '{query}'.")
    candidates.sort(key=lambda c: c["index"])
    return candidates[0]


def find_photo(query: str, user_id=None, sources=("wikimedia", "pexels", "pixabay")) -> dict:
    """Try the free libraries in order; the first hit wins. Raises StockError when all fail."""
    errors = []
    for source in sources:
        try:
            if source == "wikimedia":
                return search_wikimedia(query)
            if source == "pexels":
                found = search_photo(query, user_id)
                return {**found, "attribution_required": False, "source": "pexels"}
            if source == "pixabay":
                return search_pixabay_photo(query, user_id)
        except (StockError, ConfigError) as exc:
            errors.append(f"{source}: {exc}")
    raise StockError("; ".join(errors) or "no sources")
