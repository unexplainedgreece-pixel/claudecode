"""
Media processor and hosting bridge.

Usage (from the project root):
    python3 -m src.uploader              # every post with a local image not yet hosted
    python3 -m src.uploader --id 3       # one specific post
    python3 -m src.uploader --dry-run    # show what would happen, upload nothing

For each post that has a local image (image_path) but no permanent hosted
URL (hosted_at is empty), the script:
    1. converts the PNG to a high-quality JPEG (Instagram accepts JPEG only),
    2. uploads the JPEG to the configured host,
    3. overwrites image_url with the permanent public link.

Hosts, chosen with MEDIA_HOST in .env:
    imgur  (default)  needs IMGUR_CLIENT_ID. Quick to set up.
                      Works for Facebook and Instagram. NOT for TikTok, which
                      only pulls media from a domain you have verified.
    r2                Cloudflare R2 bucket behind your own domain. Needs
                      R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
                      R2_BUCKET, R2_PUBLIC_URL. Works for all three platforms.
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError

from config.db import get_connection, init_db

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("uploader")

MEDIA_HOST = os.getenv("MEDIA_HOST", "imgur").strip().lower()
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "90"))
IMGUR_UPLOAD_URL = "https://api.imgur.com/3/image"
UPLOAD_TIMEOUT = 120


class UploadError(Exception):
    """Raised when conversion or upload fails for one post."""


class ConfigError(Exception):
    """Raised when a required .env value is missing or still a placeholder."""


def get_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value or value.startswith("your_") or value.endswith("_here"):
        raise ConfigError(
            f"{name} is not set. Open .env and replace the placeholder with the real value."
        )
    return value


# ---------------------------------------------------------------------------
# Step 1: PNG -> JPEG
# ---------------------------------------------------------------------------

def convert_to_jpeg(png_path: Path) -> Path:
    """
    Convert an image file to a JPEG next to it (same name, .jpg extension).
    Transparent areas are flattened onto white, since JPEG has no alpha.
    """
    if not png_path.exists():
        raise UploadError(f"Local image not found: {png_path}. Run the image generator again.")

    jpeg_path = png_path.with_suffix(".jpg")
    try:
        with Image.open(png_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[3])
                rgb = background
            else:
                rgb = img.convert("RGB")
            rgb.save(jpeg_path, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    except UnidentifiedImageError:
        raise UploadError(f"{png_path.name} is not a valid image file.")
    except OSError as exc:
        raise UploadError(f"Could not convert {png_path.name} to JPEG: {exc}") from exc

    return jpeg_path


# ---------------------------------------------------------------------------
# Step 2: upload backends. Each returns (public_url, reference_for_deletion).
# ---------------------------------------------------------------------------

def upload_imgur(jpeg_path: Path, post) -> tuple[str, str]:
    client_id = get_env("IMGUR_CLIENT_ID")
    headers = {"Authorization": f"Client-ID {client_id}"}
    title = (post["topic"] or f"post {post['id']}")[:100]

    try:
        with open(jpeg_path, "rb") as fh:
            resp = requests.post(IMGUR_UPLOAD_URL, headers=headers,
                                 files={"image": (jpeg_path.name, fh, "image/jpeg")},
                                 data={"type": "file", "title": title},
                                 timeout=UPLOAD_TIMEOUT)
    except requests.RequestException as exc:
        raise UploadError(f"Network error talking to Imgur: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        raise UploadError(f"Imgur returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")

    if resp.status_code == 429:
        raise UploadError("Imgur rate limit reached. Wait an hour and run again.")
    if resp.status_code >= 400 or not payload.get("success"):
        err = (payload.get("data") or {}).get("error", payload)
        if isinstance(err, dict):
            err = err.get("message", err)
        raise UploadError(f"Imgur error (HTTP {resp.status_code}): {err}")

    data = payload.get("data") or {}
    link = data.get("link")
    if not link:
        raise UploadError(f"Imgur returned no link: {payload}")
    # Imgur returns http:// links from some endpoints. Instagram needs https.
    link = link.replace("http://", "https://", 1)
    return link, data.get("deletehash", "")


def upload_r2(jpeg_path: Path, post) -> tuple[str, str]:
    account_id = get_env("R2_ACCOUNT_ID")
    access_key = get_env("R2_ACCESS_KEY_ID")
    secret_key = get_env("R2_SECRET_ACCESS_KEY")
    bucket = get_env("R2_BUCKET")
    public_base = get_env("R2_PUBLIC_URL").rstrip("/")

    import boto3  # imported here so Imgur users do not need it configured
    from botocore.exceptions import BotoCoreError, ClientError

    key = f"posts/post_{post['id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
    try:
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        with open(jpeg_path, "rb") as fh:
            client.put_object(Bucket=bucket, Key=key, Body=fh, ContentType="image/jpeg")
    except (BotoCoreError, ClientError) as exc:
        raise UploadError(f"Cloudflare R2 upload failed: {exc}") from exc

    return f"{public_base}/{key}", key


UPLOADERS = {"imgur": upload_imgur, "r2": upload_r2}


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------

def fetch_posts_needing_hosting(post_id: int | None = None) -> list:
    """Posts with a local image that has not been uploaded yet. Or one by id."""
    with get_connection() as conn:
        if post_id is not None:
            return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchall()
        return conn.execute(
            """
            SELECT * FROM posts
            WHERE image_path IS NOT NULL AND image_path != ''
              AND (hosted_at IS NULL OR hosted_at = '')
              AND status != 'published'
            ORDER BY id
            """
        ).fetchall()


def save_hosted(post_id: int, url: str, jpeg_path: Path, ref: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE posts
            SET image_url = ?, jpeg_path = ?, host_ref = ?, hosted_at = ?, last_error = NULL
            WHERE id = ?
            """,
            (url, str(jpeg_path), ref, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), post_id),
        )


def save_error(post_id: int, message: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE posts SET last_error = ? WHERE id = ?", (message, post_id))


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def process_post(post, uploader, dry_run: bool = False) -> bool:
    pid = post["id"]
    log.info("Post #%s (%s)", pid, post["topic"])

    if not post["image_path"]:
        log.error("  skipped: no local image. Run the image generator first.")
        save_error(pid, "upload: no local image_path")
        return False

    png_path = Path(post["image_path"])
    if dry_run:
        log.info("  would convert %s and upload via %s (dry run)", png_path.name, MEDIA_HOST)
        return False

    try:
        jpeg_path = convert_to_jpeg(png_path)
        log.info("  converted to JPEG: %s (%d KB)", jpeg_path.name, jpeg_path.stat().st_size // 1024)
        url, ref = uploader(jpeg_path, post)
    except ConfigError as exc:
        log.error("  skipped: %s", exc)
        save_error(pid, f"upload: {exc}")
        return False
    except UploadError as exc:
        log.error("  FAILED: %s", exc)
        save_error(pid, f"upload: {exc}")
        return False
    except Exception as exc:
        log.exception("  crashed unexpectedly")
        save_error(pid, f"upload: unexpected error: {exc}")
        return False

    save_hosted(pid, url, jpeg_path, ref)
    log.info("  hosted at %s", url)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Convert post images to JPEG and host them.")
    parser.add_argument("--id", type=int, help="Process only this post id")
    parser.add_argument("--dry-run", action="store_true", help="Convert and upload nothing")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    uploader = UPLOADERS.get(MEDIA_HOST)
    if uploader is None:
        log.error("Unknown MEDIA_HOST '%s' in .env. Choose from: %s",
                  MEDIA_HOST, ", ".join(UPLOADERS))
        return 2

    init_db()
    posts = fetch_posts_needing_hosting(args.id)
    if not posts:
        log.info("No posts are waiting for hosting.")
        return 0

    log.info("%d post(s) to host via %s%s", len(posts), MEDIA_HOST,
             " [DRY RUN]" if args.dry_run else "")
    done = sum(process_post(p, uploader, dry_run=args.dry_run) for p in posts)
    log.info("Done. %d of %d image(s) hosted.", done, len(posts))
    return 0 if done == len(posts) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
