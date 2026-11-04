"""
Content generator: turns a topic into a draft social media post.

Usage (from the project root):
    python3 -m src.generator "Why Google reviews matter for Rhodes restaurants"
    python3 -m src.generator "Summer menu launch" --platform instagram --business "Taverna Nikos"

Each run asks OpenAI for a caption, a list of hashtags and an image prompt,
then saves the result into the `posts` table with status 'draft'.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from config.db import get_connection, init_db
from src.ai import providers
from src.security.secrets import ConfigError

# Load variables from the .env file in the project root.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SYSTEM_PROMPT = """You are a social media copywriter for small local businesses and content pages.
You write short, friendly, non-salesy posts in natural language.
If two languages are requested, write the caption in the first language, then a blank line, then the same
in the second language. The title is in the first language only.
Always answer with a single JSON object and nothing else, using exactly these keys:
  "title":        3-6 words that will be printed on the picture (a hook, no hashtags, no emojis)
  "caption":      the post text (max 2 short paragraphs, may include 1-2 emojis)
  "hashtags":     a list of 8-12 relevant hashtags, each starting with #
  "image_prompt": one sentence describing a photo or image to go with the post; no text in the image
  "image_query":  3-6 English words naming the real place, object or scene for a photo-library search
                  (for example "Baalbek trilithon stones"); empty string if nothing real fits
"""


PAGE_RULES = """This is a content page with followers, NOT a business: it sells nothing. Never write offers,
discounts, tours, bookings, 'our team', 'visit us', 'behind the scenes' or any call to buy or visit.
Every post is about ONE real, documented place, object or phenomenon (never invented, never a
composite). State only facts you are sure of (age, size, weight, who found it); if unsure, leave the
number out rather than guess. End the caption with one open question that makes the reader look at
the picture and think. No two posts about the same place."""
LANGUAGE_RULES = ("Greek must read as a native Greek speaker writes it: correct grammar, cases and accents, "
                  "natural word order, no literal translation from English. Proofread it before answering.")


def _brand_context(user_id: int | None) -> str:
    """The user's brand brief from their profile, if any."""
    if not user_id:
        return ""
    try:
        from src.library import brand_profile
        brand = brand_profile(user_id)
    except Exception:  # noqa: BLE001
        return ""
    bits = []
    is_page = brand.get("brand_kind") == "page"
    if brand.get("brand_name"):
        bits.append(f"{'Page' if is_page else 'Business'}: {brand['brand_name']}")
    if brand.get("brand_brief"):
        bits.append(f"About {'the page' if is_page else 'the business'} and tone: {brand['brand_brief']}")
    if is_page:
        bits.append(PAGE_RULES)
    if brand.get("brand_language"):
        bits.append(f"Write in: {brand['brand_language']}. " + LANGUAGE_RULES)
    try:
        from src.library import pillar_hint
        hint = pillar_hint(brand)
        if hint:
            bits.append(hint.strip())
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(bits)


def generate_post(topic: str, platform: str = "instagram", business: str = "",
                  user_id: int | None = None, facts: str = "") -> dict:
    """
    Ask the text model (Gemini or OpenAI, whichever the user has a key for)
    for a caption, hashtags and image prompt for the given topic.

    Returns a dict with keys: topic, caption, hashtags, image_prompt.
    `hashtags` is returned as a single space-separated string, ready to store.
    """
    user_prompt = f"Platform: {platform}\nTopic: {topic}"
    if business:
        user_prompt += f"\nBusiness: {business}"
    if facts:
        user_prompt += ("\n\nVERIFIED FACTS (from Wikipedia). Use ONLY these; do not add any number, date, "
                        "name or claim that is not here, and do not invent alignments, energies or theories:\n" + facts.strip())
    brand = _brand_context(user_id)
    if brand:
        user_prompt += "\n" + brand

    raw = providers.text(SYSTEM_PROMPT + "\n\n" + user_prompt, user_id, json_mode=True)
    start, end = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[start:end + 1] if start >= 0 else raw)

    hashtags = data.get("hashtags", [])
    if isinstance(hashtags, list):
        hashtags = " ".join(hashtags)

    return {
        "topic": topic,
        "title": str(data.get("title") or "").strip()[:80],
        "caption": data.get("caption", "").strip(),
        "hashtags": hashtags.strip(),
        "image_prompt": data.get("image_prompt", "").strip(),
        "image_query": str(data.get("image_query") or "").strip()[:120],
    }


def save_post(post: dict, media_type: str = "image", user_id: int | None = None) -> int:
    """
    Insert a generated post into the database as a draft. Returns the new row id.
    media_type 'image' = photo post; 'video' = vertical TikTok/Reel compiled from
    the image plus a spoken version of the caption.
    """
    if media_type not in ("image", "video"):
        raise ValueError(f"media_type must be 'image' or 'video', not {media_type!r}")
    init_db()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts (topic, caption, hashtags, image_prompt, status, media_type, user_id, source)
            VALUES (?, ?, ?, ?, 'draft', ?, ?, 'ai')
            """,
            (post["topic"], post["caption"], post["hashtags"], post["image_prompt"], media_type, user_id),
        )
        return cursor.lastrowid


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a draft social media post.")
    parser.add_argument("topic", help="What the post should be about")
    parser.add_argument("--platform", default="instagram",
                        help="instagram, facebook or tiktok (default: instagram)")
    parser.add_argument("--business", default="",
                        help="Optional business name to mention in the post")
    parser.add_argument("--media-type", default="image", choices=("image", "video"),
                        help="image = photo post, video = TikTok/Reel with voice-over (default: image)")
    args = parser.parse_args()

    print(f"Generating post about: {args.topic} ...")
    try:
        post = generate_post(args.topic, platform=args.platform, business=args.business)
    except (ConfigError, providers.AIError) as exc:
        sys.exit(str(exc))
    post_id = save_post(post, media_type=args.media_type)

    print(f"\nSaved as {args.media_type} draft #{post_id}\n")
    print("CAPTION:\n" + post["caption"] + "\n")
    print("HASHTAGS:\n" + post["hashtags"] + "\n")
    print("IMAGE PROMPT:\n" + post["image_prompt"])


if __name__ == "__main__":
    main()
