"""Authentication: password hashing, JWT lifetime, login/signup routes, protected routes."""

import os
import tempfile
import time
import unittest
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

import jwt                                              # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src.web import app as webapp, auth                 # noqa: E402


class PasswordTests(unittest.TestCase):
    def test_hash_is_bcrypt_and_verifies(self):
        h = auth.hash_password("correct horse battery")
        self.assertTrue(h.startswith("$2b$12$"))
        self.assertNotIn("horse", h)
        self.assertTrue(auth.verify_password("correct horse battery", h))
        self.assertFalse(auth.verify_password("Correct horse battery", h))
        self.assertFalse(auth.verify_password("x", "not-a-hash"))

    def test_same_password_different_hashes(self):
        self.assertNotEqual(auth.hash_password("samepass1"), auth.hash_password("samepass1"))

    def test_password_rules(self):
        with self.assertRaises(auth.AuthError):
            auth.validate_password("short")
        with self.assertRaises(auth.AuthError):
            auth.validate_password("x" * 73)
        auth.validate_password("longenough")

    def test_email_rules(self):
        self.assertEqual(auth.normalize_email("  Yannis@Example.COM "), "yannis@example.com")
        for bad in ("", "nope", "a@b", "a b@c.com"):
            with self.assertRaises(auth.AuthError):
                auth.normalize_email(bad)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"JWT_SECRET": "unit-test-secret"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_roundtrip(self):
        self.assertEqual(auth.decode_token(auth.create_token(42)), 42)

    def test_expired_token_is_rejected(self):
        token = auth.create_token(1, expires_minutes=-1)
        with self.assertRaisesRegex(auth.AuthError, "expired"):
            auth.decode_token(token)

    def test_expiry_is_honoured_to_the_second(self):
        now = int(time.time())
        token = jwt.encode({"sub": "1", "exp": now + 2}, auth._jwt_secret(), algorithm="HS256")
        self.assertEqual(auth.decode_token(token), 1)
        with mock.patch("jwt.api_jwt.datetime") as dt:   # jump 10 s into the future
            import datetime as _dt
            dt.now.return_value = _dt.datetime.fromtimestamp(now + 10, tz=_dt.timezone.utc)
            dt.timezone = _dt.timezone
            with self.assertRaises(auth.AuthError):
                auth.decode_token(token)

    def test_forged_or_wrong_secret_token_is_rejected(self):
        forged = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, "other-secret", algorithm="HS256")
        with self.assertRaisesRegex(auth.AuthError, "Invalid"):
            auth.decode_token(forged)
        with self.assertRaises(auth.AuthError):
            auth.decode_token("garbage")
        unsigned = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, None, algorithm="none")
        with self.assertRaises(auth.AuthError):
            auth.decode_token(unsigned)

    def test_token_changes_with_secret(self):
        token = auth.create_token(1)
        with mock.patch.dict(os.environ, {"JWT_SECRET": "rotated"}):
            with self.assertRaises(auth.AuthError):
                auth.decode_token(token)


class UserTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM users")

    def test_create_and_authenticate(self):
        user = auth.create_user("Me@Example.com", "password123", "Me")
        self.assertEqual(user["email"], "me@example.com")
        self.assertEqual(user["tier"], "free")
        self.assertNotIn("password_hash", user)
        self.assertEqual(auth.authenticate("me@example.com", "password123")["id"], user["id"])
        for email, pw in (("me@example.com", "wrong"), ("nobody@example.com", "password123")):
            with self.assertRaisesRegex(auth.AuthError, "Wrong email or password"):
                auth.authenticate(email, pw)

    def test_duplicate_email(self):
        auth.create_user("dup@example.com", "password123")
        with self.assertRaisesRegex(auth.AuthError, "already exists"):
            auth.create_user("DUP@example.com", "password456")

    def test_change_password(self):
        user = auth.create_user("pw@example.com", "password123")
        with self.assertRaises(auth.AuthError):
            auth.change_password(user["id"], "nope", "newpassword1")
        auth.change_password(user["id"], "password123", "newpassword1")
        auth.authenticate("pw@example.com", "newpassword1")

    def test_inactive_user_cannot_log_in(self):
        user = auth.create_user("off@example.com", "password123")
        with get_connection() as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],))
        with self.assertRaises(auth.AuthError):
            auth.authenticate("off@example.com", "password123")

    def test_login_rate_limit(self):
        auth._attempts.clear()
        for _ in range(auth.LOGIN_ATTEMPTS_PER_MINUTE):
            auth.check_login_rate("1.2.3.4")
        with self.assertRaisesRegex(auth.AuthError, "Too many"):
            auth.check_login_rate("1.2.3.4")
        auth.check_login_rate("5.6.7.8")   # another client is fine


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(webapp.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM posts")
        self.client.cookies.clear()
        auth._attempts.clear()

    def test_pages_redirect_to_login_and_api_returns_401(self):
        for path in ("/", "/queue", "/calendar", "/logs", "/settings", "/profile", "/billing", "/posts/1"):
            r = self.client.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 303, path)
            self.assertTrue(r.headers["location"].startswith("/login?next="), path)
        for path in ("/api/stats", "/api/posts", "/api/auth/me", "/api/runs"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        r = self.client.get("/files/1/video", follow_redirects=False)
        self.assertEqual(r.status_code, 303)          # media never served to anonymous callers
        self.assertEqual(self.client.post("/pipeline/run", data={"steps": "images"}, follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_signup_logs_in_and_sets_cookie(self):
        r = self.client.post("/signup", data={"email": "new@example.com", "password": "password123",
                                              "password2": "password123", "display_name": "New"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"].split("?")[0], "/")
        self.assertIn(auth.COOKIE_NAME, self.client.cookies)
        self.assertIn("set-cookie", r.headers)
        self.assertIn("HttpOnly", r.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/auth/me").json()["email"], "new@example.com")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_signup_validation(self):
        for data, needle in (
            ({"email": "a@b.co", "password": "password123", "password2": "different1"}, "do%20not%20match"),
            ({"email": "bad", "password": "password123", "password2": "password123"}, "valid%20email"),
            ({"email": "a@b.co", "password": "short", "password2": "short"}, "at%20least"),
        ):
            r = self.client.post("/signup", data=data, follow_redirects=False)
            self.assertIn(needle, r.headers["location"], data)
        self.assertNotIn(auth.COOKIE_NAME, self.client.cookies)

    def test_login_logout_and_next(self):
        auth.create_user("log@example.com", "password123")
        r = self.client.post("/login", data={"email": "log@example.com", "password": "wrong"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])
        self.assertNotIn(auth.COOKIE_NAME, self.client.cookies)
        r = self.client.post("/login", data={"email": "log@example.com", "password": "password123", "next": "/queue"},
                             follow_redirects=False)
        self.assertEqual(r.headers["location"], "/queue")
        self.assertEqual(self.client.get("/queue").status_code, 200)
        r = self.client.post("/login", data={"email": "log@example.com", "password": "password123",
                                             "next": "//evil.example.com"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/")
        r = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_expired_cookie_redirects_to_login(self):
        user = auth.create_user("exp@example.com", "password123")
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(user["id"], expires_minutes=-5))
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.client.get("/api/stats").status_code, 401)
        self.assertIn("expired", self.client.get("/api/stats").json()["detail"])

    def test_oauth2_token_flow(self):
        auth.create_user("api@example.com", "password123")
        self.assertEqual(self.client.post("/api/auth/token", data={"username": "api@example.com", "password": "x"}).status_code, 401)
        r = self.client.post("/api/auth/token", data={"username": "api@example.com", "password": "password123"})
        self.assertEqual(r.status_code, 200)
        token = r.json()["access_token"]
        self.assertEqual(r.json()["token_type"], "bearer")
        me = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.json()["email"], "api@example.com")
        self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_profile_updates(self):
        auth.create_user("prof@example.com", "password123")
        self.client.post("/login", data={"email": "prof@example.com", "password": "password123"}, follow_redirects=False)
        self.client.post("/profile", data={"display_name": "Apex Digital"}, follow_redirects=False)
        self.assertIn("Apex Digital", self.client.get("/profile").text)
        r = self.client.post("/profile/password", data={"current": "password123", "new": "newpassword1", "new2": "newpassword1"},
                             headers={"referer": "http://x/profile"}, follow_redirects=False)
        self.assertIn("msg=", r.headers["location"])
        auth.authenticate("prof@example.com", "newpassword1")
        r = self.client.post("/profile/password", data={"current": "bad", "new": "newpassword2", "new2": "newpassword2"},
                             headers={"referer": "http://x/profile"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])


if __name__ == "__main__":
    unittest.main()
