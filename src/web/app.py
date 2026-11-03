"""
Social automation dashboard (FastAPI + Jinja2 + Tailwind).

Start it with:
    python3 -m src.main web            # http://127.0.0.1:8000
or
    uvicorn src.web.app:app --reload

Pages:   /  /queue  /calendar  /logs  /posts/{id}  /settings
JSON:    /api/stats  /api/posts  /api/posts/{id}  /api/errors  /api/runs  /api/runs/{id}
Actions: POST forms under /posts/{id}/..., /pipeline/run, /settings

This dashboard has NO login. It binds to 127.0.0.1 by default so only this
machine can reach it. Do not expose it to the internet as it is.
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config.db import DATA_DIR, POST_STATUSES, init_db
from src import main as pipeline
from src.security import crypto, secrets
from src.web import queries

HERE = Path(__file__).resolve().parent
IMAGES_DIR = DATA_DIR / "images"

app = FastAPI(title="Social Automation Dashboard", docs_url="/api/docs")
templates = Jinja2Templates(directory=str(HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.on_event("startup")
def _startup():
    init_db()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(IMAGES_DIR)), name="media")


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

STATUS_STYLES = {
    "draft": "bg-slate-700 text-slate-200",
    "approved": "bg-emerald-600/30 text-emerald-300 ring-1 ring-emerald-500/40",
    "scheduled": "bg-sky-600/30 text-sky-300 ring-1 ring-sky-500/40",
    "published": "bg-violet-600/30 text-violet-300 ring-1 ring-violet-500/40",
    "rejected": "bg-rose-600/30 text-rose-300 ring-1 ring-rose-500/40",
}


def preview_path(post: dict) -> str | None:
    """URL for the local image preview, served from data/images."""
    for key in ("jpeg_path", "image_path"):
        p = post.get(key)
        if p and Path(p).exists():
            return f"/media/{Path(p).name}"
    return post.get("image_url") or None


templates.env.globals.update(
    status_style=lambda s: STATUS_STYLES.get(s, "bg-slate-700 text-slate-200"),
    preview_path=preview_path,
    statuses=POST_STATUSES,
    now=lambda: datetime.now(),
)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.update(request=request, msg=request.query_params.get("msg"),
               err=request.query_params.get("err"), active=name.split(".")[0])
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, msg: str | None = None, err: str | None = None,
         default: str = "/") -> RedirectResponse:
    """Redirect to the page the form was on, carrying a flash message."""
    target = request.headers.get("referer") or default
    target = target.split("?")[0]
    if msg:
        target += "?msg=" + quote(msg)
    elif err:
        target += "?err=" + quote(err)
    return RedirectResponse(target, status_code=303)


def post_or_404(post_id: int) -> dict:
    post = queries.get_post(post_id)
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    return post


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", stats=queries.stats(),
                  recent=queries.list_posts(limit=8), runs=queries.list_runs(limit=5),
                  errors=queries.list_errors(limit=5))


@app.get("/queue", response_class=HTMLResponse)
def approval_queue(request: Request, page: int = 1):
    return render(request, "queue.html", **queries.queue(page=page))


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, year: int | None = None, month: int | None = None):
    today = datetime.now()
    year, month = year or today.year, month or today.month
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be 1-12")
    return render(request, "calendar.html", cal=queries.calendar_month(year, month))


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return render(request, "logs.html", errors=queries.list_errors(),
                  runs=queries.list_runs(), stats=queries.stats())


@app.get("/logs/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    run = queries.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return render(request, "run.html", run=run)


@app.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int):
    return render(request, "post.html", post=post_or_404(post_id))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    try:
        crypto.load_key()
        key_ok, key_error = True, None
    except crypto.CryptoError as exc:
        key_ok, key_error = False, str(exc)
    return render(request, "settings.html", secrets=secrets.describe_secrets(),
                  key_ok=key_ok, key_error=key_error)


# ---------------------------------------------------------------------------
# Post actions (HTML forms)
# ---------------------------------------------------------------------------

@app.post("/posts/new")
def new_post(request: Request, background: BackgroundTasks, topic: str = Form(...),
             caption: str = Form(""), hashtags: str = Form(""), image_prompt: str = Form(""),
             platform: str = Form("instagram"), business: str = Form("")):
    topic = topic.strip()
    if not topic:
        return back(request, err="Topic is required.")
    if caption.strip():
        pid = queries.create_draft(topic, caption, hashtags, image_prompt)
        return RedirectResponse(f"/posts/{pid}?msg=Draft+created", status_code=303)
    background.add_task(pipeline.run_pipeline, steps=["generate"], topics=[topic],
                        platform=platform, business=business)
    return back(request, msg=f"Generating a draft for '{topic}' in the background. "
                             "Refresh in a few seconds.", default="/queue")


@app.post("/posts/{post_id}/approve")
def approve(request: Request, post_id: int, scheduled_time: str = Form("")):
    post_or_404(post_id)
    try:
        queries.set_status(post_id, "approved", scheduled_time)
    except ValueError as exc:
        return back(request, err=f"Could not approve: {exc}")
    post = queries.get_post(post_id)
    when = f"for {post['scheduled_time']}" if post["status"] == "scheduled" else "for the next run"
    return back(request, msg=f"Post #{post_id} approved {when}.", default="/queue")


@app.post("/posts/{post_id}/reject")
def reject(request: Request, post_id: int):
    post_or_404(post_id)
    queries.set_status(post_id, "rejected")
    return back(request, msg=f"Post #{post_id} rejected.", default="/queue")


@app.post("/posts/{post_id}/unapprove")
def unapprove(request: Request, post_id: int):
    post_or_404(post_id)
    queries.set_status(post_id, "draft")
    return back(request, msg=f"Post #{post_id} moved back to drafts.", default="/queue")


@app.post("/posts/{post_id}/edit")
def edit(request: Request, post_id: int, topic: str = Form(""), caption: str = Form(""),
         hashtags: str = Form(""), image_prompt: str = Form(""), scheduled_time: str = Form(""),
         image_url: str = Form(""), video_url: str = Form("")):
    post_or_404(post_id)
    try:
        queries.update_post(post_id, topic=topic, caption=caption, hashtags=hashtags,
                            image_prompt=image_prompt, scheduled_time=scheduled_time,
                            image_url=image_url, video_url=video_url)
    except ValueError:
        return back(request, err="Scheduled time must look like 2026-11-20 09:30.")
    return back(request, msg="Saved.", default=f"/posts/{post_id}")


@app.post("/posts/{post_id}/clear-error")
def clear_error(request: Request, post_id: int):
    post_or_404(post_id)
    queries.clear_error(post_id)
    return back(request, msg=f"Error cleared on post #{post_id}.", default="/logs")


@app.post("/posts/{post_id}/delete")
def delete(request: Request, post_id: int):
    post = post_or_404(post_id)
    if post["status"] == "published":
        return back(request, err="Published posts are kept for the record. Not deleted.")
    queries.delete_post(post_id)
    return RedirectResponse(f"/queue?msg=Post+%23{post_id}+deleted", status_code=303)


# ---------------------------------------------------------------------------
# Pipeline and settings actions
# ---------------------------------------------------------------------------

@app.post("/pipeline/run")
def pipeline_run(request: Request, background: BackgroundTasks,
                 steps: str = Form("images,upload,publish"), dry_run: str = Form("")):
    wanted = [s.strip() for s in steps.split(",") if s.strip()]
    bad = [s for s in wanted if s not in pipeline.STEP_ORDER]
    if bad:
        return back(request, err=f"Unknown step(s): {', '.join(bad)}")
    background.add_task(pipeline.run_pipeline, steps=wanted, dry_run=bool(dry_run))
    return RedirectResponse("/logs?msg=" + quote(f"Pipeline started: {', '.join(wanted)}. "
                                                 "Refresh to see the run."), status_code=303)


@app.post("/settings")
def settings_save(request: Request, name: str = Form(...), value: str = Form(...)):
    if name not in secrets.KNOWN_SECRETS:
        return back(request, err="Unknown setting.")
    try:
        secrets.set_secret(name, value)
    except (crypto.CryptoError, ValueError) as exc:
        return back(request, err=str(exc))
    return back(request, msg=f"{name} saved (encrypted).", default="/settings")


@app.post("/settings/delete")
def settings_delete(request: Request, name: str = Form(...)):
    secrets.delete_secret(name)
    return back(request, msg=f"{name} removed from the encrypted store.", default="/settings")


# ---------------------------------------------------------------------------
# JSON API (used by the pages for live refresh, and handy for scripts)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def api_stats():
    return queries.stats()


@app.get("/api/posts")
def api_posts(status: str | None = None, limit: int = 100, q: str | None = None):
    if status and status not in POST_STATUSES:
        raise HTTPException(400, f"status must be one of {POST_STATUSES}")
    return queries.list_posts(status=status, limit=min(limit, 500), search=q)


@app.get("/api/posts/{post_id}")
def api_post(post_id: int):
    return post_or_404(post_id)


@app.post("/api/posts/{post_id}/status")
def api_set_status(post_id: int, payload: dict):
    post_or_404(post_id)
    try:
        queries.set_status(post_id, payload.get("status", ""), payload.get("scheduled_time"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return queries.get_post(post_id)


@app.get("/api/errors")
def api_errors(limit: int = 100):
    return queries.list_errors(limit)


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return queries.list_runs(limit)


@app.get("/api/runs/{run_id}")
def api_run(run_id: int):
    run = queries.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/health")
def api_health():
    return JSONResponse({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
