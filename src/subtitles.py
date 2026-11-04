"""
Auto-captions for your own clips: transcribe the speech and burn word-group
captions into the video (the TikTok / Reels style everyone expects now).

Pipeline step "subtitles": for uploaded or library video posts of users with
"auto_subtitles" on, and no subtitled file yet:
    1. extract the audio with ffmpeg
    2. transcribe with the user's provider: OpenAI Whisper (word timestamps,
       paid) or Gemini (free tier; timestamps are estimated by the model)
    3. render captions with the same engine as the AI reels
    4. the subtitled file replaces the one that gets published (video_path),
       the original is kept in subtitled_path's sibling .orig

    python3 -m src.subtitles --user 1 --id 12
"""

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from config.db import get_connection, init_db
from src.ai import providers
from src.security.secrets import ConfigError, get_secret

log = logging.getLogger("subtitles")


class SubtitleError(Exception):
    pass


def extract_audio(video: Path, out: Path) -> Path:
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-b:a", "64k", str(out)], check=True, capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SubtitleError(f"Could not extract audio (is ffmpeg installed?): {exc}") from exc
    if not out.exists() or out.stat().st_size < 1000:
        raise SubtitleError("The clip seems to have no audio track.")
    return out


def transcribe_openai(audio: Path, user_id) -> list[dict]:
    """[{word, start, end}] from Whisper with word timestamps."""
    import openai
    client = openai.OpenAI(api_key=get_secret("OPENAI_API_KEY", user_id))
    try:
        with open(audio, "rb") as fh:
            result = client.audio.transcriptions.create(model="whisper-1", file=fh, response_format="verbose_json",
                                                        timestamp_granularities=["word"])
    except openai.OpenAIError as exc:
        raise SubtitleError(f"OpenAI transcription failed: {getattr(exc, 'message', exc)}") from exc
    words = [{"word": w.word.strip(), "start": float(w.start), "end": float(w.end)} for w in (result.words or [])]
    if not words:
        raise SubtitleError("No speech was recognised in the clip.")
    return words


def transcribe_gemini(audio: Path, user_id, duration: float) -> list[dict]:
    """Gemini listens to the audio and returns timed phrases (its timestamps are estimates)."""
    import base64
    prompt = (f"Transcribe this {duration:.0f}-second audio exactly as spoken, in its original language. "
              "Return ONLY a JSON object: {\"segments\": [{\"text\": \"2-4 words\", \"start\": seconds, \"end\": seconds}, ...]}. "
              "Segments must be in order, not overlap, and cover the speech from start to end.")
    parts = [{"text": prompt}, {"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(audio.read_bytes()).decode()}}]
    raw = providers._gemini_parts_text(providers._gemini_call(providers.GEMINI_VISION_MODEL, parts, user_id,
                                                              {"responseMimeType": "application/json"}))
    start, end = raw.find("{"), raw.rfind("}")
    try:
        segments = json.loads(raw[start:end + 1])["segments"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SubtitleError("Gemini did not return a usable transcript.") from exc
    words = []
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if text and e > s:
            words.append({"word": text, "start": max(0.0, s), "end": min(duration, e)})
    if not words:
        raise SubtitleError("No speech was recognised in the clip.")
    return words


def transcribe(audio: Path, user_id, duration: float) -> list[dict]:
    provider = providers.pick("vision", user_id)
    try:
        if provider == "openai":
            return transcribe_openai(audio, user_id)
        return transcribe_gemini(audio, user_id, duration)
    except (providers.AIError, ConfigError) as exc:
        raise SubtitleError(str(exc)) from exc


def chunks_from_words(words: list[dict], size: int = 3):
    """Group timed words into caption chunks for the renderer."""
    from src.video.video_compiler import CaptionChunk
    out = []
    for i in range(0, len(words), size):
        group = words[i:i + size]
        out.append(CaptionChunk(" ".join(w["word"] for w in group), group[0]["start"], group[-1]["end"]))
    for a, b in zip(out, out[1:]):
        a.end = max(a.start + 0.2, min(a.end + 0.15, b.start))
    return out


def burn(video: Path, chunks, out: Path, size=(1080, 1920), fps: int = 30) -> Path:
    """Overlay the caption chunks on the clip, keeping its own audio."""
    from moviepy import CompositeVideoClip, VideoFileClip
    from src.video.video_compiler import build_caption_clips, cover_fit
    src = VideoFileClip(str(video))
    try:
        base = cover_fit(src, *size)
        captions = build_caption_clips(chunks, size)
        final = CompositeVideoClip([base, *captions], size=size).with_duration(src.duration)
        out.parent.mkdir(parents=True, exist_ok=True)
        final.write_videofile(str(out), fps=fps, codec="libx264", audio_codec="aac", preset="fast", threads=4,
                              logger=None)
        final.close()
    finally:
        src.close()
    return out


def subtitle_post(post: dict, size=(1080, 1920), fps: int = 30) -> Path:
    video = Path(post["video_path"])
    if not video.exists():
        raise SubtitleError(f"Video file not found: {video}")
    from src.library import probe_video
    duration = probe_video(video).get("duration") or 0
    with tempfile.TemporaryDirectory() as tmp:
        audio = extract_audio(video, Path(tmp) / "audio.mp3")
        words = transcribe(audio, post["user_id"], duration or 60)
    chunks = chunks_from_words(words)
    out = video.with_name(video.stem + "_captioned.mp4")
    burn(video, chunks, out, size, fps)
    transcript = " ".join(w["word"] for w in words)
    with get_connection() as conn:
        conn.execute("UPDATE posts SET subtitled_path = ?, video_path = ?, transcript = ?, hosted_at = NULL, "
                     "video_url = NULL, last_error = NULL WHERE id = ?", (str(out), str(out), transcript[:5000], post["id"]))
    return out


def fetch_pending(post_id: int | None = None, user_id: int | None = None) -> list[dict]:
    with get_connection() as conn:
        if post_id is not None:
            return [dict(r) for r in conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,))]
        scope, params = (" AND p.user_id = ?", (user_id,)) if user_id else ("", ())
        return [dict(r) for r in conn.execute(
            f"""
            SELECT p.* FROM posts p JOIN users u ON u.id = p.user_id
            WHERE p.media_type = 'video' AND p.video_path IS NOT NULL AND p.video_path != ''
              AND (p.subtitled_path IS NULL OR p.subtitled_path = '')
              AND p.source IN ('upload', 'library') AND p.status NOT IN ('published', 'rejected')
              AND COALESCE(u.auto_subtitles, 0) = 1{scope}
            ORDER BY p.id
            """, params)]


def run_step(user_id: int | None = None, post_id: int | None = None) -> int:
    posts = fetch_pending(post_id, user_id)
    if not posts:
        log.info("No clips waiting for captions.")
        return 0
    failed = 0
    for post in posts:
        log.info("Post #%s (%s): adding captions", post["id"], post["topic"])
        try:
            out = subtitle_post(post)
            log.info("  captioned -> %s", out.name)
        except SubtitleError as exc:
            failed += 1
            log.error("  FAILED: %s", exc)
            with get_connection() as conn:
                conn.execute("UPDATE posts SET last_error = ?, subtitled_path = 'skipped' WHERE id = ?",
                             (f"subtitles: {exc}", post["id"]))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.exception("  crashed: %s", exc)
            with get_connection() as conn:
                conn.execute("UPDATE posts SET last_error = ?, subtitled_path = 'skipped' WHERE id = ?",
                             (f"subtitles: crashed: {exc}", post["id"]))
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Burn auto-captions into video posts.")
    parser.add_argument("--user", type=int, default=None)
    parser.add_argument("--id", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    init_db()
    return run_step(args.user, args.id)


if __name__ == "__main__":
    sys.exit(main())
