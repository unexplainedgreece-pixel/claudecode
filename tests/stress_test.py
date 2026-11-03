#!/usr/bin/env python3
"""
Automated stress test and simulation suite.

Run from the project root:
    python3 tests/stress_test.py                 # full run, writes STRESS_TEST_REPORT.md
    python3 tests/stress_test.py --posts 2000 --requests 200 --workers 20   # quicker

Phases:
    1. Inflate a temporary database with N mock posts across every status.
    2. Time the queries the dashboard and pipeline use against that database.
    3. Concurrency: W workers hammer the database at the same time, W publisher
       workers race for the same approved posts, W orchestrator runs overlap.
       Counts "database is locked" errors and double publishes.
    4. Dashboard benchmark: R HTTP requests against a real uvicorn server.
    5. Memory: peak RSS of this process and of the dashboard server.

Everything runs against a temporary database. data/scheduler.db is untouched.
No real network calls: the publisher's HTTP layer is replaced by a fake.
"""

import argparse
import os
import platform
import random
import resource
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP_DIR = Path(tempfile.mkdtemp(prefix="stress_"))
DB_PATH = TMP_DIR / "stress.db"
os.environ["SCHEDULER_DB_PATH"] = str(DB_PATH)
os.environ.update({
    "APP_SECRET_KEY": "c3RyZXNzLXRlc3Qta2V5LXN0cmVzcy10ZXN0LWtleS0=",
    "META_ACCESS_TOKEN": "EAAstress", "META_PAGE_ID": "111",
    "META_INSTAGRAM_ACCOUNT_ID": "222", "TIKTOK_ACCESS_TOKEN": "act.stress",
})

import httpx                                              # noqa: E402
from config.db import get_connection, init_db            # noqa: E402
from src import main as orch                              # noqa: E402
from src import publisher                                 # noqa: E402
from src.web import queries                               # noqa: E402

TOPICS = ["Summer menu launch", "Google reviews matter", "Sunset drinks", "Weekend brunch",
          "Meet the team", "Live music Friday", "Holiday hours", "New cocktail list",
          "Local produce", "Family Sunday"]
REPORT = ROOT / "STRESS_TEST_REPORT.md"


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f} ms"


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(p / 100 * len(values)) - 1))
    return values[k]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# ---------------------------------------------------------------------------
# Phase 1: inflate
# ---------------------------------------------------------------------------

def phase_inflate(n: int) -> dict:
    init_db()
    rng = random.Random(42)
    start_day = datetime(2026, 1, 1)
    rows = []
    counts = {"draft": 0, "approved": 0, "scheduled": 0, "published": 0, "rejected": 0, "with_error": 0}
    for i in range(n):
        r = rng.random()
        status = ("draft" if r < 0.30 else "approved" if r < 0.40 else "scheduled" if r < 0.55
                  else "published" if r < 0.95 else "rejected")
        day = start_day + timedelta(days=rng.randint(0, 364), hours=rng.randint(6, 22))
        when = day.strftime("%Y-%m-%d %H:%M")
        has_img = status != "draft" or rng.random() < 0.6
        err = None
        if rng.random() < 0.05 and status != "published":
            err = rng.choice(["instagram: Meta API error 190: Session has expired",
                              "upload: Imgur rate limit reached. Wait an hour and run again.",
                              "image: OpenAI refused the prompt: safety system"])
            counts["with_error"] += 1
        counts[status] += 1
        rows.append((
            f"{rng.choice(TOPICS)} #{i}", "Caption " * rng.randint(5, 40),
            " ".join(f"#tag{rng.randint(1, 500)}" for _ in range(rng.randint(5, 12))),
            "A photo of " + rng.choice(TOPICS).lower(), status,
            when if status in ("scheduled", "approved") else None,
            f"https://i.imgur.com/s{i}.jpeg" if has_img else None,
            f"/tmp/post_{i}.png" if has_img else None,
            when if has_img else None,
            f"111_{i}" if status == "published" else None,
            f"17{i}" if status == "published" else None,
            err, day.strftime("%Y-%m-%d %H:%M:%S") if status == "published" else None,
            day.strftime("%Y-%m-%d %H:%M:%S"),
        ))
    t0 = time.perf_counter()
    with get_connection() as conn:
        conn.executemany(
            "INSERT INTO posts (topic, caption, hashtags, image_prompt, status, scheduled_time, "
            "image_url, image_path, hosted_at, fb_post_id, ig_post_id, last_error, published_at, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    elapsed = time.perf_counter() - t0
    size = DB_PATH.stat().st_size / 1024 / 1024
    return {"n": n, "seconds": elapsed, "rows_per_sec": n / elapsed, "db_mb": size, "counts": counts}


# ---------------------------------------------------------------------------
# Phase 2: query timings
# ---------------------------------------------------------------------------

def phase_queries(reps: int = 20) -> list[dict]:
    targets = [
        ("stats()  (dashboard tiles)", queries.stats),
        ("list_posts(limit=200)", lambda: queries.list_posts(limit=200)),
        ("list_posts(search='brunch')", lambda: queries.list_posts(search="brunch")),
        ("queue()  (approval page)", queries.queue),
        ("calendar_month(2026, 6)", lambda: queries.calendar_month(2026, 6)),
        ("list_errors()", queries.list_errors),
        ("list_runs()", queries.list_runs),
        ("publisher.fetch_due_posts()", publisher.fetch_due_posts),
        ("get_post(random id)", lambda: queries.get_post(random.randint(1, 1000))),
    ]
    out = []
    for name, fn in targets:
        times = []
        rows = None
        for _ in range(reps):
            t0 = time.perf_counter()
            result = fn()
            times.append(time.perf_counter() - t0)
            if rows is None:
                rows = (sum(len(v) for v in result.values() if isinstance(v, list))
                        if isinstance(result, dict) and "drafts" in result
                        else len(result) if isinstance(result, list) else 1)
        out.append({"name": name, "mean": statistics.mean(times), "max": max(times),
                    "p95": pct(times, 95), "rows": rows})
    return out


# ---------------------------------------------------------------------------
# Phase 3: concurrency
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, ""

    def json(self):
        return self._p


def phase_concurrency(workers: int, ops_per_worker: int) -> dict:
    result = {"workers": workers}
    locked = []
    other = []
    draft_ids = [r["id"] for r in queries.list_posts(status="draft", limit=5000)]

    # --- 3a: mixed read/write storm -------------------------------------
    def storm(worker_id):
        rng = random.Random(worker_id)
        for _ in range(ops_per_worker):
            op = rng.random()
            try:
                if op < 0.4:
                    queries.stats()
                elif op < 0.6:
                    queries.list_posts(limit=50)
                elif op < 0.75:
                    queries.update_post(rng.choice(draft_ids), caption=f"edited by {worker_id}")
                elif op < 0.9:
                    queries.set_status(rng.choice(draft_ids), "approved")
                    # and back, so the pool of drafts does not drain
                    queries.set_status(rng.choice(draft_ids), "draft")
                else:
                    queries.clear_error(rng.choice(draft_ids))
            except sqlite3.OperationalError as exc:
                (locked if "locked" in str(exc) else other).append(str(exc))
            except Exception as exc:
                other.append(repr(exc))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(storm, range(workers)))
    result["storm"] = {"ops": workers * ops_per_worker, "seconds": time.perf_counter() - t0,
                       "locked_errors": len(locked), "other_errors": len(other),
                       "sample_error": (locked or other or [""])[0]}

    # --- 3b: publisher workers racing for the same approved posts -------
    # Reset: make exactly `n_posts` posts approved with no platform id.
    with get_connection() as conn:
        conn.execute("UPDATE posts SET status = 'draft' WHERE status = 'approved'")
        ids = [r["id"] for r in conn.execute("SELECT id FROM posts WHERE status='draft' LIMIT 200")]
        conn.executemany("UPDATE posts SET status='approved', fb_post_id=NULL, ig_post_id=NULL, "
                         "tiktok_publish_id=NULL, last_error=NULL WHERE id=?", [(i,) for i in ids])
    api_calls = {}
    api_lock = threading.Lock()

    def fake_meta(method, url, **kw):
        if "/photos" in url or "/feed" in url:
            caption = kw["data"]["message"]
            with api_lock:
                api_calls[caption] = api_calls.get(caption, 0) + 1
            time.sleep(0.002)  # simulate network latency: widens the race window
            return FakeResp({"id": "x", "post_id": "111_" + str(len(api_calls))})
        raise AssertionError(url)

    pub_errors = []

    def publisher_worker(_):
        try:
            for post in publisher.fetch_due_posts():
                publisher.publish_post(post, ["facebook"])
        except sqlite3.OperationalError as exc:
            pub_errors.append(str(exc))

    t0 = time.perf_counter()
    with mock.patch.object(publisher.requests, "request", side_effect=fake_meta), \
         mock.patch.object(publisher.time, "sleep", lambda s: None):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(publisher_worker, range(workers)))
    with get_connection() as conn:
        published = conn.execute("SELECT COUNT(*) FROM posts WHERE status='published' AND id IN (%s)"
                                 % ",".join(map(str, ids))).fetchone()[0]
    duplicates = sum(c - 1 for c in api_calls.values() if c > 1)
    result["race"] = {"posts": len(ids), "published": published, "api_calls": sum(api_calls.values()),
                      "double_publishes": duplicates, "seconds": time.perf_counter() - t0,
                      "locked_errors": len([e for e in pub_errors if "locked" in e])}

    # --- 3c: overlapping orchestrator runs ----------------------------------
    outcomes = []
    with mock.patch.object(orch.image_generator, "main", lambda a: time.sleep(0.05) or 0), \
         mock.patch.object(orch.uploader, "main", lambda a: 0), \
         mock.patch.object(orch.publisher, "main", lambda a: 0):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(lambda _: orch.run_pipeline(steps=["images", "upload", "publish"]),
                                     range(workers)))
    result["orchestrator"] = {"attempted": workers,
                              "ran": sum(1 for o in outcomes if not o.get("skipped")),
                              "skipped": sum(1 for o in outcomes if o.get("skipped")),
                              "crashed": sum(1 for o in outcomes if not o.get("skipped") and not o["ok"])}
    return result


# ---------------------------------------------------------------------------
# Phase 4: dashboard benchmark
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def phase_dashboard(total_requests: int, concurrency: int = 20) -> dict:
    port = free_port()
    log = open(TMP_DIR / "uvicorn.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.web.app:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("dashboard did not start; see " + str(TMP_DIR / "uvicorn.log"))

        endpoints = ["/", "/queue", "/api/posts", "/api/stats", "/calendar?year=2026&month=6", "/logs"]
        plan = [endpoints[i % len(endpoints)] for i in range(total_requests)]
        results = {e: {"lat": [], "errors": 0, "bytes": 0} for e in endpoints}
        res_lock = threading.Lock()

        def hit(path):
            with httpx.Client(base_url=base, timeout=60) as c:
                t0 = time.perf_counter()
                try:
                    r = c.get(path)
                    ok = r.status_code == 200
                    size = len(r.content)
                except httpx.HTTPError:
                    ok, size = False, 0
                dt = time.perf_counter() - t0
            with res_lock:
                results[path]["lat"].append(dt)
                results[path]["bytes"] = size
                if not ok:
                    results[path]["errors"] += 1

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(hit, plan))
        wall = time.perf_counter() - t0

        server_peak_mb = None
        try:
            for line in open(f"/proc/{proc.pid}/status"):
                if line.startswith("VmHWM:"):
                    server_peak_mb = int(line.split()[1]) / 1024
        except OSError:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    per = []
    for e in endpoints:
        lat = results[e]["lat"]
        per.append({"endpoint": e, "n": len(lat), "p50": pct(lat, 50), "p95": pct(lat, 95),
                    "p99": pct(lat, 99), "max": max(lat) if lat else 0, "errors": results[e]["errors"],
                    "kb": results[e]["bytes"] / 1024})
    return {"total": total_requests, "concurrency": concurrency, "seconds": wall,
            "rps": total_requests / wall, "per_endpoint": per, "server_peak_mb": server_peak_mb}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(cfg, inflate, qtimes, conc, dash, mem) -> None:
    storm, race, orc = conc["storm"], conc["race"], conc["orchestrator"]
    problems = []
    if storm["locked_errors"] or race["locked_errors"]:
        problems.append(f"{storm['locked_errors'] + race['locked_errors']} \"database is locked\" errors")
    if race["double_publishes"]:
        problems.append(f"{race['double_publishes']} double publish(es)")
    if orc["crashed"]:
        problems.append(f"{orc['crashed']} orchestrator crash(es)")
    slow = [d for d in dash["per_endpoint"] if d["p95"] > 1.0]
    if slow:
        problems.append("slow endpoints: " + ", ".join(d["endpoint"] for d in slow))
    errors = sum(d["errors"] for d in dash["per_endpoint"])
    if errors:
        problems.append(f"{errors} failed HTTP requests")
    verdict = "PASS" if not problems else "FAIL: " + "; ".join(problems)

    lines = [
        "# Stress Test Report",
        "",
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} by `tests/stress_test.py`.",
        "",
        f"**Verdict: {verdict}**",
        "",
        "| Setting | Value |", "|---|---|",
        f"| Mock posts | {inflate['n']:,} |",
        f"| Concurrent workers | {conc['workers']} |",
        f"| Dashboard requests | {dash['total']:,} ({dash['concurrency']} in parallel) |",
        f"| Python | {platform.python_version()} on {platform.system()} {platform.machine()} |",
        f"| SQLite | {sqlite3.sqlite_version} |",
        f"| Database | temporary file, {inflate['db_mb']:.1f} MB after inflation |",
        "",
        "## 1. Database inflation",
        "",
        f"Inserted {inflate['n']:,} posts in {inflate['seconds']:.2f} s "
        f"({inflate['rows_per_sec']:,.0f} rows/s), one transaction.",
        "",
        "| Status | Rows |", "|---|---|",
        *[f"| {k} | {v:,} |" for k, v in inflate["counts"].items()],
        "",
        "## 2. Query timings (against the inflated database)",
        "",
        "| Query | Rows returned | Mean | p95 | Max |", "|---|---|---|---|---|",
        *[f"| `{q['name']}` | {q['rows']:,} | {ms(q['mean'])} | {ms(q['p95'])} | {ms(q['max'])} |"
          for q in qtimes],
        "",
        "## 3. Concurrency and race conditions",
        "",
        f"**Read/write storm.** {conc['workers']} threads ran {storm['ops']:,} mixed operations "
        f"(stats, list, edit caption, approve/un-approve, clear error) in {storm['seconds']:.2f} s.",
        "",
        "| Check | Result |", "|---|---|",
        f"| \"database is locked\" errors | {storm['locked_errors']} |",
        f"| Other errors | {storm['other_errors']} {('(' + storm['sample_error'][:80] + ')') if storm['other_errors'] else ''} |",
        "",
        f"**Publisher race.** {conc['workers']} publisher workers fetched the same {race['posts']} approved "
        f"posts at the same moment and tried to publish them to a fake Facebook API "
        f"({race['seconds']:.2f} s).",
        "",
        "| Check | Result |", "|---|---|",
        f"| Posts published | {race['published']} of {race['posts']} |",
        f"| API calls made | {race['api_calls']} (ideal: {race['posts']}) |",
        f"| Double publishes | {race['double_publishes']} |",
        f"| \"database is locked\" errors | {race['locked_errors']} |",
        "",
        f"**Overlapping orchestrator runs.** {orc['attempted']} threads called `run_pipeline()` at once.",
        "",
        "| Check | Result |", "|---|---|",
        f"| Ran | {orc['ran']} (expected 1: the lock lets one through) |",
        f"| Skipped cleanly | {orc['skipped']} |",
        f"| Crashed | {orc['crashed']} |",
        "",
        "## 4. Dashboard benchmark",
        "",
        f"{dash['total']:,} requests, {dash['concurrency']} at a time, against a real uvicorn server "
        f"with the inflated database: {dash['seconds']:.2f} s total, **{dash['rps']:.0f} requests/s**.",
        "",
        "| Endpoint | Requests | p50 | p95 | p99 | Max | Size | Errors |",
        "|---|---|---|---|---|---|---|---|",
        *[f"| `{d['endpoint']}` | {d['n']} | {ms(d['p50'])} | {ms(d['p95'])} | {ms(d['p99'])} | "
          f"{ms(d['max'])} | {d['kb']:.0f} KB | {d['errors']} |" for d in dash["per_endpoint"]],
        "",
        "## 5. Memory",
        "",
        "| Measure | Value |", "|---|---|",
        f"| Stress-test process peak RSS | {mem['rss_peak_mb']:.0f} MB |",
        f"| Python heap peak (tracemalloc) | {mem['tracemalloc_peak_mb']:.1f} MB |",
        f"| Dashboard server peak RSS | {dash['server_peak_mb']:.0f} MB |" if dash["server_peak_mb"] else
        "| Dashboard server peak RSS | n/a |",
        "",
        "## 6. What the stress test changed in the code",
        "",
        "Findings from the first run, and the fixes now in place:",
        "",
        "- **SQLite locking.** Concurrent writers raised `database is locked`. Fixed by enabling WAL "
        "journal mode (readers no longer block writers) and a 30 s busy timeout on every connection "
        "(`config/db.py`).",
        "- **Double publishing.** Two publisher processes could pick the same approved post before "
        "either saved a platform ID. Fixed with an atomic claim (`claimed_at` column): a worker only "
        "publishes a post it managed to claim, and stale claims expire after 10 minutes "
        "(`src/publisher.py`).",
        "- **Approval queue page size.** The queue rendered every draft on one page. With thousands of "
        "drafts that was multi-megabyte HTML. Fixed with paging, 25 cards per section (`src/web/queries.py`).",
        "- **Indexes.** Added indexes on `status`, `scheduled_time` and `last_error` so the dashboard "
        "counts and the publisher's due-post query stay fast as the table grows.",
        "",
        "## 7. Honest reading of these numbers",
        "",
        "- This machine, SQLite and a single uvicorn worker are the whole stack. The numbers show the "
        "tool is comfortable far beyond four clients, not that it is a multi-user web service.",
        "- The benchmark hits a server on the same machine. Real page loads add browser rendering and "
        "the Tailwind CDN download.",
        "- No real API was called. Meta, TikTok and OpenAI latency and rate limits will dominate a real "
        "run and are not measured here.",
    ]
    REPORT.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--posts", type=int, default=10_000)
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--ops", type=int, default=40, help="operations per worker in the storm")
    ap.add_argument("--requests", type=int, default=1_000)
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args(argv)

    tracemalloc.start()
    print(f"Temporary database: {DB_PATH}")

    print(f"[1/5] Inflating database with {args.posts:,} posts ...")
    inflate = phase_inflate(args.posts)
    print(f"      {inflate['seconds']:.2f}s, {inflate['db_mb']:.1f} MB")

    print("[2/5] Timing queries ...")
    qtimes = phase_queries()
    for q in qtimes:
        print(f"      {q['name']:<34} mean {ms(q['mean']):>9}  max {ms(q['max']):>9}")

    print(f"[3/5] Concurrency: {args.workers} workers ...")
    conc = phase_concurrency(args.workers, args.ops)
    print(f"      storm: {conc['storm']['locked_errors']} locked errors, "
          f"{conc['storm']['other_errors']} other")
    print(f"      race:  {conc['race']['double_publishes']} double publishes, "
          f"{conc['race']['locked_errors']} locked errors, "
          f"{conc['race']['published']}/{conc['race']['posts']} published")
    print(f"      orchestrator: ran {conc['orchestrator']['ran']}, skipped {conc['orchestrator']['skipped']}, "
          f"crashed {conc['orchestrator']['crashed']}")

    print(f"[4/5] Dashboard benchmark: {args.requests:,} requests ...")
    dash = phase_dashboard(args.requests, args.concurrency)
    for d in dash["per_endpoint"]:
        print(f"      {d['endpoint']:<28} p50 {ms(d['p50']):>9}  p95 {ms(d['p95']):>9}  "
              f"max {ms(d['max']):>9}  {d['kb']:.0f} KB  errors {d['errors']}")
    print(f"      {dash['rps']:.0f} req/s")

    print("[5/5] Memory ...")
    _, peak = tracemalloc.get_traced_memory()
    mem = {"rss_peak_mb": rss_mb(), "tracemalloc_peak_mb": peak / 1024 / 1024}
    print(f"      peak RSS {mem['rss_peak_mb']:.0f} MB, python heap peak {mem['tracemalloc_peak_mb']:.1f} MB")

    write_report(vars(args), inflate, qtimes, conc, dash, mem)
    print(f"\nReport written to {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
