"""
MoviePy rendering engine: speech MP3 + image (or stock clip) -> vertical MP4.

Usage (from the project root):
    python3 -m src.video.video_compiler              # all video posts with audio but no video
    python3 -m src.video.video_compiler --id 3
    python3 -m src.video.video_compiler --dry-run

What it does for each post:
    1. Loads the speech MP3 and measures its length.
    2. Takes the post's image (JPEG/PNG) or a random clip from data/stock/,
       scales and crops it to fill 1080x1920, and loops/stretches it to the
       exact length of the speech.
    3. Splits the spoken text into 2-3 word chunks and overlays them as big,
       centered, outlined captions timed to the audio. Timing is estimated
       from word lengths by default; set CAPTION_SYNC=whisper to use real
       word timestamps from OpenAI's transcription API (one extra API call).
    4. Mixes in a quiet background track from data/bg_music/ if one exists.
    5. Writes data/videos/post_<id>.mp4 at 1080x1920, 30 fps, H.264 + AAC.

Rendering is CPU heavy: expect roughly real time or slower (a 30 s video
takes about 30-90 s on a laptop).
"""

import argparse
import logging
import os
import random
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from pydub import AudioSegment

from config.db import DATA_DIR, get_connection, init_db
from src.security.secrets import ConfigError, get_secret
from src.video.audio_generator import spoken_text

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
log = logging.getLogger("video")

VIDEO_DIR = DATA_DIR / "videos"
BG_MUSIC_DIR = DATA_DIR / "bg_music"
STOCK_DIR = DATA_DIR / "stock"

WIDTH, HEIGHT, FPS = 1080, 1920, 30
CHUNK_WORDS = int(os.getenv("CAPTION_WORDS", "3"))
CAPTION_SYNC = os.getenv("CAPTION_SYNC", "estimate").strip().lower()   # estimate | whisper
MUSIC_GAIN_DB = float(os.getenv("MUSIC_GAIN_DB", "-18"))               # background music level
CAPTION_COLOR = os.getenv("CAPTION_COLOR", "#FFE600")                  # highlighted text
CAPTION_STROKE = os.getenv("CAPTION_STROKE", "black")
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "medium")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")

FONT_CANDIDATES = [
    os.getenv("VIDEO_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


class VideoError(Exception):
    """The video for one post could not be made."""


@dataclass
class CaptionChunk:
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.05, self.end - self.start)


def find_font() -> str:
    for candidate in FONT_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise VideoError("No bold TrueType font found. Set VIDEO_FONT=/path/to/font.ttf in .env.")


# ---------------------------------------------------------------------------
# Caption timeline
# ---------------------------------------------------------------------------

def chunk_words(words: list[str], size: int = CHUNK_WORDS) -> list[list[str]]:
    """Group words 'size' at a time, never leaving a lonely 1-word chunk at the end."""
    size = max(1, size)
    chunks = [words[i:i + size] for i in range(0, len(words), size)]
    if len(chunks) > 1 and len(chunks[-1]) == 1 and size > 1:
        chunks[-2].extend(chunks.pop())
    return chunks


def estimate_timeline(text: str, duration: float, chunk_size: int = CHUNK_WORDS) -> list[CaptionChunk]:
    """
    Spread chunks across the audio in proportion to their length in characters,
    with a little extra weight for sentence-ending punctuation (a natural pause).
    """
    words = text.split()
    if not words or duration <= 0:
        return []
    chunks = chunk_words(words, chunk_size)
    weights = []
    for chunk in chunks:
        w = sum(len(word) + 1 for word in chunk)
        if chunk[-1][-1] in ".!?":
            w += 3
        weights.append(w)
    total = sum(weights)
    out, t = [], 0.0
    for chunk, w in zip(chunks, weights):
        length = duration * w / total
        out.append(CaptionChunk(" ".join(chunk), round(t, 3), round(t + length, 3)))
        t += length
    out[-1].end = round(duration, 3)
    return out


def whisper_timeline(audio_path: Path, chunk_size: int = CHUNK_WORDS,
                     user_id: int | None = None) -> list[CaptionChunk]:
    """Real word timestamps from OpenAI's transcription API, grouped into chunks."""
    from openai import OpenAI
    client = OpenAI(api_key=get_secret("OPENAI_API_KEY", user_id))
    with open(audio_path, "rb") as fh:
        result = client.audio.transcriptions.create(
            model="whisper-1", file=fh, response_format="verbose_json",
            timestamp_granularities=["word"])
    words = [(w.word.strip(), float(w.start), float(w.end)) for w in (result.words or [])]
    if not words:
        raise VideoError("Transcription returned no word timestamps.")
    out = []
    for group in chunk_words(words, chunk_size):
        out.append(CaptionChunk(" ".join(w for w, _, _ in group), group[0][1], group[-1][2]))
    # Close the gaps so a caption stays on screen until the next one starts.
    for a, b in zip(out, out[1:]):
        a.end = b.start
    return out


def build_timeline(text: str, audio_path: Path, duration: float,
                   user_id: int | None = None) -> list[CaptionChunk]:
    if CAPTION_SYNC == "whisper":
        try:
            return whisper_timeline(audio_path, user_id=user_id)
        except (ConfigError, VideoError, Exception) as exc:  # noqa: BLE001
            log.warning("  whisper sync failed (%s); using estimated timing", exc)
    return estimate_timeline(text, duration)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def audio_duration(path: Path) -> float:
    try:
        return len(AudioSegment.from_file(path)) / 1000.0
    except Exception as exc:  # pydub raises CouldntDecodeError / FileNotFoundError
        raise VideoError(f"Cannot read audio {path}: {exc}") from exc


def pick_music() -> Path | None:
    if not BG_MUSIC_DIR.exists():
        return None
    tracks = sorted(p for p in BG_MUSIC_DIR.iterdir() if p.suffix.lower() in AUDIO_EXTS)
    return random.choice(tracks) if tracks else None


def mix_audio(speech_path: Path, out_path: Path, music_path: Path | None = None) -> Path:
    """Speech on top of looped, quiet, faded background music. Returns out_path."""
    speech = AudioSegment.from_file(speech_path)
    if music_path is None:
        speech.export(out_path, format="mp3")
        return out_path
    music = AudioSegment.from_file(music_path)
    if len(music) == 0:
        raise VideoError(f"Background track {music_path.name} is empty.")
    loops = len(speech) // len(music) + 1
    bed = (music * loops)[: len(speech)].apply_gain(MUSIC_GAIN_DB).fade_in(500).fade_out(1500)
    bed.overlay(speech).export(out_path, format="mp3")
    return out_path


# ---------------------------------------------------------------------------
# Visuals
# ---------------------------------------------------------------------------

def cover_fit(clip, width: int, height: int):
    """Scale so the clip fills width x height, then crop the overflow (like CSS object-fit: cover)."""
    scale = max(width / clip.w, height / clip.h)
    clip = clip.resized(scale)
    return clip.cropped(x_center=clip.w / 2, y_center=clip.h / 2, width=width, height=height)


def build_visual(visual_path: Path, duration: float, size=(WIDTH, HEIGHT)):
    from moviepy import ImageClip, VideoFileClip, vfx
    if not visual_path.exists():
        raise VideoError(f"Visual not found: {visual_path}")
    suffix = visual_path.suffix.lower()
    if suffix in IMAGE_EXTS:
        clip = ImageClip(str(visual_path)).with_duration(duration)
    elif suffix in VIDEO_EXTS:
        clip = VideoFileClip(str(visual_path)).without_audio()
        if clip.duration < duration:
            clip = clip.with_effects([vfx.Loop(duration=duration)])
        else:
            clip = clip.subclipped(0, duration)
    else:
        raise VideoError(f"Unsupported visual type: {visual_path.suffix}")
    return cover_fit(clip, *size).with_duration(duration)


def build_caption_clips(chunks: list[CaptionChunk], size=(WIDTH, HEIGHT), font: str | None = None):
    from moviepy import TextClip
    width, height = size
    font = font or find_font()
    font_size = int(width * 0.08)
    clips = []
    for chunk in chunks:
        tc = TextClip(font=font, text=chunk.text.upper(), font_size=font_size, color=CAPTION_COLOR,
                      stroke_color=CAPTION_STROKE, stroke_width=max(2, font_size // 14),
                      method="caption", size=(int(width * 0.86), None), text_align="center")
        clips.append(tc.with_start(chunk.start).with_duration(chunk.duration)
                       .with_position(("center", int(height * 0.62))))
    return clips


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(visual_path: Path, audio_path: Path, out_path: Path, text: str,
           music_path: Path | None = None, chunks: list[CaptionChunk] | None = None,
           size=(WIDTH, HEIGHT), fps: int = FPS) -> Path:
    """Compose and write the final video. Returns out_path."""
    from moviepy import AudioFileClip, CompositeVideoClip
    duration = audio_duration(audio_path)
    if duration < 0.5:
        raise VideoError(f"Speech audio is too short ({duration:.2f}s).")
    chunks = chunks if chunks is not None else estimate_timeline(text, duration)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        mixed = mix_audio(audio_path, Path(tmp) / "mix.mp3", music_path)
        visual = build_visual(visual_path, duration, size)
        captions = build_caption_clips(chunks, size)
        video = (CompositeVideoClip([visual, *captions], size=size)
                 .with_audio(AudioFileClip(str(mixed)))
                 .with_duration(duration))
        try:
            video.write_videofile(str(out_path), fps=fps, codec="libx264", audio_codec="aac",
                                  preset=VIDEO_PRESET, threads=os.cpu_count() or 2, logger=None)
        finally:
            video.close()
            visual.close()
    return out_path


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------

def fetch_posts_needing_video(post_id: int | None = None, user_id: int | None = None) -> list:
    with get_connection() as conn:
        if post_id is not None:
            return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchall()
        scope, params = (" AND user_id = ?", (user_id,)) if user_id else ("", ())
        return conn.execute(
            f"""
            SELECT * FROM posts
            WHERE media_type = 'video'
              AND COALESCE(source, 'ai') = 'ai'
              AND audio_path IS NOT NULL AND audio_path != ''
              AND (video_path IS NULL OR video_path = '')
              AND status NOT IN ('published', 'rejected'){scope}
            ORDER BY id
            """, params
        ).fetchall()


def save_video(post_id: int, path: Path) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET video_path = ?, last_error = NULL WHERE id = ?",
                     (str(path), post_id))


def save_error(post_id: int, message: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = ? WHERE id = ?", (message, post_id))


def pick_visual(post) -> Path | None:
    """The post's own image if it has one, else a random stock clip, else None."""
    for key in ("jpeg_path", "image_path"):
        if post[key] and Path(post[key]).exists():
            return Path(post[key])
    if STOCK_DIR.exists():
        clips = sorted(p for p in STOCK_DIR.iterdir() if p.suffix.lower() in VIDEO_EXTS + IMAGE_EXTS)
        if clips:
            return random.choice(clips)
    return None


def compile_for_post(post, dry_run: bool = False) -> bool:
    pid = post["id"]
    log.info("Post #%s (%s)", pid, post["topic"])
    if not post["audio_path"] or not Path(post["audio_path"]).exists():
        log.error("  skipped: no speech audio yet. Run the audio generator first.")
        save_error(pid, "video: no audio_path (run the audio generator)")
        return False
    visual = pick_visual(post)
    if visual is None:
        log.error("  skipped: no image and no stock clip in %s", STOCK_DIR)
        save_error(pid, "video: no image and no stock clip to use as background")
        return False
    music = pick_music()
    log.info("  visual %s, music %s", visual.name, music.name if music else "none")
    if dry_run:
        log.info("  would render %dx%d @ %d fps (dry run)", WIDTH, HEIGHT, FPS)
        return False
    try:
        text = spoken_text(post)
        audio_path = Path(post["audio_path"])
        chunks = build_timeline(text, audio_path, audio_duration(audio_path), post["user_id"])
        out = render(visual, audio_path, VIDEO_DIR / f"post_{pid}.mp4", text, music, chunks)
    except VideoError as exc:
        log.error("  FAILED: %s", exc)
        save_error(pid, f"video: {exc}")
        return False
    except Exception as exc:
        log.exception("  render crashed")
        save_error(pid, f"video: render crashed: {exc}")
        return False
    save_video(pid, out)
    log.info("  saved %s (%d KB, %d captions)", out.name, out.stat().st_size // 1024, len(chunks))
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render vertical videos for video posts.")
    parser.add_argument("--id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user", type=int, default=None,
                        help="Only this user's posts (default: every user's)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    init_db()
    posts = fetch_posts_needing_video(args.id, args.user)
    if not posts:
        log.info("No video posts are waiting to be rendered.")
        return 0
    log.info("%d post(s) to render%s", len(posts), " [DRY RUN]" if args.dry_run else "")
    done = sum(compile_for_post(p, dry_run=args.dry_run) for p in posts)
    log.info("Done. %d of %d video(s) rendered.", done, len(posts))
    return 0 if done == len(posts) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
