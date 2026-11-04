"""
Media library: the user's own photos and clips become posts automatically.

Folder:  data/library/user_<id>/   (drop files there, or upload on the Library page)

    python3 -m src.library scan            # register new files (all users)
    python3 -m src.library analyze         # describe them with the vision model
    python3 -m src.library plan --user 3 --count 2    # create 2 draft posts for user 3
    python3 -m src.library plan            # every user with posts_per_day > 0, once a day

What "plan" makes, from the least-used material:
    * a photo            -> an image post with an AI caption and hashtags
    * a short clip       -> a video post using the clip as it is (<= MAX_CLIP_SECONDS)
    * several clips/photos -> a vertical montage: a few seconds of each, 1080x1920,
                              background music from data/bg_music/ if present

Captions are written from what the vision model sees in the photo (or in three
frames of the clip), guided by the user's brand brief (Profile page).
Everything lands in the approval queue as a draft. Nothing is posted by itself.
"""

import argparse
import hashlib
import json
import logging
import random
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from config.db import DATA_DIR, get_connection, init_db
from src.ai import providers
from src.security.secrets import ConfigError

log = logging.getLogger("library")

LIBRARY_DIR = DATA_DIR / "library"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
MAX_CLIP_SECONDS = 90          # longer clips only go into montages
MONTAGE_CLIPS = 4              # clips per montage
MONTAGE_SECONDS_PER_CLIP = 4.0
MONTAGE_MIN_CLIPS = 3
MONTAGE_SIZE = (1080, 1920)
MONTAGE_FPS = 30
FRAMES_PER_VIDEO = 3


class LibraryError(Exception):
    pass


def user_dir(user_id: int) -> Path:
    path = LIBRARY_DIR / f"user_{user_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _fingerprint(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        h.update(fh.read(1024 * 1024))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()


def probe_video(path: Path) -> dict:
    """duration/width/height via ffprobe, or {} when ffprobe is missing or fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        info = json.loads(out.stdout or "{}")
        stream = (info.get("streams") or [{}])[0]
        return {"width": stream.get("width"), "height": stream.get("height"),
                "duration": float((info.get("format") or {}).get("duration") or 0) or None}
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return {}


def probe_image(path: Path) -> dict:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return {"width": img.width, "height": img.height}
    except Exception:  # noqa: BLE001
        return {}


def scan(user_id: int) -> dict:
    """Register new files in the user's library folder. Returns counts."""
    folder = user_dir(user_id)
    with get_connection() as conn:
        known = {r["path"]: r for r in conn.execute(
            "SELECT id, path, fingerprint FROM assets WHERE user_id = ?", (user_id,))}
    added, skipped, removed = 0, 0, 0
    seen = set()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            kind = "image"
        elif ext in VIDEO_EXTS:
            kind = "video"
        else:
            continue
        key = str(path)
        seen.add(key)
        fp = _fingerprint(path)
        if key in known and known[key]["fingerprint"] == fp:
            skipped += 1
            continue
        meta = probe_image(path) if kind == "image" else probe_video(path)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO assets (user_id, path, kind, fingerprint, size_bytes, width, height, duration) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, path) DO UPDATE SET fingerprint = excluded.fingerprint, "
                "size_bytes = excluded.size_bytes, width = excluded.width, height = excluded.height, "
                "duration = excluded.duration, analyzed_at = NULL, analysis = NULL, caption = NULL, "
                "hashtags = NULL, description = NULL, analysis_error = NULL",
                (user_id, key, kind, fp, path.stat().st_size, meta.get("width"), meta.get("height"),
                 meta.get("duration")))
        added += 1
    # files that disappeared from the folder
    gone = [known[k]["id"] for k in known if k not in seen]
    if gone:
        with get_connection() as conn:
            for aid in gone:
                conn.execute("DELETE FROM assets WHERE id = ?", (aid,))
        removed = len(gone)
    log.info("Library user %s: %d new/changed, %d unchanged, %d removed", user_id, added, skipped, removed)
    return {"added": added, "skipped": skipped, "removed": removed}


# ---------------------------------------------------------------------------
# Analysis (vision)
# ---------------------------------------------------------------------------

def brand_profile(user_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT display_name, brand_name, brand_brief, brand_language, posts_per_day, auto_platforms, "
            "montage_enabled, last_plan_date, content_pillars, auto_subtitles, ai_posts_per_day, ai_auto_approve, "
            "ai_title_overlay, ai_picture_source, brand_kind FROM users WHERE id = ?",
            (user_id,)).fetchone()
    return dict(row) if row else {}


def pillar_hint(brand: dict) -> str:
    """Rotate through the user's content pillars so posts are not all the same kind."""
    raw = (brand.get("content_pillars") or "").strip()
    if not raw:
        return ""
    pillars = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    if not pillars:
        return ""
    pick = pillars[datetime.now().timetuple().tm_yday % len(pillars)]
    what = "this page" if brand.get("brand_kind") == "page" else "this business"
    return (f"Content pillars for {what}: {', '.join(pillars)}. "
            f"If the picture fits, angle today's post towards: {pick}.\n")


def analysis_prompt(brand: dict, kind: str, n_images: int) -> str:
    name = brand.get("brand_name") or brand.get("display_name") or "the business"
    brief = (brand.get("brand_brief") or "").strip() or "A local business. Friendly, genuine, not salesy."
    language = brand.get("brand_language") or "the same language as the brief, otherwise English"
    what = "a photo" if kind == "image" else f"{n_images} frames from a short video clip"
    pillars = pillar_hint(brand)
    return f"""You write social media posts for {name}.
About the business and how it should sound:
{brief}
{pillars}
You are looking at {what} from the business's own media library.
Write ONE post for it. Answer with a single JSON object and nothing else:
{{
  "description": "what is actually in the picture(s), one factual sentence (used internally)",
  "caption": "the post text in {language}: 1-3 short sentences, natural, specific to what is visible, no hashtags, at most 2 emojis",
  "hook": "a 3-6 word on-screen title for a short video, in the same language, no hashtags",
  "hashtags": ["8 to 12 relevant hashtags, lowercase, with #, mix of specific and local"],
  "mood": "one word",
  "quality": 1-5 (5 = excellent, 1 = unusable: blurry, dark, nothing to see)
}}"""


def extract_frames(video: Path, n: int = FRAMES_PER_VIDEO, duration: float | None = None) -> list[Path]:
    """Grab n evenly spaced JPEG frames with ffmpeg into a temp folder."""
    duration = duration or (probe_video(video).get("duration") or 0) or 3.0
    tmp = Path(tempfile.mkdtemp(prefix="frames_"))
    frames = []
    for i in range(n):
        t = max(0.0, duration * (i + 0.5) / n)
        out = tmp / f"frame_{i}.jpg"
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=768:-2", str(out)],
                           capture_output=True, timeout=60, check=True)
            if out.exists() and out.stat().st_size > 0:
                frames.append(out)
        except (OSError, subprocess.SubprocessError):
            continue
    return frames


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise LibraryError("The model did not answer with JSON.")
    return json.loads(text[start:end + 1])


def analyze(asset: dict, brand: dict | None = None) -> dict:
    """Describe one asset with the vision model and store caption/hashtags. Returns the analysis."""
    brand = brand or brand_profile(asset["user_id"])
    path = Path(asset["path"])
    if not path.exists():
        raise LibraryError(f"File is gone: {path}")
    images = [path] if asset["kind"] == "image" else extract_frames(path, duration=asset.get("duration"))
    if not images:
        raise LibraryError("Could not read any frame from the video (is ffmpeg installed?).")
    raw = providers.vision(analysis_prompt(brand, asset["kind"], len(images)), images,
                           asset["user_id"], json_mode=True)
    data = _parse_json(raw)
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    tags = [t if t.startswith("#") else f"#{t}" for t in (str(x).strip() for x in tags) if t]
    data["hashtags"] = tags
    with get_connection() as conn:
        conn.execute(
            "UPDATE assets SET description = ?, caption = ?, hashtags = ?, analysis = ?, analyzed_at = ?, "
            "analysis_error = NULL WHERE id = ?",
            (str(data.get("description", ""))[:1000], str(data.get("caption", "")).strip(),
             " ".join(tags), json.dumps(data, ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), asset["id"]))
    return data


def analyze_pending(user_id: int | None = None, limit: int = 50) -> dict:
    scope, params = (" AND user_id = ?", (user_id,)) if user_id else ("", ())
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM assets WHERE analyzed_at IS NULL AND excluded = 0{scope} ORDER BY id LIMIT ?",
            (*params, limit))]
    done, failed = 0, 0
    brands = {}
    for asset in rows:
        uid = asset["user_id"]
        brands.setdefault(uid, brand_profile(uid))
        try:
            analyze(asset, brands[uid])
            done += 1
            log.info("  analyzed %s", Path(asset["path"]).name)
        except (providers.AIError, ConfigError, LibraryError, ValueError) as exc:
            failed += 1
            log.error("  %s: %s", Path(asset["path"]).name, exc)
            with get_connection() as conn:
                conn.execute("UPDATE assets SET analysis_error = ? WHERE id = ?", (str(exc)[:500], asset["id"]))
            if isinstance(exc, (providers.QuotaError, ConfigError)):
                log.warning("  stopping analysis for user %s: %s", uid, exc)
                break
    return {"analyzed": done, "failed": failed, "pending": len(rows) - done - failed}


# ---------------------------------------------------------------------------
# Planning: assets -> draft posts
# ---------------------------------------------------------------------------

MIN_QUALITY = 2


def _quality(asset: dict) -> int:
    try:
        return int(json.loads(asset.get("analysis") or "{}").get("quality", 3))
    except (ValueError, TypeError):
        return 3


def _pick_assets(user_id: int, kind: str | None, n: int, max_duration: float | None = None) -> list[dict]:
    """Least-used usable assets first; ties broken at random. Works on SQLite and PostgreSQL."""
    sql = "SELECT * FROM assets WHERE user_id = ? AND excluded = 0 AND analyzed_at IS NOT NULL"
    params: list = [user_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if max_duration is not None:
        sql += " AND (duration IS NULL OR duration <= ?)"
        params.append(max_duration)
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    rows = [r for r in rows if _quality(r) >= MIN_QUALITY]
    random.shuffle(rows)
    rows.sort(key=lambda r: r.get("times_used") or 0)
    return rows[:n]


def _mark_used(asset_ids: list[int]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        for aid in asset_ids:
            conn.execute("UPDATE assets SET times_used = times_used + 1, last_used_at = ? WHERE id = ?", (now, aid))


def _create_post(user_id: int, brand: dict, asset: dict, media_type: str, **extra) -> int:
    from src.web import queries
    topic = (json.loads(asset["analysis"] or "{}").get("hook") or asset["caption"] or Path(asset["path"]).stem)[:80]
    pid = queries.create_draft(
        topic, asset["caption"] or "", asset["hashtags"] or "", media_type=media_type, user_id=user_id,
        source="library", platforms=(brand.get("auto_platforms") or None), upload_name=Path(asset["path"]).name,
        image_path=asset["path"] if media_type == "image" else None,
        video_path=extra.get("video_path", asset["path"] if media_type == "video" else None),
        page_id=_default_page(user_id))
    with get_connection() as conn:
        conn.execute("UPDATE posts SET asset_ids = ? WHERE id = ?",
                     (",".join(str(a) for a in extra.get("asset_ids", [asset["id"]])), pid))
    return pid


def _default_page(user_id: int):
    from src import pages
    return pages.default_page_id(user_id)


def compile_montage(clips: list[Path], out_path: Path, seconds_per_clip: float = MONTAGE_SECONDS_PER_CLIP,
                    music: Path | None = None, title: str | None = None, size=(1080, 1920), fps: int = 30) -> Path:
    """A vertical montage: the middle few seconds of each clip, one after another."""
    from moviepy import AudioFileClip, CompositeVideoClip, ImageClip, VideoFileClip, afx, concatenate_videoclips
    from src.video.video_compiler import build_caption_clips, cover_fit, CaptionChunk

    parts, handles = [], []
    for path in clips:
        if path.suffix.lower() in IMAGE_EXTS:
            clip = ImageClip(str(path)).with_duration(seconds_per_clip)
        else:
            src = VideoFileClip(str(path))
            handles.append(src)
            if src.duration <= seconds_per_clip:
                clip = src
            else:
                start = max(0.0, (src.duration - seconds_per_clip) / 2)
                clip = src.subclipped(start, start + seconds_per_clip)
            if music is not None:
                clip = clip.without_audio()
        parts.append(cover_fit(clip, *size))
    if not parts:
        raise LibraryError("No usable clips for a montage.")
    video = concatenate_videoclips(parts, method="compose")
    total = video.duration
    if music is not None:
        bed = AudioFileClip(str(music)).with_effects([afx.AudioLoop(duration=total), afx.MultiplyVolume(0.35),
                                                     afx.AudioFadeOut(1.5)])
        video = video.with_audio(bed)
    if title:
        captions = build_caption_clips([CaptionChunk(title, 0.3, min(total, 3.5))], size)
        video = CompositeVideoClip([video, *captions], size=size).with_duration(total)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        video.write_videofile(str(out_path), fps=fps, codec="libx264", audio_codec="aac", preset="fast",
                              threads=4, logger=None)
    finally:
        video.close()
        for h in handles:
            h.close()
    return out_path


def make_montage_post(user_id: int, brand: dict) -> int | None:
    """Pick a few clips/photos and build one montage post. None if not enough material."""
    from src.video.video_compiler import VIDEO_DIR, pick_music
    clips = _pick_assets(user_id, "video", MONTAGE_CLIPS)
    if len(clips) < MONTAGE_MIN_CLIPS:
        clips += _pick_assets(user_id, "image", MONTAGE_CLIPS - len(clips))
    if len(clips) < MONTAGE_MIN_CLIPS:
        return None
    lead = clips[0]
    analysis = json.loads(lead["analysis"] or "{}")
    # Caption: ask the text model to merge the clips' descriptions into one post.
    descriptions = "\n".join(f"- {json.loads(c['analysis'] or '{}').get('description', '')}" for c in clips)
    prompt = (analysis_prompt(brand, "video", len(clips))
              .replace("You are looking at", "These are descriptions of the clips in a short montage video:\n"
                       + descriptions + "\nYou are NOT looking at pictures; use the descriptions above. Ignore")
              )
    caption, hashtags, hook = lead["caption"], lead["hashtags"], analysis.get("hook")
    try:
        data = _parse_json(providers.text(prompt, user_id, json_mode=True))
        caption = str(data.get("caption") or caption)
        tags = data.get("hashtags") or []
        hashtags = " ".join(t if str(t).startswith("#") else f"#{t}" for t in tags) or hashtags
        hook = data.get("hook") or hook
    except (providers.AIError, ConfigError, LibraryError, ValueError) as exc:
        log.warning("  montage caption fell back to the first clip's: %s", exc)
    out = VIDEO_DIR / f"montage_u{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.mp4"
    compile_montage([Path(c["path"]) for c in clips], out, music=pick_music(), title=hook,
                    size=MONTAGE_SIZE, fps=MONTAGE_FPS)
    merged = dict(lead, caption=caption, hashtags=hashtags,
                  analysis=json.dumps(dict(analysis, hook=hook), ensure_ascii=False))
    pid = _create_post(user_id, brand, merged, "video", video_path=str(out), asset_ids=[c["id"] for c in clips])
    _mark_used([c["id"] for c in clips])
    log.info("  montage post #%s from %d clips -> %s", pid, len(clips), out.name)
    return pid


def plan(user_id: int, count: int, montage: bool | None = None) -> list[int]:
    """Create up to `count` draft posts from the library. Returns the new post ids."""
    scan(user_id)
    analyze_pending(user_id)
    brand = brand_profile(user_id)
    if montage is None:
        montage = bool(brand.get("montage_enabled", 1))
    made: list[int] = []
    kinds = ["image", "video", "montage"] if montage else ["image", "video"]
    attempts = 0
    while len(made) < count and attempts < count * 3:
        kind = kinds[attempts % len(kinds)]
        attempts += 1
        try:
            if kind == "montage":
                pid = make_montage_post(user_id, brand)
                if pid:
                    made.append(pid)
                continue
            picked = _pick_assets(user_id, kind, 1, MAX_CLIP_SECONDS if kind == "video" else None)
            if not picked:
                continue
            asset = picked[0]
            pid = _create_post(user_id, brand, asset, kind)
            _mark_used([asset["id"]])
            made.append(pid)
            log.info("  %s post #%s from %s", kind, pid, Path(asset["path"]).name)
        except Exception as exc:  # noqa: BLE001  one bad clip must not stop the plan
            log.exception("  could not make a %s post: %s", kind, exc)
    with get_connection() as conn:
        conn.execute("UPDATE users SET last_plan_date = ? WHERE id = ?",
                     (datetime.now().strftime("%Y-%m-%d"), user_id))
    return made


def plan_due_users() -> list[dict]:
    """Users with automatic planning on who have not had today's plan yet."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, posts_per_day FROM users WHERE is_active = 1 AND COALESCE(posts_per_day, 0) > 0 "
            "AND (last_plan_date IS NULL OR last_plan_date < ?)", (today,))]


def run_daily(user_id: int | None = None) -> int:
    """Pipeline step: plan today's posts for every due user (or one user). Returns exit code."""
    users = plan_due_users()
    if user_id is not None:
        users = [u for u in users if u["id"] == user_id]
    if not users:
        log.info("No library plans due today.")
        return 0
    failures = 0
    for u in users:
        try:
            made = plan(u["id"], int(u["posts_per_day"]))
            log.info("User %s: %d library post(s) created", u["id"], len(made))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.exception("User %s: planning failed: %s", u["id"], exc)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Queries for the Library page
# ---------------------------------------------------------------------------

def list_assets(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM assets WHERE user_id = ? ORDER BY analyzed_at IS NOT NULL, times_used, id DESC",
            (user_id,))]
    for r in rows:
        r["name"] = Path(r["path"]).name
    return rows


def get_asset(asset_id: int, user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ? AND user_id = ?", (asset_id, user_id)).fetchone()
    return dict(row) if row else None


def set_excluded(asset_id: int, user_id: int, excluded: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE assets SET excluded = ? WHERE id = ? AND user_id = ?",
                     (1 if excluded else 0, asset_id, user_id))


def delete_asset(asset_id: int, user_id: int, remove_file: bool = False) -> None:
    asset = get_asset(asset_id, user_id)
    if not asset:
        return
    if remove_file:
        Path(asset["path"]).unlink(missing_ok=True)
    with get_connection() as conn:
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))


def save_brand(user_id: int, **fields) -> None:
    allowed = {"brand_name", "brand_brief", "brand_language", "posts_per_day", "auto_platforms", "montage_enabled",
               "content_pillars", "auto_subtitles", "ai_posts_per_day", "ai_auto_approve", "ai_title_overlay",
               "ai_picture_source", "brand_kind"}
    if "brand_kind" in fields:
        fields["brand_kind"] = "page" if fields["brand_kind"] == "page" else "business"
    if "ai_picture_source" in fields:
        fields["ai_picture_source"] = "ai" if fields["ai_picture_source"] == "ai" else "photo"
    for flag in ("auto_subtitles", "ai_auto_approve", "ai_title_overlay"):
        if flag in fields:
            fields[flag] = 1 if fields[flag] else 0
    if "ai_posts_per_day" in fields:
        fields["ai_posts_per_day"] = max(0, min(10, int(fields["ai_posts_per_day"] or 0)))
    clean = {k: v for k, v in fields.items() if k in allowed}
    if "posts_per_day" in clean:
        clean["posts_per_day"] = max(0, min(20, int(clean["posts_per_day"] or 0)))
    if "montage_enabled" in clean:
        clean["montage_enabled"] = 1 if clean["montage_enabled"] else 0
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*clean.values(), user_id))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Media library: scan, analyze, plan posts.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "analyze", "plan"):
        p = sub.add_parser(name)
        p.add_argument("--user", type=int, default=None)
        if name == "plan":
            p.add_argument("--count", type=int, default=None, help="how many posts (default: the user's posts_per_day)")
            p.add_argument("--force", action="store_true", help="plan even if today's plan was already made")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    init_db()
    if args.cmd == "scan":
        from src.web.auth import list_user_ids
        for uid in ([args.user] if args.user else list_user_ids()):
            scan(uid)
        return 0
    if args.cmd == "analyze":
        r = analyze_pending(args.user)
        log.info("Analysis: %s", r)
        return 0 if not r["failed"] else 1
    if args.cmd == "plan":
        if args.user and (args.count or args.force):
            made = plan(args.user, args.count or int(brand_profile(args.user).get("posts_per_day") or 1))
            log.info("Created %d post(s): %s", len(made), made)
            return 0
        return run_daily(args.user)
    return 2


if __name__ == "__main__":
    sys.exit(main())
