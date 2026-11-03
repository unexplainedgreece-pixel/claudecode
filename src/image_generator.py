"""
Image generator: creates an image for every draft post that has none.

Usage (from the project root):
    python3 -m src.image_generator              # all drafts missing an image
    python3 -m src.image_generator --id 3       # one specific post
    python3 -m src.image_generator --dry-run    # show prompts, call no API

For each draft post with an image_prompt but no image_url, the script sends
the prompt to OpenAI's image API, stores the returned URL in image_url, and
also downloads a local copy to data/images/post_<id>.png.

IMPORTANT LIMITATION: the URL OpenAI returns expires after about one hour.
Instagram and TikTok fetch the image from that URL at publish time, so the
publisher must run within the hour. The local copy is kept so the image is
never lost; a later step will upload it to permanent hosting.
"""

import argparse
import base64
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import openai
import requests
from dotenv import load_dotenv
from openai import OpenAI

from config.db import DATA_DIR, get_connection, init_db

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("image_generator")

# Change in .env if needed: OPENAI_IMAGE_MODEL, OPENAI_IMAGE_SIZE, OPENAI_IMAGE_QUALITY
IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "dall-e-3")
IMAGE_SIZE = os.getenv("OPENAI_IMAGE_SIZE", "1024x1024")     # square suits Instagram/Facebook
IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "standard")  # "standard" or "hd" (hd costs 2x)

IMAGES_DIR = DATA_DIR / "images"
DOWNLOAD_TIMEOUT = 60

# Extra guidance appended to every prompt so results suit social media.
PROMPT_SUFFIX = " Photorealistic, well lit, no text or watermarks in the image."


class ImageGenError(Exception):
    """Raised when an image could not be generated or saved for one post."""


# ---------------------------------------------------------------------------
# OpenAI side
# ---------------------------------------------------------------------------

def get_client() -> OpenAI:
    """Return an OpenAI client, or stop cleanly if the key is a placeholder."""
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or api_key.startswith("your_"):
        sys.exit(
            "OPENAI_API_KEY is not set. Open the .env file in the project root "
            "and replace 'your_openai_key_here' with your real OpenAI API key."
        )
    return OpenAI(api_key=api_key)


def generate_image(client: OpenAI, prompt: str) -> dict:
    """
    Ask OpenAI for one image. Returns {"url": ..., "b64": ..., "revised_prompt": ...}.
    Exactly one of url / b64 will be set, depending on the model.
    """
    try:
        result = client.images.generate(
            model=IMAGE_MODEL,
            prompt=prompt.strip() + PROMPT_SUFFIX,
            size=IMAGE_SIZE,
            quality=IMAGE_QUALITY,
            n=1,
        )
    except openai.AuthenticationError:
        # Wrong key affects every post, so stop the whole run.
        sys.exit("OpenAI rejected the API key. Check OPENAI_API_KEY in .env.")
    except openai.BadRequestError as exc:
        # Most common cause: the prompt tripped OpenAI's content policy.
        raise ImageGenError(f"OpenAI refused the prompt: {exc.message}") from exc
    except openai.RateLimitError as exc:
        raise ImageGenError(f"OpenAI rate limit or no credit: {exc.message}") from exc
    except openai.APIConnectionError as exc:
        raise ImageGenError(f"Could not reach OpenAI: {exc}") from exc
    except openai.APIError as exc:
        raise ImageGenError(f"OpenAI error: {exc}") from exc

    if not result.data:
        raise ImageGenError("OpenAI returned no image data.")
    image = result.data[0]
    return {"url": image.url, "b64": image.b64_json, "revised_prompt": image.revised_prompt}


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def save_local_copy(post_id: int, url: str | None, b64: str | None) -> Path:
    """Write the image to data/images/post_<id>.png and return the path."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / f"post_{post_id}.png"

    if b64:
        data = base64.b64decode(b64)
    else:
        try:
            resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ImageGenError(f"Image was generated but download failed: {exc}") from exc
        data = resp.content

    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------

def fetch_posts_needing_images(post_id: int | None = None) -> list:
    """Draft posts with a prompt but no image_url. Or one post by id."""
    with get_connection() as conn:
        if post_id is not None:
            return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchall()
        return conn.execute(
            """
            SELECT * FROM posts
            WHERE status = 'draft'
              AND (image_url IS NULL OR image_url = '')
              AND image_prompt IS NOT NULL AND image_prompt != ''
            ORDER BY id
            """
        ).fetchall()


def save_image(post_id: int, url: str | None, path: Path) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE posts SET image_url = ?, image_path = ?, last_error = NULL WHERE id = ?",
            (url, str(path), post_id),
        )


def save_error(post_id: int, message: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = ? WHERE id = ?", (message, post_id))


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def process_post(client, post, dry_run: bool = False) -> bool:
    """Generate and store an image for one post. Returns True on success."""
    pid = post["id"]
    prompt = (post["image_prompt"] or "").strip()
    log.info("Post #%s (%s)", pid, post["topic"])

    if not prompt:
        log.error("  skipped: post has no image_prompt")
        save_error(pid, "image: no image_prompt to generate from")
        return False
    if post["image_url"]:
        log.info("  already has an image_url, skipping")
        return True

    log.info("  prompt: %s", prompt[:120] + ("..." if len(prompt) > 120 else ""))
    if dry_run:
        log.info("  would generate image (dry run)")
        return False

    try:
        result = generate_image(client, prompt)
        path = save_local_copy(pid, result["url"], result["b64"])
    except ImageGenError as exc:
        log.error("  FAILED: %s", exc)
        save_error(pid, f"image: {exc}")
        return False
    except Exception as exc:  # unexpected: record it, keep going with other posts
        log.exception("  crashed unexpectedly")
        save_error(pid, f"image: unexpected error: {exc}")
        return False

    save_image(pid, result["url"], path)
    log.info("  saved local copy: %s", path)
    if result["url"]:
        log.info("  image_url set (expires in about 1 hour: run the publisher soon)")
    else:
        log.warning("  model %s returned no URL. Local copy saved, but image_url is empty: "
                    "the post needs hosting before Instagram/TikTok can use it.", IMAGE_MODEL)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate images for draft posts.")
    parser.add_argument("--id", type=int, help="Generate for only this post id")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show which posts and prompts would be used, call no API")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    init_db()
    posts = fetch_posts_needing_images(args.id)
    if not posts:
        log.info("No draft posts are missing an image.")
        return 0

    client = None if args.dry_run else get_client()
    log.info("%d post(s) need an image (model %s, %s, %s)%s", len(posts), IMAGE_MODEL,
             IMAGE_SIZE, IMAGE_QUALITY, " [DRY RUN]" if args.dry_run else "")

    done = sum(process_post(client, p, dry_run=args.dry_run) for p in posts)
    log.info("Done. %d of %d image(s) generated.", done, len(posts))
    return 0 if done == len(posts) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
