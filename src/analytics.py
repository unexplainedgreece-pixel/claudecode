"""
Performance analytics: pull likes, comments, reach and views for published
posts from Meta, keep them in post_metrics, and build client reports.

    python3 -m src.analytics fetch            # refresh metrics for recent published posts
    python3 -m src.analytics report --user 1 --month 2026-11

Facebook: GET /{post-id}?fields=likes.summary(true),comments.summary(true),shares
          plus /{post-id}/insights (post_impressions_unique, post_impressions) when allowed.
Instagram: GET /{media-id}/insights?metric=views,reach,likes,comments,shares,saved,total_interactions
          (impressions was retired by Meta in 2025; views replaced it).
Metrics are refreshed at most once a day per post, for posts published in the
last REFRESH_DAYS days. Data can lag Meta by up to 48 hours.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from config.db import get_connection, init_db
from src import publisher
from src.security.secrets import ConfigError

log = logging.getLogger("analytics")
REFRESH_DAYS = 60
IG_METRICS = "views,reach,likes,comments,shares,saved,total_interactions"
FB_INSIGHTS = "post_impressions_unique,post_impressions"


class AnalyticsError(Exception):
    pass


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_facebook(post) -> dict:
    creds = publisher.meta_credentials(post)
    token = creds["token"]
    fid = post["fb_post_id"]
    data = publisher.meta_request(
        "GET", f"{publisher.META_GRAPH_URL}/{fid}",
        params={"fields": "likes.summary(true),comments.summary(true),shares", "access_token": token})
    out = {"likes": (data.get("likes") or {}).get("summary", {}).get("total_count"),
           "comments": (data.get("comments") or {}).get("summary", {}).get("total_count"),
           "shares": (data.get("shares") or {}).get("count"), "views": None, "reach": None, "saved": None}
    try:
        ins = publisher.meta_request("GET", f"{publisher.META_GRAPH_URL}/{fid}/insights",
                                     params={"metric": FB_INSIGHTS, "access_token": token})
        for item in ins.get("data", []):
            value = (item.get("values") or [{}])[0].get("value")
            if item.get("name") == "post_impressions_unique":
                out["reach"] = value
            elif item.get("name") == "post_impressions":
                out["views"] = value
    except publisher.PublishError as exc:   # insights need pages_read_engagement; fine without
        log.debug("  facebook insights unavailable: %s", exc)
    out["raw"] = data
    return out


def fetch_instagram(post) -> dict:
    creds = publisher.meta_credentials(post)
    mid = post["ig_post_id"]
    ins = publisher.meta_request("GET", f"{publisher.META_GRAPH_URL}/{mid}/insights",
                                 params={"metric": IG_METRICS, "access_token": creds["token"]})
    out = {"views": None, "reach": None, "likes": None, "comments": None, "shares": None, "saved": None}
    for item in ins.get("data", []):
        value = (item.get("values") or [{}])[0].get("value")
        name = item.get("name")
        if name in out:
            out[name] = value
        elif name == "total_interactions":
            out["engagement"] = value
    out["raw"] = ins
    return out


FETCHERS = {"facebook": fetch_facebook, "instagram": fetch_instagram}


def _engagement(m: dict) -> int | None:
    parts = [m.get(k) for k in ("likes", "comments", "shares", "saved")]
    if all(p is None for p in parts):
        return m.get("engagement")
    return sum(p or 0 for p in parts)


def save_metrics(post, platform: str, m: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO post_metrics (post_id, user_id, platform, fetched_at, views, reach, likes, comments, "
            "shares, saved, engagement, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(post_id, platform) DO UPDATE SET fetched_at = excluded.fetched_at, views = excluded.views, "
            "reach = excluded.reach, likes = excluded.likes, comments = excluded.comments, shares = excluded.shares, "
            "saved = excluded.saved, engagement = excluded.engagement, raw = excluded.raw",
            (post["id"], post["user_id"], platform, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             m.get("views"), m.get("reach"), m.get("likes"), m.get("comments"), m.get("shares"), m.get("saved"),
             _engagement(m), json.dumps(m.get("raw"), ensure_ascii=False)[:20000]))


def posts_to_refresh(user_id: int | None = None, force: bool = False) -> list[dict]:
    since = (datetime.now() - timedelta(days=REFRESH_DAYS)).strftime("%Y-%m-%d")
    stale = (datetime.now() - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")
    scope, params = (" AND p.user_id = ?", [user_id]) if user_id else ("", [])
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT p.* FROM posts p
            WHERE p.status = 'published' AND p.published_at >= ?
              AND (p.fb_post_id IS NOT NULL OR p.ig_post_id IS NOT NULL){scope}
            ORDER BY p.published_at DESC
            """, [since, *params]).fetchall()
        fetched = {(r["post_id"], r["platform"]): r["fetched_at"] for r in conn.execute(
            "SELECT post_id, platform, fetched_at FROM post_metrics")}
    out = []
    for p in rows:
        p = dict(p)
        due = []
        for platform, col in (("facebook", "fb_post_id"), ("instagram", "ig_post_id")):
            if p.get(col) and (force or (fetched.get((p["id"], platform)) or "") < stale):
                due.append(platform)
        if due:
            p["_platforms"] = due
            out.append(p)
    return out


def refresh(user_id: int | None = None, force: bool = False) -> dict:
    todo = posts_to_refresh(user_id, force)
    done, failed = 0, 0
    for post in todo:
        for platform in post["_platforms"]:
            try:
                save_metrics(post, platform, FETCHERS[platform](post))
                done += 1
            except (publisher.PublishError, ConfigError) as exc:
                failed += 1
                log.warning("  post #%s %s: %s", post["id"], platform, exc)
    log.info("Metrics refreshed: %d ok, %d failed, %d post(s)", done, failed, len(todo))
    return {"ok": done, "failed": failed, "posts": len(todo)}


def run_step(user_id: int | None = None) -> int:
    """Pipeline step entry point."""
    r = refresh(user_id)
    return 1 if r["failed"] and not r["ok"] else 0


# ---------------------------------------------------------------------------
# Best times to post (when your followers are online)
# ---------------------------------------------------------------------------

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def online_followers(user_id: int, page_id: int | None = None) -> dict[int, dict[int, float]]:
    """
    {weekday: {hour: average online followers}} from Instagram's online_followers
    insight over the last 30 days (needs 100+ followers), with the Facebook
    Page's page_fans_online as a fallback. Empty dict when neither is available.
    """
    from src import pages as pages_store
    page = pages_store.get_page(page_id, user_id) if page_id else (pages_store.list_pages(user_id) or [None])[0]
    if page and "token" not in page:
        page = pages_store.get_page(page["id"], user_id)
    fake_post = {"user_id": user_id, "page_id": page["id"] if page else None}
    creds = publisher.meta_credentials(fake_post)
    token = creds["token"]
    since = int((datetime.now() - timedelta(days=30)).timestamp())
    until = int(datetime.now().timestamp())
    grid: dict[int, dict[int, list]] = {d: {h: [] for h in range(24)} for d in range(7)}
    data = None
    if creds.get("ig_id"):
        try:
            data = publisher.meta_request("GET", f"{publisher.META_GRAPH_URL}/{creds['ig_id']}/insights",
                                          params={"metric": "online_followers", "period": "lifetime", "since": since,
                                                  "until": until, "access_token": token}).get("data") or []
        except publisher.PublishError as exc:
            log.info("online_followers unavailable: %s", exc)
    if not data:
        try:
            data = publisher.meta_request("GET", f"{publisher.META_GRAPH_URL}/{creds['page_id']}/insights",
                                          params={"metric": "page_fans_online", "period": "day", "since": since,
                                                  "until": until, "access_token": token}).get("data") or []
        except publisher.PublishError as exc:
            log.info("page_fans_online unavailable: %s", exc)
            return {}
    for item in data:
        for v in item.get("values") or []:
            end = v.get("end_time", "")
            counts = v.get("value") or {}
            if not isinstance(counts, dict) or not end:
                continue
            try:
                day = datetime.strptime(end[:10], "%Y-%m-%d") - timedelta(days=1)   # end_time is the day after
            except ValueError:
                continue
            for hour, n in counts.items():
                try:
                    grid[day.weekday()][int(hour)].append(float(n))
                except (ValueError, TypeError):
                    continue
    return {d: {h: (sum(v) / len(v)) for h, v in hours.items() if v} for d, hours in grid.items()}


def suggest_slots(user_id: int, page_id: int | None = None, per_day: int = 2, hour_from: int = 8,
                  hour_to: int = 22) -> str | None:
    """A posting_slots string from the hours your followers are online most (UTC hours as Meta gives them)."""
    grid = online_followers(user_id, page_id)
    if not any(grid.values()):
        return None
    groups: dict[str, list[str]] = {}
    for d in range(7):
        hours = {h: n for h, n in grid.get(d, {}).items() if hour_from <= h <= hour_to}
        if not hours:
            continue
        best = sorted(sorted(hours, key=hours.get, reverse=True)[:per_day])
        key = ",".join(f"{h:02d}:00" for h in best)
        groups.setdefault(key, []).append(WEEKDAYS[d])
    return "; ".join(f"{','.join(days)} {times}" for times, days in groups.items()) or None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def month_bounds(month: str | None) -> tuple[str, str, str]:
    """'2026-11' -> ('2026-11-01', '2026-12-01', '2026-11'). Default: current month."""
    if not month:
        month = datetime.now().strftime("%Y-%m")
    start = datetime.strptime(month, "%Y-%m")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), month


def performance(user_id: int, month: str | None = None, page_id: int | None = None) -> dict:
    """Everything the Performance page and the client report need for one month."""
    start, end, month = month_bounds(month)
    scope, params = "", [user_id, start, end]
    if page_id:
        scope = " AND p.page_id = ?"
        params.append(page_id)
    with get_connection() as conn:
        posts = [dict(r) for r in conn.execute(
            f"""
            SELECT p.id, p.topic, p.caption, p.media_type, p.source, p.published_at, p.platforms, p.page_id,
                   p.fb_post_id, p.ig_post_id, p.tiktok_publish_id
            FROM posts p WHERE p.user_id = ? AND p.status = 'published' AND p.published_at >= ? AND p.published_at < ?{scope}
            ORDER BY p.published_at DESC
            """, params)]
        ids = [p["id"] for p in posts]
        metrics = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            for r in conn.execute(f"SELECT * FROM post_metrics WHERE post_id IN ({marks})", ids):
                metrics.setdefault(r["post_id"], {})[r["platform"]] = dict(r)
    totals = {"posts": len(posts), "facebook": 0, "instagram": 0, "tiktok": 0,
              "views": 0, "reach": 0, "likes": 0, "comments": 0, "shares": 0, "saved": 0, "engagement": 0}
    for p in posts:
        p["metrics"] = metrics.get(p["id"], {})
        p["engagement"] = 0
        for platform, m in p["metrics"].items():
            for key in ("views", "reach", "likes", "comments", "shares", "saved", "engagement"):
                totals[key] += m.get(key) or 0
            p["engagement"] += m.get("engagement") or 0
        for platform, col in (("facebook", "fb_post_id"), ("instagram", "ig_post_id"), ("tiktok", "tiktok_publish_id")):
            if p.get(col):
                totals[platform] += 1
    top = sorted(posts, key=lambda p: p["engagement"], reverse=True)[:5]
    prev_start = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m")
    return {"month": month, "start": start, "end": end, "posts": posts, "totals": totals, "top": top,
            "prev_month": prev_start, "next_month": datetime.strptime(end, "%Y-%m-%d").strftime("%Y-%m"),
            "avg_engagement": round(totals["engagement"] / len(posts), 1) if posts else 0}


def report_csv(data: dict) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["post_id", "published_at", "title", "platform", "views", "reach", "likes", "comments", "shares", "saved", "engagement"])
    for p in data["posts"]:
        for platform, m in sorted(p["metrics"].items()):
            w.writerow([p["id"], p["published_at"], p["topic"], platform, m.get("views"), m.get("reach"), m.get("likes"),
                        m.get("comments"), m.get("shares"), m.get("saved"), m.get("engagement")])
        if not p["metrics"]:
            w.writerow([p["id"], p["published_at"], p["topic"], "", "", "", "", "", "", "", ""])
    return buf.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Post performance metrics.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--user", type=int, default=None)
    f.add_argument("--force", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--user", type=int, required=True)
    r.add_argument("--month", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    init_db()
    if args.cmd == "fetch":
        return run_step(args.user) if not args.force else (0 if refresh(args.user, True)["failed"] == 0 else 1)
    data = performance(args.user, args.month)
    print(report_csv(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
