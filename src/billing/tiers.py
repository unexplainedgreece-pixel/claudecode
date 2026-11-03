"""
Subscription tiers. Limits are per calendar month.

    free        5 posts / month. AI image posts only.
    pro         50 posts / month. Custom video uploads.
    enterprise  unlimited posts. Custom uploads and AI video reels.

`posts_per_month` None means unlimited.
"""

TIERS = {
    "free": {"label": "Free", "posts_per_month": 5, "uploads": False, "reels": False,
             "price_label": "€0", "blurb": "Try the pipeline with 5 AI image posts a month."},
    "pro": {"label": "Pro", "posts_per_month": 50, "uploads": True, "reels": False,
            "price_label": "€29 / month", "blurb": "50 posts a month plus your own video uploads."},
    "enterprise": {"label": "Enterprise", "posts_per_month": None, "uploads": True, "reels": True,
                   "price_label": "€99 / month",
                   "blurb": "Unlimited posts, custom uploads and AI video reels with voice-over."},
}
TIER_ORDER = ("free", "pro", "enterprise")
DEFAULT_TIER = "free"


def tier_info(tier: str | None) -> dict:
    return TIERS.get(tier or DEFAULT_TIER, TIERS[DEFAULT_TIER])


def is_valid_tier(tier: str) -> bool:
    return tier in TIERS
