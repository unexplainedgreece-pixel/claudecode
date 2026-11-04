"""
Image generator: creates an image for every draft post that has none.

Usage (from the project root):
    python3 -m src.image_generator              # all drafts missing an image
    python3 -m src.image_generator --id 3       # one specific post
    python3 -m src.image_generator --dry-run    # show prompts, call no API

For each AI draft post with an image_prompt but no image yet, the script asks
the image provider (Cloudflare Workers AI, Gemini or OpenAI, whichever the
user has a key for; IMAGE_PROVIDER forces one) and saves the result to
data/images/post_<id>.png. The uploader then converts and hosts it.
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from config.db import DATA_DIR, get_connection, init_db
from src.ai import providers
from src.security.secrets import ConfigError

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("image_generator")


IMAGES_DIR = DATA_DIR / "images"

# Extra guidance appended to every prompt so results suit social media.
PROMPT_SUFFIX = " Photorealistic, well lit, no text or watermarks in the image."


class ImageGenError(Exception):
    """Raised when an image could not be generated or saved for one post."""


# ---------------------------------------------------------------------------
# OpenAI side
# ---------------------------------------------------------------------------

def generate_image(prompt: str, user_id: int | None = None) -> bytes:
    """
    Make one image with whichever provider the user has (Cloudflare, Gemini or
    OpenAI). Returns the image bytes. Raises ImageGenError with a clean message.
    """
    try:
        return providers.image(prompt.strip() + PROMPT_SUFFIX, user_id)
    except providers.QuotaError as exc:
        raise ImageGenError(f"Out of quota: {exc}") from exc
    except providers.AIError as exc:
        raise ImageGenError(str(exc)) from exc
    except ConfigError as exc:
        raise ImageGenError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def save_local_copy(post_id: int, data: bytes) -> Path:
    """Write the image to data/images/post_<id>.png and return the path."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / f"post_{post_id}.png"
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------

def fetch_posts_needing_images(post_id: int | None = None, user_id: int | None = None) -> list:
    """AI draft posts with a prompt but no image_url. Or one post by id."""
    with get_connection() as conn:
        if post_id is not None:
            return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchall()
        scope, params = (" AND user_id = ?", (user_id,)) if user_id else ("", ())
        return conn.execute(
            f"""
            SELECT * FROM posts
            WHERE status = 'draft'
              AND COALESCE(source, 'ai') = 'ai'
              AND (image_url IS NULL OR image_url = '')
              AND (image_path IS NULL OR image_path = '')
              AND image_prompt IS NOT NULL AND image_prompt != ''{scope}
            ORDER BY id
            """, params
        ).fetchall()


def save_image(post_id: int, path: Path) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE posts SET image_path = ?, last_error = NULL WHERE id = ?",
            (str(path), post_id),
        )


def save_error(post_id: int, message: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = ? WHERE id = ?", (message, post_id))


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def process_post(post, dry_run: bool = False) -> bool:
    """Generate and store an image for one post. Returns True on success."""
    pid = post["id"]
    prompt = (post["image_prompt"] or "").strip()
    log.info("Post #%s (%s)", pid, post["topic"])

    if not prompt:
        log.error("  skipped: post has no image_prompt")
        save_error(pid, "image: no image_prompt to generate from")
        return False
    if post["image_url"] or (post["image_path"] and Path(post["image_path"]).exists()):
        log.info("  already has an image, skipping")
        return True

    provider = providers.pick("image", post["user_id"])
    log.info("  prompt (%s): %s", provider, prompt[:120] + ("..." if len(prompt) > 120 else ""))
    if dry_run:
        log.info("  would generate image with %s (dry run)", provider)
        return False

    try:
        data = generate_image(prompt, post["user_id"])
        path = save_local_copy(pid, data)
    except ImageGenError as exc:
        log.error("  FAILED: %s", exc)
        save_error(pid, f"image: {exc}")
        return False
    except Exception as exc:  # unexpected: record it, keep going with other posts
        log.exception("  crashed unexpectedly")
        save_error(pid, f"image: unexpected error: {exc}")
        return False

    save_image(pid, path)
    log.info("  saved %s (%d KB)", path.name, len(data) // 1024)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate images for draft posts.")
    parser.add_argument("--id", type=int, help="Generate for only this post id")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show which posts and prompts would be used, call no API")
    parser.add_argument("--user", type=int, default=None,
                        help="Only this user's posts (default: every user's)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    init_db()
    posts = fetch_posts_needing_images(args.id, args.user)
    if not posts:
        log.info("No draft posts are missing an image.")
        return 0

    log.info("%d post(s) need an image%s", len(posts), " [DRY RUN]" if args.dry_run else "")

    done = sum(process_post(p, dry_run=args.dry_run) for p in posts)
    log.info("Done. %d of %d image(s) generated.", done, len(posts))
    return 0 if done == len(posts) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
