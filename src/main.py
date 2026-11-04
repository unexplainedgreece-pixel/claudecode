"""
Central pipeline orchestrator.

Usage (from the project root):
    python3 -m src.main run                      # images -> upload -> publish, once
    python3 -m src.main run --topic "Summer menu" --topic "New hours"
                                                 # generate drafts first, then the rest
    python3 -m src.main run --steps images,upload # only some steps
    python3 -m src.main run --dry-run            # every step in dry-run mode
    python3 -m src.main loop                     # run every 5 minutes until Ctrl+C
    python3 -m src.main loop --interval 10       # every 10 minutes
    python3 -m src.main generate "Topic here"    # just create one draft
    python3 -m src.main web                      # start the dashboard on http://127.0.0.1:8000
    python3 -m src.main keygen                   # print a new APP_SECRET_KEY

Steps, in order:
    generate  -> src.generator              (only when --topic is given)
    ops       -> src.ops                    (daily backup + Page token health check)
    recycle   -> src.recycle                (evergreen posts back into the queue)
    plan      -> src.library                (today's posts from the media library, once a day)
    images    -> src.image_generator        (drafts missing an image)
    audio     -> src.video.audio_generator  (video posts: caption -> speech MP3)
    video     -> src.video.video_compiler   (video posts: image + speech -> vertical MP4)
    subtitles -> src.subtitles              (burn auto-captions into your own clips, if enabled)
    upload    -> src.uploader               (image posts: JPEG; video posts: MP4) to hosting
    publish   -> src.publisher              (approved + due scheduled posts only)
    metrics   -> src.analytics              (likes, comments, reach for published posts)

Image posts skip the audio and video steps; video posts skip the JPEG path
in the uploader and have their MP4 hosted instead.

Every run is recorded in the pipeline_runs table with its full log, so the
dashboard's Logs page can show what happened, even for background runs.
"""

import argparse
import io
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime

from config.db import get_connection, init_db
from src import analytics, generator, image_generator, library, notify, ops, publisher, recycle, subtitles, uploader
from src.security.secrets import ConfigError
from src.video import audio_generator, video_compiler

log = logging.getLogger("main")

STEP_ORDER = ("generate", "ops", "recycle", "plan", "images", "audio", "video", "subtitles", "upload", "publish", "metrics")
# Steps the background loop runs. Generating needs a topic, so it is manual.
# "plan" makes today's posts from the media library for users who switched it on.
# "recycle" re-queues evergreen posts; "metrics" pulls likes/reach for published posts.
LOOP_STEPS = ("ops", "recycle", "plan", "images", "audio", "video", "subtitles", "upload", "publish", "metrics")
from dotenv import load_dotenv  # noqa: E402
load_dotenv()
DEFAULT_INTERVAL_MINUTES = float(os.getenv("WORKER_INTERVAL_MINUTES") or 5)   # .env: WORKER_INTERVAL_MINUTES=15

# Set by the dashboard / loop so two pipelines never run at the same time.
_run_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Running steps
# ---------------------------------------------------------------------------

def _run_generate(topics, platform="instagram", business="", dry_run=False,
                  media_type="image", user_id=None) -> int:
    """Create one draft per topic. Returns 0 if all succeeded."""
    failures = 0
    for topic in topics:
        if dry_run:
            log.info("Would generate draft for topic: %s (dry run)", topic)
            continue
        try:
            post = generator.generate_post(topic, platform=platform, business=business,
                                           user_id=user_id)
            post_id = generator.save_post(post, media_type=media_type, user_id=user_id)
            log.info("%s draft #%s created for topic: %s", media_type.capitalize(), post_id, topic)
        except ConfigError as exc:
            log.error("Generate skipped: %s", exc)
            return 2
        except Exception as exc:  # OpenAI errors, JSON problems, network
            log.error("Generate FAILED for topic '%s': %s", topic, exc)
            failures += 1
    return 1 if failures else 0


def _draft_count(user_id) -> int:
    scope, params = (" AND user_id = ?", (user_id,)) if user_id else ("", ())
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM posts WHERE status = 'draft'{scope}", params).fetchone()["n"]


def run_step(name: str, dry_run: bool = False, topics=(), user_id: int | None = None,
             **gen_opts) -> int:
    """Run one named step and return its exit code (0 = fully successful)."""
    flags = ["--dry-run"] if dry_run else []
    if user_id is not None:
        flags += ["--user", str(user_id)]
    if name == "generate":
        return _run_generate(topics, dry_run=dry_run, user_id=user_id, **gen_opts)
    if name == "plan":
        before = _draft_count(user_id)
        code = library.run_daily(user_id)
        try:
            from src import ai_plan
            code = max(code, ai_plan.run_step(user_id))
        except Exception as exc:  # noqa: BLE001
            log.exception("AI planning crashed: %s", exc)
            code = max(code, 1)
        new = _draft_count(user_id) - before
        if user_id and new > 0:
            notify.drafts_waiting(user_id, new)
        return code
    if name == "ops":
        return ops.run_daily()
    if name == "recycle":
        return recycle.run_step(user_id)
    if name == "metrics":
        try:
            from src import tiktok
            tiktok.check_pending(user_id)
        except Exception as exc:  # noqa: BLE001 - a TikTok hiccup must not stop the metrics step
            log.warning("TikTok status check skipped: %s", exc)
        return analytics.run_step(user_id)
    if name == "subtitles":
        return subtitles.run_step(user_id)
    if name == "images":
        return image_generator.main(flags)
    if name == "audio":
        return audio_generator.main(flags)
    if name == "video":
        return video_compiler.main(flags)
    if name == "upload":
        return uploader.main(flags)
    if name == "publish":
        return publisher.main(flags)
    raise ValueError(f"Unknown step: {name}")


class _LogCapture(logging.Handler):
    """Collects every log line emitted during a run so it can be stored."""

    def __init__(self):
        super().__init__()
        self.buffer = io.StringIO()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record):
        self.buffer.write(self.format(record) + "\n")


def run_pipeline(steps=LOOP_STEPS, topics=(), dry_run: bool = False,
                 platform: str = "instagram", business: str = "",
                 media_type: str = "image", user_id: int | None = None, quiet_if_idle: bool = False) -> dict:
    """
    Run the given steps in pipeline order and record the run in the database.

    Returns a dict: {"id", "ok", "steps": [{"name", "exit_code", "ok"}], "log"}.
    Exit code meaning per step: 0 all good, 1 some items failed, 2 config missing.
    A step failure does not stop later steps: a publish can still succeed for
    posts whose image was hosted earlier.
    """
    if not _run_lock.acquire(blocking=False):
        log.warning("A pipeline run is already in progress. Skipping this one.")
        return {"id": None, "ok": False, "steps": [], "log": "skipped: already running",
                "skipped": True}

    capture = _LogCapture()
    logging.getLogger().addHandler(capture)
    started = datetime.now()
    results = []
    try:
        ordered = [s for s in STEP_ORDER if s in steps]
        if "generate" in ordered and not topics:
            ordered.remove("generate")
        log.info("Pipeline start: %s%s%s", " -> ".join(ordered) or "(no steps)",
                 f" [user {user_id}]" if user_id else " [all users]", " [DRY RUN]" if dry_run else "")
        for name in ordered:
            log.info("--- step: %s ---", name)
            try:
                code = run_step(name, dry_run=dry_run, topics=topics, user_id=user_id,
                                platform=platform, business=business, media_type=media_type)
            except Exception as exc:
                log.exception("Step %s crashed: %s", name, exc)
                code = 3
            results.append({"name": name, "exit_code": code, "ok": code == 0})
        ok = all(r["ok"] for r in results)
        log.info("Pipeline finished: %s", "OK" if ok else "with problems")
    finally:
        logging.getLogger().removeHandler(capture)
        _run_lock.release()

    summary = ", ".join(f"{r['name']}={'ok' if r['ok'] else 'exit ' + str(r['exit_code'])}"
                        for r in results) or "nothing to do"
    log_text = capture.buffer.getvalue()
    if quiet_if_idle and is_idle_run(ok, log_text):
        try:
            ops.set_state("worker_idle", f"user {user_id}" if user_id else "all")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record idle check: %s", exc)
        return {"id": None, "ok": True, "steps": results, "log": log_text, "summary": "nothing to do", "idle": True}
    run_id = record_run(started, datetime.now(), ordered, ok, summary, log_text, user_id)
    return {"id": run_id, "ok": ok, "steps": results, "log": log_text, "summary": summary}


# A background cycle that only looked around (no post touched, no warning) is not
# worth a row in the runs list; the dashboard shows "checked at ..., nothing to do".
ACTIVITY_MARKERS = ("WARNING", "ERROR", "Post #", "post #", "hosted at", "marked as published", "created",
                    "backup", "token", "recycled", "planned", "Analyzing", "saved ")


def is_idle_run(ok: bool, log_text: str) -> bool:
    if not ok:
        return False
    import re
    body = "\n".join(line for line in log_text.splitlines()
                     if "Pipeline start" not in line and "Pipeline finished" not in line and "--- step:" not in line)
    if any(marker in body for marker in ACTIVITY_MARKERS):
        return False
    if re.search(r"Metrics refreshed: [1-9]", body):
        return False
    return True


def record_run(started, finished, steps, ok, summary, log_text, user_id=None) -> int:
    init_db()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (started_at, finished_at, steps, ok, summary, log, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (started.strftime("%Y-%m-%d %H:%M:%S"), finished.strftime("%Y-%m-%d %H:%M:%S"),
             ",".join(steps), int(ok), summary, log_text, user_id),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

def run_loop(interval_minutes: float = DEFAULT_INTERVAL_MINUTES, steps=LOOP_STEPS,
             max_runs: int | None = None, dry_run: bool = False) -> int:
    """
    Run the pipeline every `interval_minutes` until Ctrl+C / SIGTERM.
    `max_runs` stops after that many runs (used by tests).
    """
    stop = threading.Event()

    def _handle_signal(signum, frame):
        log.info("Stop requested, finishing current run...")
        stop.set()

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _handle_signal)
        except ValueError:
            pass  # not in main thread (e.g. started from the dashboard)

    runs = 0
    try:
        while not stop.is_set():
            runs += 1
            log.info("=== Loop run %d (every %s min) ===", runs, interval_minutes)
            try:
                ops.heartbeat("loop")
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat failed: %s", exc)
            try:
                # One run per user, so each user's Logs page shows their own runs.
                from src.web.auth import list_user_ids
                user_ids = list_user_ids() or [None]
                for uid in user_ids:
                    run_pipeline(steps=steps, dry_run=dry_run, user_id=uid, quiet_if_idle=True)
            except Exception as exc:  # never let the loop die
                log.exception("Loop run crashed: %s", exc)
            if max_runs is not None and runs >= max_runs:
                break
            stop.wait(interval_minutes * 60)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    log.info("Loop stopped after %d run(s).", runs)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Social media pipeline orchestrator.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the pipeline once")
    p_run.add_argument("--topic", action="append", default=[], help="Generate a draft for this topic (repeatable)")
    p_run.add_argument("--steps", default=",".join(STEP_ORDER), help="Comma-separated subset of: " + ",".join(STEP_ORDER))
    p_run.add_argument("--platform", default="instagram")
    p_run.add_argument("--business", default="")
    p_run.add_argument("--media-type", default="image", choices=("image", "video"),
                       help="For --topic drafts: image post or TikTok/Reel video (default: image)")
    p_run.add_argument("--user", type=int, default=None, help="Only this user's posts")
    p_run.add_argument("--dry-run", action="store_true")

    p_loop = sub.add_parser("loop", help="Run images/upload/publish every N minutes")
    p_loop.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_MINUTES, help="Minutes between runs")
    p_loop.add_argument("--max-runs", type=int, default=None)
    p_loop.add_argument("--dry-run", action="store_true")

    p_gen = sub.add_parser("generate", help="Create one draft post")
    p_gen.add_argument("topic")
    p_gen.add_argument("--platform", default="instagram")
    p_gen.add_argument("--business", default="")
    p_gen.add_argument("--media-type", default="image", choices=("image", "video"))
    p_gen.add_argument("--user", type=int, default=None, help="Owner of the new draft")

    p_web = sub.add_parser("web", help="Start the dashboard")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)
    p_web.add_argument("--reload", action="store_true")

    sub.add_parser("keygen", help="Print a new APP_SECRET_KEY for .env")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    init_db()

    if args.command == "run":
        steps = [s.strip() for s in args.steps.split(",") if s.strip()]
        bad = [s for s in steps if s not in STEP_ORDER]
        if bad:
            log.error("Unknown step(s): %s", ", ".join(bad))
            return 2
        result = run_pipeline(steps=steps, topics=args.topic, dry_run=args.dry_run,
                              platform=args.platform, business=args.business,
                              media_type=args.media_type, user_id=args.user)
        return 0 if result["ok"] else 1

    if args.command == "loop":
        return run_loop(args.interval, max_runs=args.max_runs, dry_run=args.dry_run)

    if args.command == "generate":
        result = run_pipeline(steps=["generate"], topics=[args.topic],
                              platform=args.platform, business=args.business,
                              media_type=args.media_type, user_id=args.user)
        return 0 if result["ok"] else 1

    if args.command == "web":
        import uvicorn
        log.info("Dashboard: http://%s:%s", args.host, args.port)
        uvicorn.run("src.web.app:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "keygen":
        from src.security import crypto
        return crypto.main(["keygen"])

    return 2


if __name__ == "__main__":
    sys.exit(main())
