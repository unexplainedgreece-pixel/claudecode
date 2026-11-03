"""
Social automation dashboard (FastAPI + Jinja2 + Tailwind), multi-user.

Start it with:
    python3 -m src.main web            # http://127.0.0.1:8000
or
    uvicorn src.web.app:app --reload

Every page and API route needs a logged-in user except /login, /signup,
/api/auth/token, /api/billing/webhook, /api/health and /static. A user only
ever sees their own posts, runs, credentials and media files.

Pages:   /  /queue  /calendar  /logs  /posts/{id}  /settings  /profile  /billing
JSON:    /api/stats  /api/posts  /api/posts/{id}  /api/errors  /api/runs  /api/auth/me
Upload:  POST /api/posts/upload   (multipart: file, topic, caption, hashtags)
Files:   /files/{post_id}/{image|video|audio}   (ownership checked)
"""

import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config.db import DATA_DIR, MEDIA_TYPES, POST_STATUSES, init_db
from src import main as pipeline
from src import publisher
from src.billing import limits, stripe_integration as billing
from src.billing.tiers import TIER_ORDER, TIERS, tier_info
from src.security import crypto, secrets
from src.web import auth, queries
from src.web.auth import LoginRedirect, current_user

log = logging.getLogger("web")
HERE = Path(__file__).resolve().parent
UPLOAD_DIR = DATA_DIR / "uploaded_videos"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
UPLOAD_EXTENSIONS = {".mp4", ".mov", ".webm"}
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") in ("1", "true", "yes")

app = FastAPI(title="Social Automation Dashboard", docs_url="/api/docs")
templates = Jinja2Templates(directory=str(HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.on_event("startup")
def _startup():
    init_db()
    for folder in (DATA_DIR / "images", DATA_DIR / "videos", DATA_DIR / "audio", UPLOAD_DIR):
        folder.mkdir(parents=True, exist_ok=True)


@app.exception_handler(LoginRedirect)
async def _login_redirect(request: Request, exc: LoginRedirect):
    return auth.login_redirect_response(exc.next_path)


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


def _local_exists(post: dict, *keys) -> bool:
    return any(post.get(k) and Path(post[k]).exists() for k in keys)


def preview_path(post: dict) -> str | None:
    if _local_exists(post, "jpeg_path", "image_path"):
        return f"/files/{post['id']}/image"
    return post.get("image_url") or None


def video_src(post: dict) -> str | None:
    if _local_exists(post, "video_path"):
        return f"/files/{post['id']}/video"
    return post.get("video_url") or None


def audio_src(post: dict) -> str | None:
    return f"/files/{post['id']}/audio" if _local_exists(post, "audio_path") else None


templates.env.globals.update(
    status_style=lambda s: STATUS_STYLES.get(s, "bg-slate-700 text-slate-200"),
    preview_path=preview_path, video_src=video_src, audio_src=audio_src,
    statuses=POST_STATUSES, media_types=MEDIA_TYPES, tiers=TIERS, tier_order=TIER_ORDER,
    tier_info=tier_info, now=lambda: datetime.now(),
    all_platforms=publisher.ALL_PLATFORMS, parse_platforms=publisher.parse_platforms,
    platform_labels={"facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok"},
)


def render(request: Request, name: str, user: dict | None = None, **ctx) -> HTMLResponse:
    ctx.update(request=request, msg=request.query_params.get("msg"),
               err=request.query_params.get("err"), active=name.split(".")[0], user=user)
    if user:
        ctx.setdefault("plan", tier_info(user.get("tier")))
        ctx.setdefault("configured", publisher.configured_platforms(user["id"]))
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, msg: str | None = None, err: str | None = None,
         default: str = "/") -> RedirectResponse:
    target = (request.headers.get("referer") or default).split("?")[0]
    if msg:
        target += "?msg=" + quote(msg)
    elif err:
        target += "?err=" + quote(err)
    return RedirectResponse(target, status_code=303)


def post_or_404(post_id: int, user: dict) -> dict:
    """The post if it belongs to this user, else 404 (never reveal other users' ids)."""
    post = queries.get_post(post_id, user_id=user["id"])
    if not post:
        raise HTTPException(404, f"Post {post_id} not found")
    return post


# ---------------------------------------------------------------------------
# Auth pages and API
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if auth.optional_user(request, None):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", next=next)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    try:
        auth.check_login_rate(request.client.host if request.client else "?")
        user = auth.authenticate(email, password)
    except auth.AuthError as exc:
        return RedirectResponse(f"/login?err={quote(str(exc))}&next={quote(next)}", status_code=303)
    response = auth.login_redirect_response(next)
    response.headers["location"] = next if next.startswith("/") and not next.startswith("//") else "/"
    auth.set_session_cookie(response, auth.create_token(user["id"]), COOKIE_SECURE)
    return response


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return render(request, "signup.html")


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...),
           password2: str = Form(""), display_name: str = Form("")):
    if password != password2:
        return RedirectResponse("/signup?err=" + quote("Passwords do not match."), status_code=303)
    try:
        user = auth.create_user(email, password, display_name)
    except auth.AuthError as exc:
        return RedirectResponse("/signup?err=" + quote(str(exc)), status_code=303)
    response = RedirectResponse("/?msg=" + quote("Welcome! Your account is ready."), status_code=303)
    auth.set_session_cookie(response, auth.create_token(user["id"]), COOKIE_SECURE)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login?msg=" + quote("Logged out."), status_code=303)
    auth.clear_session_cookie(response)
    return response


@app.post("/api/auth/token")
def api_token(form: OAuth2PasswordRequestForm = Depends()):
    try:
        user = auth.authenticate(form.username, form.password)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"})
    return {"access_token": auth.create_token(user["id"]), "token_type": "bearer",
            "expires_in": auth.JWT_EXPIRE_MINUTES * 60}


@app.get("/api/auth/me")
def api_me(user: dict = Depends(current_user)):
    return {**user, "usage": limits.usage(user["id"]), "plan": tier_info(user["tier"])}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(current_user)):
    uid = user["id"]
    return render(request, "dashboard.html", user, stats=queries.stats(uid),
                  recent=queries.list_posts(limit=8, user_id=uid), runs=queries.list_runs(5, uid),
                  errors=queries.list_errors(5, uid), usage=limits.usage(uid),
                  remaining=limits.remaining_posts(user), max_upload_mb=MAX_UPLOAD_MB)


@app.get("/queue", response_class=HTMLResponse)
def approval_queue(request: Request, page: int = 1, user: dict = Depends(current_user)):
    return render(request, "queue.html", user, **queries.queue(page=page, user_id=user["id"]))


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, year: int | None = None, month: int | None = None,
                  user: dict = Depends(current_user)):
    today = datetime.now()
    year, month = year or today.year, month or today.month
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be 1-12")
    return render(request, "calendar.html", user, cal=queries.calendar_month(year, month, user["id"]))


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, user: dict = Depends(current_user)):
    uid = user["id"]
    return render(request, "logs.html", user, errors=queries.list_errors(user_id=uid),
                  runs=queries.list_runs(user_id=uid), stats=queries.stats(uid))


@app.get("/logs/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, user: dict = Depends(current_user)):
    run = queries.get_run(run_id, user_id=user["id"])
    if not run:
        raise HTTPException(404, "Run not found")
    return render(request, "run.html", user, run=run)


@app.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int, user: dict = Depends(current_user)):
    return render(request, "post.html", user, post=post_or_404(post_id, user))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: dict = Depends(current_user)):
    try:
        crypto.load_key()
        key_ok, key_error = True, None
    except crypto.CryptoError as exc:
        key_ok, key_error = False, str(exc)
    return render(request, "settings.html", user, secrets=secrets.describe_secrets(user["id"]),
                  key_ok=key_ok, key_error=key_error)


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: dict = Depends(current_user)):
    return render(request, "profile.html", user, usage=limits.usage(user["id"]))


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, user: dict = Depends(current_user)):
    return render(request, "billing.html", user, usage=limits.usage(user["id"]),
                  stripe_ready=billing.is_configured())


# ---------------------------------------------------------------------------
# Media files (ownership checked, never served from a public static mount)
# ---------------------------------------------------------------------------

@app.get("/files/{post_id}/{kind}")
def post_file(post_id: int, kind: str, user: dict = Depends(current_user)):
    post = post_or_404(post_id, user)
    keys = {"image": ("jpeg_path", "image_path"), "video": ("video_path",), "audio": ("audio_path",)}
    if kind not in keys:
        raise HTTPException(404, "Unknown file kind")
    for key in keys[kind]:
        path = post.get(key)
        if path and Path(path).exists():
            return FileResponse(path)
    raise HTTPException(404, "File not found")


# ---------------------------------------------------------------------------
# Post actions (HTML forms)
# ---------------------------------------------------------------------------

@app.post("/posts/new")
def new_post(request: Request, background: BackgroundTasks, topic: str = Form(...),
             caption: str = Form(""), hashtags: str = Form(""), image_prompt: str = Form(""),
             platform: str = Form("instagram"), business: str = Form(""),
             media_type: str = Form("image"), platforms: list[str] = Form([]),
             user: dict = Depends(current_user)):
    topic = topic.strip()
    if not topic:
        return back(request, err="Topic is required.")
    if media_type not in MEDIA_TYPES:
        return back(request, err="Media type must be image or video.")
    try:
        limits.enforce(user, "reel" if media_type == "video" else "create_post")
    except limits.LimitError as exc:
        return back(request, err=str(exc))
    if caption.strip():
        pid = queries.create_draft(topic, caption, hashtags, image_prompt, media_type=media_type,
                                   user_id=user["id"], platforms=platforms)
        return RedirectResponse(f"/posts/{pid}?msg=Draft+created", status_code=303)
    background.add_task(pipeline.run_pipeline, steps=["generate"], topics=[topic],
                        platform=platform, business=business, media_type=media_type,
                        user_id=user["id"])
    return back(request, msg=f"Generating a draft for '{topic}' in the background. "
                             "Refresh in a few seconds.", default="/queue")


@app.post("/posts/{post_id}/approve")
def approve(request: Request, post_id: int, scheduled_time: str = Form(""),
            platforms: list[str] = Form([]), user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    chosen = publisher.parse_platforms(platforms)
    if not chosen:
        return back(request, err="Tick at least one platform to post to.")
    try:
        queries.set_status(post_id, "approved", scheduled_time, platforms=chosen)
    except ValueError as exc:
        return back(request, err=f"Could not approve: {exc}")
    post = queries.get_post(post_id)
    when = f"for {post['scheduled_time']}" if post["status"] == "scheduled" else "for the next run"
    return back(request, msg=f"Post #{post_id} approved {when}: {', '.join(chosen)}.", default="/queue")


@app.post("/posts/{post_id}/reject")
def reject(request: Request, post_id: int, user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    queries.set_status(post_id, "rejected")
    return back(request, msg=f"Post #{post_id} rejected.", default="/queue")


@app.post("/posts/{post_id}/unapprove")
def unapprove(request: Request, post_id: int, user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    queries.set_status(post_id, "draft")
    return back(request, msg=f"Post #{post_id} moved back to drafts.", default="/queue")


@app.post("/posts/{post_id}/edit")
def edit(request: Request, post_id: int, topic: str = Form(""), caption: str = Form(""),
         hashtags: str = Form(""), image_prompt: str = Form(""), scheduled_time: str = Form(""),
         image_url: str = Form(""), video_url: str = Form(""), media_type: str = Form("image"),
         platforms: list[str] = Form([]), user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    try:
        queries.update_post(post_id, topic=topic, caption=caption, hashtags=hashtags,
                            image_prompt=image_prompt, scheduled_time=scheduled_time,
                            image_url=image_url, video_url=video_url, media_type=media_type,
                            platforms=platforms)
    except ValueError as exc:
        return back(request, err=f"Not saved: {exc}" if "media_type" in str(exc)
                    else "Scheduled time must look like 2026-11-20 09:30.")
    return back(request, msg="Saved.", default=f"/posts/{post_id}")


@app.post("/posts/{post_id}/clear-error")
def clear_error(request: Request, post_id: int, user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    queries.clear_error(post_id)
    return back(request, msg=f"Error cleared on post #{post_id}.", default="/logs")


@app.post("/posts/{post_id}/delete")
def delete(request: Request, post_id: int, user: dict = Depends(current_user)):
    post = post_or_404(post_id, user)
    if post["status"] == "published":
        return back(request, err="Published posts are kept for the record. Not deleted.")
    queries.delete_post(post_id)
    return RedirectResponse(f"/queue?msg=Post+%23{post_id}+deleted", status_code=303)


# ---------------------------------------------------------------------------
# Custom video upload
# ---------------------------------------------------------------------------

def _probe_is_video(path: Path) -> bool:
    """True if ffprobe finds a video stream. True (unchecked) when ffprobe is missing."""
    if not shutil.which("ffprobe"):
        return True
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30)
        return out.returncode == 0 and "video" in out.stdout
    except (subprocess.SubprocessError, OSError):
        return False


@app.post("/api/posts/upload")
async def upload_video(file: UploadFile = File(...), topic: str = Form(""), caption: str = Form(""),
                       hashtags: str = Form(""), platforms: list[str] = Form([]),
                       user: dict = Depends(limits.require_feature("upload"))):
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in UPLOAD_EXTENSIONS:
        raise HTTPException(400, f"Only {', '.join(sorted(UPLOAD_EXTENSIONS))} files are accepted.")
    if file.content_type and not file.content_type.startswith(("video/", "application/octet-stream")):
        raise HTTPException(400, f"Unexpected content type {file.content_type}.")

    folder = UPLOAD_DIR / f"user_{user['id']}"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{uuid.uuid4().hex}{ext}"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB.")
                out.write(chunk)
        if written == 0:
            raise HTTPException(400, "The file is empty.")
        if not _probe_is_video(dest):
            raise HTTPException(400, "That file does not contain a video stream.")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    pid = queries.create_draft(topic.strip() or Path(name).stem, caption, hashtags, media_type="video",
                               user_id=user["id"], source="upload", video_path=str(dest), upload_name=name,
                               platforms=platforms)
    log.info("User %s uploaded %s (%d KB) as post #%s", user["id"], name, written // 1024, pid)
    return {"id": pid, "url": f"/posts/{pid}", "bytes": written, "filename": name}


# ---------------------------------------------------------------------------
# Pipeline, settings, profile, billing actions
# ---------------------------------------------------------------------------

@app.post("/pipeline/run")
def pipeline_run(request: Request, background: BackgroundTasks,
                 steps: str = Form("images,audio,video,upload,publish"), dry_run: str = Form(""),
                 user: dict = Depends(current_user)):
    wanted = [s.strip() for s in steps.split(",") if s.strip()]
    bad = [s for s in wanted if s not in pipeline.STEP_ORDER]
    if bad:
        return back(request, err=f"Unknown step(s): {', '.join(bad)}")
    background.add_task(pipeline.run_pipeline, steps=wanted, dry_run=bool(dry_run), user_id=user["id"])
    return RedirectResponse("/logs?msg=" + quote(f"Pipeline started: {', '.join(wanted)}. "
                                                 "Refresh to see the run."), status_code=303)


@app.post("/settings")
def settings_save(request: Request, name: str = Form(...), value: str = Form(...),
                  user: dict = Depends(current_user)):
    if name not in secrets.KNOWN_SECRETS:
        return back(request, err="Unknown setting.")
    try:
        secrets.set_secret(name, value, user_id=user["id"])
    except (crypto.CryptoError, ValueError) as exc:
        return back(request, err=str(exc))
    return back(request, msg=f"{name} saved (encrypted).", default="/settings")


@app.post("/settings/delete")
def settings_delete(request: Request, name: str = Form(...), user: dict = Depends(current_user)):
    secrets.delete_secret(name, user_id=user["id"])
    return back(request, msg=f"{name} removed from your encrypted store.", default="/settings")


@app.post("/profile")
def profile_save(request: Request, display_name: str = Form(""), user: dict = Depends(current_user)):
    auth.update_profile(user["id"], display_name)
    return back(request, msg="Profile saved.", default="/profile")


@app.post("/profile/password")
def profile_password(request: Request, current: str = Form(...), new: str = Form(...),
                     new2: str = Form(""), user: dict = Depends(current_user)):
    if new != new2:
        return back(request, err="New passwords do not match.")
    try:
        auth.change_password(user["id"], current, new)
    except auth.AuthError as exc:
        return back(request, err=str(exc))
    return back(request, msg="Password changed.", default="/profile")


@app.post("/billing/checkout")
def billing_checkout(request: Request, tier: str = Form(...), user: dict = Depends(current_user)):
    base = str(request.base_url).rstrip("/")
    try:
        url = billing.create_checkout_session(user, tier, f"{base}/billing?msg=" + quote("Thank you! Your plan updates as soon as Stripe confirms."),
                                              f"{base}/billing?err=" + quote("Checkout cancelled."))
    except billing.BillingError as exc:
        return back(request, err=str(exc), default="/billing")
    return RedirectResponse(url, status_code=303)


@app.post("/billing/portal")
def billing_portal(request: Request, user: dict = Depends(current_user)):
    try:
        url = billing.create_portal_session(user, str(request.base_url).rstrip("/") + "/billing")
    except billing.BillingError as exc:
        return back(request, err=str(exc), default="/billing")
    return RedirectResponse(url, status_code=303)


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    payload = await request.body()
    try:
        result = billing.handle_webhook(payload, request.headers.get("stripe-signature", ""))
    except billing.BillingError as exc:
        raise HTTPException(400, str(exc))
    return result


@app.get("/api/billing/usage")
def api_usage(user: dict = Depends(current_user)):
    return {"tier": user["tier"], "plan": tier_info(user["tier"]), "usage": limits.usage(user["id"]),
            "remaining_posts": limits.remaining_posts(user)}


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def api_stats(user: dict = Depends(current_user)):
    return queries.stats(user["id"])


@app.get("/api/posts")
def api_posts(status: str | None = None, limit: int = 100, q: str | None = None,
              user: dict = Depends(current_user)):
    if status and status not in POST_STATUSES:
        raise HTTPException(400, f"status must be one of {POST_STATUSES}")
    return queries.list_posts(status=status, limit=min(limit, 500), search=q, user_id=user["id"])


@app.get("/api/posts/{post_id}")
def api_post(post_id: int, user: dict = Depends(current_user)):
    return post_or_404(post_id, user)


@app.post("/api/posts/{post_id}/status")
def api_set_status(post_id: int, payload: dict, user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    try:
        queries.set_status(post_id, payload.get("status", ""), payload.get("scheduled_time"),
                           platforms=payload.get("platforms"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return queries.get_post(post_id)


@app.get("/api/errors")
def api_errors(limit: int = 100, user: dict = Depends(current_user)):
    return queries.list_errors(limit, user["id"])


@app.get("/api/runs")
def api_runs(limit: int = 50, user: dict = Depends(current_user)):
    return queries.list_runs(limit, user["id"])


@app.get("/api/runs/{run_id}")
def api_run(run_id: int, user: dict = Depends(current_user)):
    run = queries.get_run(run_id, user["id"])
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/health")
def api_health():
    return JSONResponse({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
