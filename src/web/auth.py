"""
Authentication: users, bcrypt password hashing, JWT sessions.

Browser pages use a JWT in an httpOnly cookie (set by /login).
API clients use OAuth2 password flow: POST /api/auth/token -> {"access_token"}
and send "Authorization: Bearer <token>".

Secrets: JWT_SECRET in .env (falls back to APP_SECRET_KEY). Without either, a
random secret is used and every restart logs everyone out.
"""

import hashlib
import logging
import os
import re
import secrets as pysecrets
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer

from config.db import get_connection

log = logging.getLogger("auth")

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", str(60 * 24 * 7)))   # 7 days
COOKIE_NAME = "session"
MIN_PASSWORD_LENGTH = 8
LOGIN_ATTEMPTS_PER_MINUTE = 8
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


class AuthError(Exception):
    """Bad credentials, expired token, or a validation problem. Message is user-safe."""


def _jwt_secret() -> str:
    value = os.getenv("JWT_SECRET") or os.getenv("APP_SECRET_KEY") or ""
    if value and not value.startswith("your_"):
        return hashlib.sha256(value.encode()).hexdigest()
    global _EPHEMERAL
    if "_EPHEMERAL" not in globals():
        _EPHEMERAL = pysecrets.token_hex(32)
        log.warning("JWT_SECRET / APP_SECRET_KEY not set: using a temporary secret. "
                    "Logins will not survive a restart.")
    return _EPHEMERAL


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > 72:
        raise AuthError("Password is too long (72 bytes maximum).")


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email):
        raise AuthError("That does not look like a valid email address.")
    return email


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _public(user: dict | None) -> dict | None:
    if user is None:
        return None
    return {k: v for k, v in user.items() if k != "password_hash"}


def create_user(email: str, password: str, display_name: str = "") -> dict:
    email = normalize_email(email)
    validate_password(password)
    with get_connection() as conn:
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            raise AuthError("An account with that email already exists.")
        uid = conn.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
            (email, hash_password(password), display_name.strip() or email.split("@")[0]),
        ).lastrowid
    return get_user(uid)


def get_user(user_id: int) -> dict | None:
    with get_connection() as conn:
        return _public(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_email(email: str) -> dict | None:
    with get_connection() as conn:
        return _public(conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone())


def authenticate(email: str, password: str) -> dict:
    """Return the user for valid credentials, else raise AuthError (same message either way)."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    # Always run bcrypt so a missing account takes as long as a wrong password.
    ok = verify_password(password, row["password_hash"] if row else hash_password("x" * 8))
    if not row or not ok or not row["is_active"]:
        raise AuthError("Wrong email or password.")
    with get_connection() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
    return _public(row)


def change_password(user_id: int, current: str, new: str) -> None:
    with get_connection() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not verify_password(current, row["password_hash"]):
        raise AuthError("Current password is wrong.")
    validate_password(new)
    with get_connection() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user_id))


def update_profile(user_id: int, display_name: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name.strip()[:80], user_id))


def list_user_ids() -> list[int]:
    with get_connection() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM users WHERE is_active = 1 ORDER BY id")]


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def create_token(user_id: int, expires_minutes: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now,
               "exp": now + timedelta(minutes=expires_minutes or JWT_EXPIRE_MINUTES)}
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> int:
    """Return the user id inside a valid token. Raises AuthError if expired or forged."""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Your session has expired. Please log in again.") from exc
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise AuthError("Invalid session. Please log in again.") from exc


# ---------------------------------------------------------------------------
# Login rate limit (per client address, in memory)
# ---------------------------------------------------------------------------

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def check_login_rate(client: str) -> None:
    now = time.time()
    with _attempts_lock:
        recent = [t for t in _attempts.get(client, []) if now - t < 60]
        if len(recent) >= LOGIN_ATTEMPTS_PER_MINUTE:
            _attempts[client] = recent
            raise AuthError("Too many login attempts. Wait a minute and try again.")
        recent.append(now)
        _attempts[client] = recent


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def _token_from_request(request: Request, bearer: str | None) -> str | None:
    return bearer or request.cookies.get(COOKIE_NAME)


def optional_user(request: Request, bearer: str | None = Depends(oauth2_scheme)) -> dict | None:
    token = _token_from_request(request, bearer)
    if not token:
        return None
    try:
        user = get_user(decode_token(token))
    except AuthError:
        return None
    return user if user and user["is_active"] else None


def current_user(request: Request, bearer: str | None = Depends(oauth2_scheme)) -> dict:
    """
    The logged-in user, or: 401 JSON for /api/* routes, a redirect to /login for pages.
    """
    token = _token_from_request(request, bearer)
    user = None
    reason = "Please log in."
    if token:
        try:
            user = get_user(decode_token(token))
        except AuthError as exc:
            reason = str(exc)
    if user is None or not user["is_active"]:
        if request.url.path.startswith("/api/"):
            raise HTTPException(401, reason, headers={"WWW-Authenticate": "Bearer"})
        raise LoginRedirect(request.url.path)
    return user


class LoginRedirect(Exception):
    """Raised by current_user for page routes; turned into a 303 by the app."""

    def __init__(self, next_path: str):
        self.next_path = next_path


def set_session_cookie(response, token: str, secure: bool) -> None:
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", secure=secure,
                        max_age=JWT_EXPIRE_MINUTES * 60, path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def login_redirect_response(next_path: str = "/") -> RedirectResponse:
    safe = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    return RedirectResponse(f"/login?next={safe}", status_code=303)
