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
import secrets as pysecrets
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config.db import DATA_DIR, MEDIA_TYPES, POST_STATUSES, init_db
from src import analytics, carousel, ideas, importer, library, ops, review, slots, tiktok, youtube
from src import main as pipeline
from src import pages as pages_store
from src import publisher
from src.ai import providers
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
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
UPLOAD_IMAGE_DIR = DATA_DIR / "images"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") in ("1", "true", "yes")

app = FastAPI(title="Social Automation Dashboard", docs_url="/api/docs")
templates = Jinja2Templates(directory=str(HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

CSRF_COOKIE = "csrf"
CSRF_EXEMPT = ("/api/billing/webhook", "/api/auth/token")
CSRF_ALWAYS = ("/login", "/signup")   # checked even without a session cookie (login CSRF)
CSRF_EXEMPT_PREFIXES = ("/review/",)
CSRF_FORM_MAX_BYTES = 64 * 1024 * 1024   # bigger multipart posts must carry the token in a header or ?csrf=   # the secret token in the URL is the protection; no session needed


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Double-submit cookie: every browser gets a random csrf cookie; state-changing
    requests made with the session cookie must echo it (form field or X-CSRF-Token
    header). Bearer-token API clients and the exempt public routes are not affected.
    Disabled with CSRF_DISABLED=1 (the test suite).
    """

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE)
        request.state.csrf = token or pysecrets.token_urlsafe(24)
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and os.getenv("CSRF_DISABLED", "0") != "1"
                and (request.cookies.get(auth.COOKIE_NAME) or request.url.path in CSRF_ALWAYS)
                and not request.headers.get("authorization")
                and request.url.path not in CSRF_EXEMPT and not request.url.path.startswith(CSRF_EXEMPT_PREFIXES)):
            sent = request.headers.get("x-csrf-token") or request.query_params.get("csrf")
            if not sent:
                ctype = request.headers.get("content-type", "")
                if ctype.startswith("multipart/form-data") and \
                        int(request.headers.get("content-length") or 0) > CSRF_FORM_MAX_BYTES:
                    # Reading the token out of a huge multipart body would buffer it all in RAM.
                    return Response("Upload too large for the plain form. Enable JavaScript (the page then sends the "
                                    "token in the address) or use the drop zone.", status_code=413)
                if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
                    # Read (and cache) the raw body first: Starlette only replays a body to
                    # the route when request.body() was used; request.form() alone would
                    # consume the stream and the route would see an empty form.
                    await request.body()
                    form = await request.form()
                    sent = form.get("csrf")
                    await form.close()        # drop any upload buffers; the route re-parses the cached body
            if not token or sent != token:
                return Response("Request blocked: missing or wrong CSRF token. Reload the page and try again.",
                                status_code=403)
        response = await call_next(request)
        if not token:
            response.set_cookie(CSRF_COOKIE, request.state.csrf, samesite="lax", secure=COOKIE_SECURE, path="/")
        return response


app.add_middleware(CSRFMiddleware)


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
    caption_issues=publisher.caption_issues, carousel_items=carousel.items,
    tiktok_status_text=lambda s: tiktok.STATUS_TEXT.get(s, s or ""),
    creatable_media_types=("image", "video"),
    platform_labels={"facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok", "threads": "Threads",
                     "youtube": "YouTube Shorts"},
)


def render(request: Request, name: str, user: dict | None = None, **ctx) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    ctx.update(request=request, active=name.split(".")[0], user=user,
               csrf=getattr(request.state, "csrf", None) or request.cookies.get(CSRF_COOKIE, ""))
    if user:
        ctx.setdefault("plan", tier_info(user.get("tier")))
        ctx.setdefault("configured", publisher.configured_platforms(user["id"]))
        ctx.setdefault("fb_pages", pages_store.list_pages(user["id"]))
        ctx.setdefault("worker", ops.worker_status())
        ctx.setdefault("expiring_pages", ops.expiring_pages(user["id"]))
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


@app.get("/posts", response_class=HTMLResponse)
def posts_list(request: Request, status: str = "all", q: str = "", page: int = 1,
               user: dict = Depends(current_user)):
    if status != "all" and status not in POST_STATUSES:
        status = "all"
    return render(request, "posts.html", user,
                  **queries.posts_page(user["id"], status=status, search=q.strip(), page=page))


@app.get("/posts/import/template.csv")
def import_template(user: dict = Depends(current_user)):
    return PlainTextResponse(importer.template_csv(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="posts-template.csv"'})


@app.post("/posts/import")
async def import_posts(request: Request, file: UploadFile = File(...), page_id: str = Form(""),
                       user: dict = Depends(current_user)):
    raw = await file.read()
    await file.close()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return back(request, err="The file must be a UTF-8 CSV (save as 'CSV UTF-8' from Excel).", default="/posts")
    try:
        limits.enforce(user, "create_post")
    except limits.LimitError as exc:
        return back(request, err=str(exc), default="/posts")
    result = importer.import_csv(text, user["id"], page_id=_page_ref(page_id, user))
    msg = f"Imported {result['created']} post(s)."
    if result["errors"]:
        return back(request, err=msg + " Problems: " + " ".join(result["errors"][:5]), default="/posts")
    return back(request, msg=msg, default="/posts")


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
                  key_ok=key_ok, key_error=key_error, youtube=youtube.connection(user["id"]),
                  tiktok_conn=tiktok.connection(user["id"]), tiktok_mode=tiktok.POST_MODE,
                  review_links=review.list_links(user["id"]), base_url=str(request.base_url).rstrip("/"))


# ---------------------------------------------------------------------------
# Client review links (owner side)
# ---------------------------------------------------------------------------

@app.post("/settings/review-links")
def review_link_create(request: Request, label: str = Form(""), page_id: str = Form(""), days: str = Form(""),
                       user: dict = Depends(current_user)):
    days = days.strip()
    if days and not (days.isdigit() and 1 <= int(days) <= 365):
        return RedirectResponse("/settings?err=" + quote("Expiry must be a number of days between 1 and 365, or empty.") + "#review",
                                status_code=303)
    if page_id.strip() and not page_id.strip().isdigit():
        return RedirectResponse("/settings?err=" + quote("Pick a Page from the list.") + "#review", status_code=303)
    try:
        link = review.create_link(user["id"], label, page_id.strip() or None, int(days) if days else None)
    except review.ReviewError as exc:
        return RedirectResponse("/settings?err=" + quote(str(exc)) + "#review", status_code=303)
    return RedirectResponse("/settings?msg=" + quote(f"Review link created for {link['label']}. Copy it below and send it to the client.")
                            + "#review", status_code=303)


@app.post("/settings/review-links/{link_id}/revoke")
def review_link_revoke(link_id: int, user: dict = Depends(current_user)):
    if not review.revoke(link_id, user["id"]):
        raise HTTPException(404, "Link not found")
    return RedirectResponse("/settings?msg=" + quote("Review link revoked. It no longer opens.") + "#review", status_code=303)


# ---------------------------------------------------------------------------
# Client review page (public, token in the URL, no login)
# ---------------------------------------------------------------------------

def _review_link_or_404(token: str) -> dict:
    link = review.by_token(token)
    if not link:
        raise HTTPException(404, "This review link is not valid any more. Ask for a new one.")
    return link


def _review_ctx(request: Request, link: dict, **ctx) -> dict:
    ctx.update(request=request, link=link, token=link["token"],
               posts=review.pending_posts(link), done=review.recent_decisions(link),
               csrf=getattr(request.state, "csrf", None) or "",
               msg=request.query_params.get("msg"), err=request.query_params.get("err"),
               platform_labels=templates.env.globals["platform_labels"])
    return ctx


@app.get("/review/{token}", response_class=HTMLResponse)
def review_page(request: Request, token: str):
    link = _review_link_or_404(token)
    return templates.TemplateResponse(request, "review.html", _review_ctx(request, link))


@app.post("/review/{token}/{post_id}")
def review_decide(request: Request, token: str, post_id: int, decision: str = Form(...), note: str = Form("")):
    link = _review_link_or_404(token)
    try:
        post = review.decide(link, post_id, decision, note)
    except review.ReviewError as exc:
        return RedirectResponse(f"/review/{token}?err=" + quote(str(exc)), status_code=303)
    msg = (f"Approved: \"{post['topic'] or 'post'}\" will be published." if decision == "approve"
           else "Thanks, your note was sent.")
    return RedirectResponse(f"/review/{token}?msg=" + quote(msg), status_code=303)


@app.get("/review/{token}/files/{post_id}/carousel/{n}")
def review_slide(token: str, post_id: int, n: int):
    link = _review_link_or_404(token)
    if not review.post_for_link(link, post_id):
        raise HTTPException(404, "Not found")
    return _slide_response(post_id, n)


@app.get("/review/{token}/files/{post_id}/{kind}")
def review_file(token: str, post_id: int, kind: str):
    link = _review_link_or_404(token)
    post = review.post_for_link(link, post_id)
    if not post:
        raise HTTPException(404, "Not found")
    keys = {"image": ("jpeg_path", "image_path"), "video": ("video_path",)}
    for key in keys.get(kind, ()):
        path = post.get(key)
        if path and Path(path).exists():
            return FileResponse(path)
    raise HTTPException(404, "File not found")


@app.post("/settings/youtube/connect")
def youtube_connect(request: Request, user: dict = Depends(current_user)):
    state = pysecrets.token_urlsafe(16)
    try:
        url = youtube.auth_url(user["id"], str(request.base_url), state)
    except secrets.ConfigError as exc:
        return back(request, err=str(exc), default="/settings")
    response = RedirectResponse(url, status_code=303)
    response.set_cookie("yt_state", state, httponly=True, samesite="lax", max_age=600, path="/settings")
    return response


@app.get("/settings/youtube/callback")
def youtube_callback(request: Request, code: str = "", state: str = "", error: str = "",
                     user: dict = Depends(current_user)):
    if error or not code:
        return RedirectResponse("/settings?err=" + quote(f"YouTube connection cancelled: {error or 'no code'}"), status_code=303)
    if not state or state != request.cookies.get("yt_state"):
        return RedirectResponse("/settings?err=" + quote("YouTube connection failed the security check. Try again."), status_code=303)
    try:
        record = youtube.exchange_code(user["id"], code, str(request.base_url))
    except (youtube.YouTubeError, secrets.ConfigError, crypto.CryptoError) as exc:
        return RedirectResponse("/settings?err=" + quote(str(exc)), status_code=303)
    response = RedirectResponse("/settings?msg=" + quote(f"YouTube connected: {record.get('channel_title') or 'channel'}"),
                                status_code=303)
    response.delete_cookie("yt_state", path="/settings")
    return response


@app.post("/settings/youtube/disconnect")
def youtube_disconnect(request: Request, user: dict = Depends(current_user)):
    youtube.disconnect(user["id"])
    return back(request, msg="YouTube disconnected.", default="/settings")


@app.post("/settings/tiktok/connect")
def tiktok_connect(request: Request, user: dict = Depends(current_user)):
    state, verifier = pysecrets.token_urlsafe(16), tiktok.new_verifier()
    try:
        url = tiktok.auth_url(user["id"], str(request.base_url), state, verifier)
    except secrets.ConfigError as exc:
        return back(request, err=str(exc), default="/settings")
    response = RedirectResponse(url, status_code=303)
    response.set_cookie("tt_state", state, httponly=True, samesite="lax", max_age=600, path="/settings")
    response.set_cookie("tt_verifier", verifier, httponly=True, samesite="lax", max_age=600, path="/settings")
    return response


@app.get("/settings/tiktok/callback/")
@app.get("/settings/tiktok/callback")
def tiktok_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = "",
                    user: dict = Depends(current_user)):
    if error or not code:
        return RedirectResponse("/settings?err=" + quote(f"TikTok connection cancelled: {error_description or error or 'no code'}"),
                                status_code=303)
    verifier = request.cookies.get("tt_verifier", "")
    if not state or state != request.cookies.get("tt_state") or not verifier:
        return RedirectResponse("/settings?err=" + quote("TikTok connection failed the security check. Try again."), status_code=303)
    try:
        record = tiktok.exchange_code(user["id"], code, str(request.base_url), verifier)
    except (tiktok.TikTokError, secrets.ConfigError, crypto.CryptoError) as exc:
        return RedirectResponse("/settings?err=" + quote(str(exc)), status_code=303)
    response = RedirectResponse("/settings?msg=" + quote(f"TikTok connected: {record.get('display_name') or 'account'}"),
                                status_code=303)
    response.delete_cookie("tt_state", path="/settings")
    response.delete_cookie("tt_verifier", path="/settings")
    return response


@app.post("/settings/tiktok/disconnect")
def tiktok_disconnect(request: Request, user: dict = Depends(current_user)):
    tiktok.disconnect(user["id"])
    return back(request, msg="TikTok disconnected.", default="/settings")


@app.post("/settings/pages/connect")
def pages_connect(request: Request, token: str = Form(...), user: dict = Depends(current_user)):
    try:
        crypto.load_key()
        connected = pages_store.connect(user["id"], token)
    except (pages_store.PagesError, crypto.CryptoError) as exc:
        return back(request, err=str(exc), default="/settings")
    names = ", ".join(f"{p['name']}" + (f" (Instagram @{p['ig_username']})" if p["ig_username"] else "")
                      for p in connected)
    return back(request, msg=f"Connected {len(connected)} Page(s): {names}", default="/settings")


@app.post("/settings/pages/{page_id}/delete")
def pages_delete(request: Request, page_id: int, user: dict = Depends(current_user)):
    pages_store.delete_page(page_id, user["id"])
    return back(request, msg="Page disconnected.", default="/settings")


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: dict = Depends(current_user)):
    try:
        next_slots = slots.preview(user.get("posting_slots"), 6)
    except slots.SlotsError:
        next_slots = []
    return render(request, "profile.html", user, usage=limits.usage(user["id"]),
                  brand=library.brand_profile(user["id"]), next_slots=next_slots,
                  default_slots=slots.DEFAULT_SLOTS)


@app.post("/profile/posting")
def posting_save(request: Request, posting_slots: str = Form(""), hashtags_as_comment: str = Form(""),
                 notify_telegram: str = Form(""), user: dict = Depends(current_user)):
    try:
        slots.parse_slots(posting_slots)
    except slots.SlotsError as exc:
        return back(request, err=str(exc), default="/profile")
    with queries.get_connection() as conn:
        conn.execute("UPDATE users SET posting_slots = ?, hashtags_as_comment = ?, notify_telegram = ? WHERE id = ?",
                     (posting_slots.strip() or None, 1 if hashtags_as_comment else 0, 1 if notify_telegram else 0,
                      user["id"]))
    return back(request, msg="Posting settings saved.", default="/profile")


@app.post("/profile/suggest-times")
def suggest_times(request: Request, page_id: str = Form(""), user: dict = Depends(current_user)):
    try:
        suggestion = analytics.suggest_slots(user["id"], int(page_id) if page_id.isdigit() else None)
    except (secrets.ConfigError, publisher.PublishError) as exc:
        return back(request, err=f"Could not read follower activity: {exc}", default="/profile")
    if not suggestion:
        return back(request, err="Meta has no 'followers online' data yet (Instagram needs 100+ followers and a few days of history).",
                    default="/profile")
    with queries.get_connection() as conn:
        conn.execute("UPDATE users SET posting_slots = ? WHERE id = ?", (suggestion, user["id"]))
    return back(request, msg=f"Posting times set from your followers' activity: {suggestion}", default="/profile")


@app.post("/profile/brand")
def brand_save(request: Request, brand_name: str = Form(""), brand_brief: str = Form(""),
               brand_language: str = Form(""), posts_per_day: str = Form("0"),
               auto_platforms: list[str] = Form([]), montage_enabled: str = Form(""),
               content_pillars: str = Form(""), auto_subtitles: str = Form(""),
               ai_posts_per_day: str = Form("0"), ai_auto_approve: str = Form(""), ai_title_overlay: str = Form(""),
               user: dict = Depends(current_user)):
    try:
        per_day = int(posts_per_day or 0)
        ai_per_day = int(ai_posts_per_day or 0)
    except ValueError:
        return back(request, err="Posts per day must be a number.", default="/profile")
    library.save_brand(user["id"], brand_name=brand_name.strip()[:120], brand_brief=brand_brief.strip()[:4000],
                       brand_language=brand_language.strip()[:60], posts_per_day=per_day,
                       auto_platforms=",".join(publisher.parse_platforms(auto_platforms)) or None,
                       montage_enabled=bool(montage_enabled), content_pillars=content_pillars.strip()[:1000],
                       auto_subtitles=bool(auto_subtitles),
                       ai_posts_per_day=ai_per_day, ai_auto_approve=bool(ai_auto_approve),
                       ai_title_overlay=bool(ai_title_overlay))
    note = ""
    if per_day:
        note += f" The worker will make {per_day} post(s) a day from your library."
    if ai_per_day:
        note += f" It will also write and paint {ai_per_day} AI post(s) every morning."
    return back(request, msg="Brand profile saved." + note, default="/profile")


# ---------------------------------------------------------------------------
# Ideas and calendar of dates
# ---------------------------------------------------------------------------

@app.get("/ideas", response_class=HTMLResponse)
def ideas_page(request: Request, user: dict = Depends(current_user)):
    return render(request, "ideas.html", user, events=ideas.upcoming(60), suggestions=None)


@app.post("/ideas/suggest", response_class=HTMLResponse)
def ideas_suggest(request: Request, user: dict = Depends(current_user)):
    brand = library.brand_profile(user["id"])
    try:
        suggestions = ideas.suggest_topics(brand, user["id"])
    except (providers.AIError, secrets.ConfigError) as exc:
        return render(request, "ideas.html", user, events=ideas.upcoming(60), suggestions=None,
                      err=f"Could not get ideas: {exc}")
    return render(request, "ideas.html", user, events=ideas.upcoming(60), suggestions=suggestions)


# ---------------------------------------------------------------------------
# Performance and client reports
# ---------------------------------------------------------------------------

@app.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request, month: str | None = None, page_id: int | None = None,
                     user: dict = Depends(current_user)):
    try:
        data = analytics.performance(user["id"], month, page_id)
    except ValueError:
        raise HTTPException(400, "month must look like 2026-11")
    return render(request, "performance.html", user, data=data, page_id=page_id)


@app.post("/performance/refresh")
def performance_refresh(request: Request, background: BackgroundTasks, user: dict = Depends(current_user)):
    background.add_task(analytics.refresh, user["id"], True)
    return back(request, msg="Fetching the latest numbers from Meta in the background. Refresh in a moment.",
                default="/performance")


@app.get("/performance/report", response_class=HTMLResponse)
def performance_report(request: Request, month: str | None = None, page_id: int | None = None,
                       user: dict = Depends(current_user)):
    """A clean, printable monthly report to send to a client (print to PDF from the browser)."""
    try:
        data = analytics.performance(user["id"], month, page_id)
    except ValueError:
        raise HTTPException(400, "month must look like 2026-11")
    brand = library.brand_profile(user["id"])
    page = pages_store.get_page(page_id, user["id"]) if page_id else None
    return templates.TemplateResponse(request, "report.html", {"request": request, "data": data, "brand": brand,
                                                               "page": page, "user": user, "now": datetime.now()})


@app.get("/performance/report.csv")
def performance_csv(month: str | None = None, page_id: int | None = None, user: dict = Depends(current_user)):
    data = analytics.performance(user["id"], month, page_id)
    return PlainTextResponse(analytics.report_csv(data), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="report-{data["month"]}.csv"'})


@app.post("/posts/{post_id}/evergreen")
def evergreen_toggle(request: Request, post_id: int, evergreen: str = Form(""), recycle_days: str = Form("30"),
                     user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    try:
        days = max(1, min(365, int(recycle_days or 30)))
    except ValueError:
        days = 30
    with queries.get_connection() as conn:
        conn.execute("UPDATE posts SET evergreen = ?, recycle_days = ? WHERE id = ?",
                     (1 if evergreen else 0, days, post_id))
    return back(request, msg=("Evergreen: this post will come back as a draft every %d days." % days) if evergreen
                else "Evergreen off.", default=f"/posts/{post_id}")


# ---------------------------------------------------------------------------
# Media library
# ---------------------------------------------------------------------------

@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, user: dict = Depends(current_user)):
    library.scan(user["id"])
    assets = library.list_assets(user["id"])
    return render(request, "library.html", user, assets=assets, folder=str(library.user_dir(user["id"])),
                  brand=library.brand_profile(user["id"]),
                  pending=sum(1 for a in assets if not a["analyzed_at"] and not a["excluded"]),
                  vision_provider=providers.pick("vision", user["id"]),
                  vision_ready=_vision_ready(user["id"]))


def _vision_ready(user_id: int) -> bool:
    for name in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        try:
            secrets.get_secret(name, user_id)
            return True
        except secrets.ConfigError:
            continue
    return False


@app.post("/library/upload")
async def library_upload(request: Request, files: list[UploadFile] = File(...),
                         user: dict = Depends(limits.require_feature("upload"))):
    folder = library.user_dir(user["id"])
    saved, refused = 0, []
    for f in files:
        name = Path(f.filename or "").name
        ext = Path(name).suffix.lower()
        if ext not in library.IMAGE_EXTS | library.VIDEO_EXTS:
            refused.append(name)
            await f.close()
            continue
        dest = folder / name
        stem, n = dest.stem, 1
        while dest.exists():
            dest = folder / f"{stem}_{n}{ext}"
            n += 1
        limit = MAX_UPLOAD_MB * 1024 * 1024
        written = 0
        try:
            with open(dest, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(413, name)
                    out.write(chunk)
            saved += 1
        except HTTPException:
            dest.unlink(missing_ok=True)
            refused.append(f"{name} (over {MAX_UPLOAD_MB} MB)")
        finally:
            await f.close()
    library.scan(user["id"])
    msg = f"{saved} file(s) added to your library."
    if refused:
        return back(request, err=msg + " Skipped: " + ", ".join(refused), default="/library")
    return back(request, msg=msg, default="/library")


@app.post("/library/analyze")
def library_analyze(request: Request, background: BackgroundTasks, user: dict = Depends(current_user)):
    if not _vision_ready(user["id"]):
        return back(request, err="Add a Gemini (free) or OpenAI key on API settings first, so the tool can look at your photos.",
                    default="/library")
    background.add_task(library.analyze_pending, user["id"])
    return back(request, msg="Analyzing your library in the background. Refresh in a minute.", default="/library")


@app.post("/library/plan")
def library_plan(request: Request, background: BackgroundTasks, count: str = Form("1"),
                 user: dict = Depends(current_user)):
    try:
        n = max(1, min(10, int(count or 1)))
    except ValueError:
        n = 1
    try:
        limits.enforce(user, "create_post")
    except limits.LimitError as exc:
        return back(request, err=str(exc), default="/library")
    if not _vision_ready(user["id"]):
        return back(request, err="Add a Gemini (free) or OpenAI key on API settings first.", default="/library")
    background.add_task(library.plan, user["id"], n)
    return back(request, msg=f"Making {n} post(s) from your library in the background. They appear in the "
                             "approval queue; montages take a minute or two to render.", default="/library")


@app.post("/library/carousel")
def library_carousel(request: Request, asset_ids: list[str] = Form([]), user: dict = Depends(current_user)):
    """Make one carousel draft from the ticked library photos (in the order ticked)."""
    try:
        limits.enforce(user, "create_post")
        pid = carousel.create_from_assets(user["id"], asset_ids)
    except (limits.LimitError, carousel.CarouselError) as exc:
        return back(request, err=str(exc), default="/library")
    return RedirectResponse(f"/posts/{pid}?msg=" + quote(f"Carousel draft #{pid} created with {len(asset_ids)} photos. "
                                                        "Check the caption, then approve it."), status_code=303)


@app.post("/library/{asset_id}/exclude")
def library_exclude(request: Request, asset_id: int, excluded: str = Form("1"),
                    user: dict = Depends(current_user)):
    if not library.get_asset(asset_id, user["id"]):
        raise HTTPException(404, "Not found")
    library.set_excluded(asset_id, user["id"], excluded == "1")
    return back(request, msg="Updated.", default="/library")


@app.post("/library/{asset_id}/delete")
def library_delete(request: Request, asset_id: int, remove_file: str = Form(""),
                   user: dict = Depends(current_user)):
    if not library.get_asset(asset_id, user["id"]):
        raise HTTPException(404, "Not found")
    library.delete_asset(asset_id, user["id"], remove_file=bool(remove_file))
    return back(request, msg="Removed from the library." + (" File deleted." if remove_file else ""), default="/library")


@app.get("/library/file/{asset_id}")
def library_file(asset_id: int, user: dict = Depends(current_user)):
    asset = library.get_asset(asset_id, user["id"])
    if not asset or not Path(asset["path"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(asset["path"])


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, user: dict = Depends(current_user)):
    return render(request, "billing.html", user, usage=limits.usage(user["id"]),
                  stripe_ready=billing.is_configured())


# ---------------------------------------------------------------------------
# Media files (ownership checked, never served from a public static mount)
# ---------------------------------------------------------------------------

@app.get("/files/{post_id}/carousel/{n}")
def post_slide(post_id: int, n: int, user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    return _slide_response(post_id, n)


def _slide_response(post_id: int, n: int):
    slides = carousel.items(post_id)
    if not 0 <= n < len(slides):
        raise HTTPException(404, "No such photo")
    for key in ("jpeg_path", "image_path"):
        path = slides[n].get(key)
        if path and Path(path).exists():
            return FileResponse(path)
    raise HTTPException(404, "File not found")


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
             business: str = Form(""), media_type: str = Form("image"),
             platforms: list[str] = Form([]), page_id: str = Form(""),
             user: dict = Depends(current_user)):
    topic = topic.strip()
    page_ref = _page_ref(page_id, user)
    # The AI writes in the style of the first platform ticked (Instagram if none).
    platform = (publisher.parse_platforms(platforms) or ["instagram"])[0]
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
                                   user_id=user["id"], platforms=platforms, page_id=page_ref)
        return RedirectResponse(f"/posts/{pid}?msg=Draft+created", status_code=303)
    background.add_task(pipeline.run_pipeline, steps=["generate"], topics=[topic],
                        platform=platform, business=business, media_type=media_type,
                        user_id=user["id"])
    return back(request, msg=f"Generating a draft for '{topic}' in the background. "
                             "Refresh in a few seconds.", default="/queue")


def _page_ref(page_id: str, user: dict) -> int | None:
    """Validate a page choice from a form: must be one of this user's connected Pages."""
    value = (page_id or "").strip()
    if not value.isdigit():
        return pages_store.default_page_id(user["id"])
    return int(value) if pages_store.get_page(int(value), user["id"]) else pages_store.default_page_id(user["id"])


@app.post("/posts/{post_id}/approve")
@app.post("/posts/{post_id}/approve")
def approve(request: Request, post_id: int, scheduled_time: str = Form(""),
            platforms: list[str] = Form([]), page_id: str = Form(""), also_story: str = Form(""),
            user: dict = Depends(current_user)):
    post = post_or_404(post_id, user)
    queries.update_post(post_id, also_story=1 if also_story else 0)
    chosen = publisher.parse_platforms(platforms)
    if not chosen:
        return back(request, err="Tick at least one platform to post to.")
    if post["media_type"] == "carousel":
        chosen = [p for p in chosen if p in publisher.CAROUSEL_PLATFORMS]
        if not chosen:
            return back(request, err="Carousels only go to Facebook and Instagram. Tick one of them.")
    if scheduled_time.strip().lower() == "next":
        try:
            scheduled_time = slots.next_free_slot(user["id"], user.get("posting_slots"))
        except slots.SlotsError as exc:
            return back(request, err=str(exc))
    try:
        queries.set_status(post_id, "approved", scheduled_time, platforms=chosen,
                           page_id=_page_ref(page_id, user))
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
         platforms: list[str] = Form([]), page_id: str = Form(""), user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    try:
        queries.update_post(post_id, topic=topic, caption=caption, hashtags=hashtags,
                            image_prompt=image_prompt, scheduled_time=scheduled_time,
                            image_url=image_url, video_url=video_url, media_type=media_type,
                            platforms=platforms, page_id=_page_ref(page_id, user))
    except ValueError as exc:
        return back(request, err=f"Not saved: {exc}" if "media_type" in str(exc)
                    else "Scheduled time must look like 2026-11-20 09:30.")
    return back(request, msg="Saved.", default=f"/posts/{post_id}")


@app.post("/posts/{post_id}/clear-error")
def clear_error(request: Request, post_id: int, user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    queries.clear_error(post_id)
    return back(request, msg=f"Error cleared on post #{post_id}.", default="/logs")


def _delete_one(post: dict, remove_remote: bool) -> str | None:
    """Delete a post locally, optionally from Facebook too. Returns a warning or None."""
    warning = None
    if remove_remote and post.get("fb_post_id"):
        try:
            publisher.delete_facebook_post(post, post["fb_post_id"])
        except (publisher.PublishError, secrets.ConfigError) as exc:
            warning = f"Removed here, but Facebook refused to delete post {post['fb_post_id']}: {exc}"
    queries.delete_post(post["id"])
    return warning


@app.post("/posts/{post_id}/delete")
def delete(request: Request, post_id: int, remove_remote: str = Form(""), next: str = Form("/posts"),
           user: dict = Depends(current_user)):
    post = post_or_404(post_id, user)
    warning = _delete_one(post, bool(remove_remote))
    target = next if next.startswith("/") and not next.startswith("//") else "/posts"
    if warning:
        return RedirectResponse(f"{target}?err=" + quote(warning), status_code=303)
    return RedirectResponse(f"{target}?msg=" + quote(f"Post #{post_id} deleted."), status_code=303)


@app.post("/posts/{post_id}/reset")
def reset(request: Request, post_id: int, user: dict = Depends(current_user)):
    post_or_404(post_id, user)
    queries.reset_publishing(post_id)
    return back(request, msg=f"Post #{post_id} is a draft again. Approve it to publish once more.",
                default=f"/posts/{post_id}")


@app.post("/posts/bulk")
def posts_bulk(request: Request, action: str = Form(...), ids: list[int] = Form([]),
               remove_remote: str = Form(""), user: dict = Depends(current_user)):
    if action not in ("delete", "reset", "reject", "unapprove"):
        return back(request, err="Unknown action.", default="/posts")
    done, warnings = 0, []
    for pid in ids:
        post = queries.get_post(pid, user_id=user["id"])
        if not post:
            continue
        if action == "delete":
            w = _delete_one(post, bool(remove_remote))
            if w:
                warnings.append(w)
        elif action == "reset":
            queries.reset_publishing(pid)
        elif action == "reject":
            queries.set_status(pid, "rejected")
        elif action == "unapprove":
            queries.set_status(pid, "draft")
        done += 1
    msg = f"{action}: {done} post(s)."
    if warnings:
        return back(request, err=msg + " " + " ".join(warnings), default="/posts")
    return back(request, msg=msg, default="/posts")


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


def _probe_is_image(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:  # noqa: BLE001
        return False


async def _receive_media(file: UploadFile, user: dict) -> tuple[Path, str, int, str]:
    """
    Stream an uploaded image or video to disk with validation.
    Returns (path, kind 'image'|'video', bytes written, original filename).
    """
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext in UPLOAD_EXTENSIONS:
        kind = "video"
    elif ext in IMAGE_EXTENSIONS:
        kind = "image"
    else:
        raise HTTPException(400, "Only video (mp4, mov, webm) or image (jpg, png, webp) files are accepted.")
    ctype = file.content_type or ""
    if ctype and not ctype.startswith((f"{kind}/", "application/octet-stream")):
        raise HTTPException(400, f"Unexpected content type {ctype} for a {kind}.")

    folder = (UPLOAD_DIR if kind == "video" else UPLOAD_IMAGE_DIR) / f"user_{user['id']}"
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
        if kind == "video" and not _probe_is_video(dest):
            raise HTTPException(400, "That file does not contain a video stream.")
        if kind == "image" and not _probe_is_image(dest):
            raise HTTPException(400, "That file is not a readable image.")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return dest, kind, written, name


@app.post("/api/posts/upload")
async def upload_media(file: UploadFile = File(...), topic: str = Form(""), caption: str = Form(""),
                       hashtags: str = Form(""), platforms: list[str] = Form([]), page_id: str = Form(""),
                       user: dict = Depends(limits.require_feature("upload"))):
    """Drop a finished video or a photo: becomes a draft that skips every AI step."""
    dest, kind, written, name = await _receive_media(file, user)
    pid = queries.create_draft(
        topic.strip() or Path(name).stem, caption, hashtags, media_type=kind, user_id=user["id"],
        source="upload", upload_name=name, platforms=platforms, page_id=_page_ref(page_id, user),
        video_path=str(dest) if kind == "video" else None,
        image_path=str(dest) if kind == "image" else None)
    log.info("User %s uploaded %s %s (%d KB) as post #%s", user["id"], kind, name, written // 1024, pid)
    return {"id": pid, "url": f"/posts/{pid}", "bytes": written, "filename": name, "kind": kind}


@app.post("/posts/{post_id}/media")
async def attach_media(request: Request, post_id: int, file: UploadFile = File(...),
                       user: dict = Depends(current_user)):
    """Attach (or replace) the photo or video of an existing post."""
    post = post_or_404(post_id, user)
    try:
        dest, kind, written, name = await _receive_media(file, user)
    except HTTPException as exc:
        return back(request, err=str(exc.detail), default=f"/posts/{post_id}")
    note = ""
    with queries.get_connection() as conn:
        if post["media_type"] == "carousel":
            conn.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
            note = " The carousel photos were replaced; this is now a single-media post."
        if kind == "video":
            conn.execute("UPDATE posts SET video_path = ?, video_url = NULL, hosted_at = NULL, media_type = 'video', "
                         "upload_name = ?, last_error = NULL WHERE id = ?", (str(dest), name, post_id))
        else:
            new_type = "image" if post["media_type"] == "carousel" else post["media_type"]   # a video keeps its format
            conn.execute("UPDATE posts SET image_path = ?, jpeg_path = NULL, image_url = NULL, hosted_at = NULL, "
                         "media_type = ?, upload_name = ?, last_error = NULL WHERE id = ?", (str(dest), new_type, name, post_id))
    return back(request, msg=f"{kind.capitalize()} attached ({written // 1024} KB).{note}", default=f"/posts/{post_id}")


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
