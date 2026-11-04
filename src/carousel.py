"""
Carousel posts: 2-10 photos from the media library in one Instagram / Facebook
post (Instagram calls it a carousel, Facebook a multi-photo post).

A carousel is an ordinary row in `posts` (media_type = 'carousel') whose
photos live in `post_media`, one row per slide in order. The post's own
image_path / image_url mirror the first slide so previews, the uploader's
"needs hosting" query and the dashboard keep working unchanged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from config.db import get_connection

log = logging.getLogger("carousel")
MIN_ITEMS, MAX_ITEMS = 2, 10          # Instagram's limits


class CarouselError(Exception):
    """User-safe message."""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def items(post_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM post_media WHERE post_id = ? ORDER BY position", (post_id,)).fetchall()
    return [dict(r) for r in rows]


def create_from_assets(user_id: int, asset_ids: list[int], topic: str = "", caption: str = "",
                       hashtags: str = "") -> int:
    """Make a carousel draft from library photos (in the given order). Caption and
    hashtags default to what the AI wrote for the first analysed photo."""
    from src import library, pages
    from src.web import queries

    ids = []
    for raw in asset_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    if not MIN_ITEMS <= len(ids) <= MAX_ITEMS:
        raise CarouselError(f"Pick between {MIN_ITEMS} and {MAX_ITEMS} photos for a carousel.")
    assets = []
    for aid in ids:
        asset = library.get_asset(aid, user_id)
        if not asset:
            raise CarouselError("One of the selected files is not in your library.")
        if asset["kind"] != "image" or Path(asset["path"]).suffix.lower() not in library.IMAGE_EXTS:
            raise CarouselError(f"{Path(asset['path']).name} is not a photo. Carousels take photos only.")
        if not Path(asset["path"]).exists():
            raise CarouselError(f"{Path(asset['path']).name} is missing on disk.")
        assets.append(asset)

    first = next((a for a in assets if a.get("caption")), assets[0])
    analysis = json.loads(first.get("analysis") or "{}") if first.get("analysis") else {}
    topic = (topic or analysis.get("hook") or first.get("caption") or Path(first["path"]).stem)[:80]
    caption = caption or first.get("caption") or ""
    hashtags = hashtags or first.get("hashtags") or ""
    brand = library.brand_profile(user_id)
    from src.publisher import CAROUSEL_PLATFORMS, parse_platforms
    platforms = [p for p in parse_platforms(brand.get("auto_platforms") or "") if p in CAROUSEL_PLATFORMS] or None
    pid = queries.create_draft(
        topic, caption, hashtags, media_type="carousel", user_id=user_id, source="library",
        platforms=platforms, upload_name=f"{len(assets)} photos",
        image_path=assets[0]["path"], page_id=pages.default_page_id(user_id))
    with get_connection() as conn:
        for pos, asset in enumerate(assets):
            conn.execute("INSERT INTO post_media (post_id, position, image_path, asset_id) VALUES (?, ?, ?, ?)",
                         (pid, pos, asset["path"], asset["id"]))
        conn.execute("UPDATE posts SET asset_ids = ? WHERE id = ?", (",".join(str(a["id"]) for a in assets), pid))
    library._mark_used([a["id"] for a in assets])
    log.info("carousel post #%s created from %d photos", pid, len(assets))
    return pid


def save_hosted(item_id: int, url: str, jpeg_path: Path, ref: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE post_media SET image_url = ?, jpeg_path = ?, host_ref = ?, hosted_at = ? WHERE id = ?",
                     (url, str(jpeg_path), ref, _now(), item_id))


def mark_post_hosted(post_id: int) -> bool:
    """When every slide is hosted, mirror the first one onto the post (hosted_at,
    image_url, jpeg_path). Returns True when the post is fully hosted."""
    slides = items(post_id)
    if not slides or not all(s["image_url"] for s in slides):
        return False
    first = slides[0]
    with get_connection() as conn:
        conn.execute("UPDATE posts SET image_url = ?, jpeg_path = ?, host_ref = ?, hosted_at = ?, last_error = NULL "
                     "WHERE id = ?", (first["image_url"], first["jpeg_path"], first["host_ref"], _now(), post_id))
    return True


def hosted_urls(post_id: int) -> list[str]:
    return [s["image_url"] for s in items(post_id) if s["image_url"]]


def local_files(post_id: int) -> list[Path]:
    out = []
    for s in items(post_id):
        for key in ("jpeg_path", "image_path"):
            if s.get(key) and Path(s[key]).exists():
                out.append(Path(s[key]))
                break
    return out
