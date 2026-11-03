"""
Audio engine: turns a post's caption into a speech MP3.

Usage (from the project root):
    python3 -m src.video.audio_generator             # all video posts missing audio
    python3 -m src.video.audio_generator --id 3
    python3 -m src.video.audio_generator --dry-run

Only posts with media_type = 'video' get audio. The spoken text is the caption
with hashtags, links and emojis removed. Output: data/audio/post_<id>.mp3.

Providers (TTS_PROVIDER in .env): "openai" (default) or "elevenlabs".
TTS_FALLBACK names a second provider to try when the first one is out of
quota or not configured. A refused or malformed request is not retried on
the fallback: that would hide a real problem.
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import openai
import requests
from dotenv import load_dotenv
from openai import OpenAI

from config.db import DATA_DIR, get_connection, init_db
from src.security.secrets import ConfigError, get_secret

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
log = logging.getLogger("audio")

AUDIO_DIR = DATA_DIR / "audio"
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "openai").strip().lower()
TTS_FALLBACK = os.getenv("TTS_FALLBACK", "").strip().lower()
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "nova")
OPENAI_TTS_INSTRUCTIONS = os.getenv("OPENAI_TTS_INSTRUCTIONS",
                                    "Warm, upbeat, natural pace, like a friendly local business owner.")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MAX_TTS_CHARS = 4000
REQUEST_TIMEOUT = 120


class AudioGenError(Exception):
    """The audio for one post could not be made."""


class QuotaError(AudioGenError):
    """Provider is out of credit or rate limited. A fallback provider may be tried."""


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

_HASHTAG = re.compile(r"(?<!\w)#\w+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
                    "\U0001FA70-\U0001FAFF\u2B50\u2B55\u203C\u2049\u2122\u2139\uFE0F]+")


def spoken_text(post) -> str:
    """Caption as it should be read aloud: no hashtags, links or emojis."""
    text = post["caption"] or ""
    text = _URL.sub("", text)
    text = _HASHTAG.sub("", text)
    text = _EMOJI.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:MAX_TTS_CHARS]


# ---------------------------------------------------------------------------
# Providers. Each returns MP3 bytes or raises.
# ---------------------------------------------------------------------------

def synthesize_openai(text: str) -> bytes:
    client = OpenAI(api_key=get_secret("OPENAI_API_KEY"))
    kwargs = {"model": OPENAI_TTS_MODEL, "voice": OPENAI_TTS_VOICE, "input": text,
              "response_format": "mp3"}
    if OPENAI_TTS_MODEL.startswith("gpt-") and OPENAI_TTS_INSTRUCTIONS:
        kwargs["instructions"] = OPENAI_TTS_INSTRUCTIONS
    try:
        response = client.audio.speech.create(**kwargs)
    except openai.RateLimitError as exc:
        raise QuotaError(f"OpenAI quota or rate limit: {exc.message}") from exc
    except openai.AuthenticationError as exc:
        raise AudioGenError("OpenAI rejected the API key. Check OPENAI_API_KEY.") from exc
    except openai.BadRequestError as exc:
        raise AudioGenError(f"OpenAI refused the request: {exc.message}") from exc
    except openai.APIConnectionError as exc:
        raise AudioGenError(f"Could not reach OpenAI: {exc}") from exc
    except openai.APIError as exc:
        raise AudioGenError(f"OpenAI error: {exc}") from exc
    data = response.content
    if not data:
        raise AudioGenError("OpenAI returned no audio data.")
    return data


def synthesize_elevenlabs(text: str) -> bytes:
    key = get_secret("ELEVENLABS_API_KEY")
    try:
        resp = requests.post(
            ELEVENLABS_URL.format(voice_id=ELEVENLABS_VOICE_ID),
            headers={"xi-api-key": key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
            json={"text": text, "model_id": ELEVENLABS_MODEL,
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AudioGenError(f"Could not reach ElevenLabs: {exc}") from exc

    if resp.status_code == 200 and resp.content:
        return resp.content
    try:
        detail = resp.json().get("detail", {})
        status = detail.get("status", "") if isinstance(detail, dict) else ""
        message = detail.get("message", detail) if isinstance(detail, dict) else detail
    except ValueError:
        status, message = "", resp.text[:200]
    if resp.status_code == 429 or status in ("quota_exceeded", "too_many_concurrent_requests"):
        raise QuotaError(f"ElevenLabs quota or rate limit: {message}")
    if resp.status_code == 401:
        raise AudioGenError("ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY.")
    raise AudioGenError(f"ElevenLabs error (HTTP {resp.status_code}): {message}")


PROVIDERS = {"openai": synthesize_openai, "elevenlabs": synthesize_elevenlabs}


def synthesize(text: str, provider: str | None = None, fallback: str | None = None) -> tuple[bytes, str]:
    """
    Make speech with the primary provider; on quota exhaustion or a missing
    key, try the fallback provider if one is configured. Returns (mp3, provider).
    """
    provider = provider or TTS_PROVIDER
    fallback = TTS_FALLBACK if fallback is None else fallback
    if provider not in PROVIDERS:
        raise AudioGenError(f"Unknown TTS_PROVIDER '{provider}'. Choose from: {', '.join(PROVIDERS)}")
    try:
        return PROVIDERS[provider](text), provider
    except (QuotaError, ConfigError) as exc:
        if fallback and fallback != provider and fallback in PROVIDERS:
            log.warning("  %s unavailable (%s). Falling back to %s.", provider, exc, fallback)
            return PROVIDERS[fallback](text), fallback
        raise


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------

def fetch_posts_needing_audio(post_id: int | None = None) -> list:
    with get_connection() as conn:
        if post_id is not None:
            return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchall()
        return conn.execute(
            """
            SELECT * FROM posts
            WHERE media_type = 'video'
              AND (audio_path IS NULL OR audio_path = '')
              AND status NOT IN ('published', 'rejected')
              AND caption IS NOT NULL AND caption != ''
            ORDER BY id
            """
        ).fetchall()


def save_audio(post_id: int, path: Path) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET audio_path = ?, last_error = NULL WHERE id = ?",
                     (str(path), post_id))


def save_error(post_id: int, message: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = ? WHERE id = ?", (message, post_id))


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def process_post(post, dry_run: bool = False) -> bool:
    pid = post["id"]
    text = spoken_text(post)
    log.info("Post #%s (%s)", pid, post["topic"])
    if not text:
        log.error("  skipped: caption is empty after removing hashtags and emojis")
        save_error(pid, "audio: nothing to say (caption empty after cleaning)")
        return False
    log.info("  script (%d chars): %s", len(text), text[:100] + ("..." if len(text) > 100 else ""))
    if dry_run:
        log.info("  would synthesize with %s (dry run)", TTS_PROVIDER)
        return False
    try:
        data, used = synthesize(text)
    except (AudioGenError, ConfigError) as exc:
        log.error("  FAILED: %s", exc)
        save_error(pid, f"audio: {exc}")
        return False
    except Exception as exc:
        log.exception("  crashed unexpectedly")
        save_error(pid, f"audio: unexpected error: {exc}")
        return False

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIO_DIR / f"post_{pid}.mp3"
    path.write_bytes(data)
    save_audio(pid, path)
    log.info("  saved %s (%d KB, via %s)", path.name, len(data) // 1024, used)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate speech audio for video posts.")
    parser.add_argument("--id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    init_db()
    posts = fetch_posts_needing_audio(args.id)
    if not posts:
        log.info("No video posts are missing audio.")
        return 0
    log.info("%d post(s) need audio (provider %s%s)%s", len(posts), TTS_PROVIDER,
             f", fallback {TTS_FALLBACK}" if TTS_FALLBACK else "", " [DRY RUN]" if args.dry_run else "")
    done = sum(process_post(p, dry_run=args.dry_run) for p in posts)
    log.info("Done. %d of %d audio file(s) generated.", done, len(posts))
    return 0 if done == len(posts) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
