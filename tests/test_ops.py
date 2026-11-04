"""Heartbeat, backups, token health, retry cap, Stories, ideas/holidays, CSRF."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import ideas, ops, pages, publisher            # noqa: E402
from src.ai import providers                            # noqa: E402
from src.security import crypto                         # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

KEY = "c3RyZXNzLXRlc3Qta2V5LXN0cmVzcy10ZXN0LWtleS0="


class FakeResp:
    def __init__(self, payload=None, status=200):
        self._p, self.status_code, self.text, self.headers = payload, status, "", {}

    def json(self):
        return self._p


class Base(unittest.TestCase):
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
            for t in ("posts", "users", "credentials", "pages", "app_state"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY, "JWT_SECRET": "ops", "GEMINI_API_KEY": "gem"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.user = auth.create_user("ops@example.com", "password123", "Taverna")
        self.uid = self.user["id"]
        with get_connection() as conn:
            conn.execute("UPDATE users SET tier = 'enterprise' WHERE id = ?", (self.uid,))
        self.client.cookies.clear()          # a login in an earlier test leaves a cookie for a deleted user
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))


class HeartbeatBackupTests(Base):
    def test_heartbeat_and_dashboard_warning(self):
        self.assertTrue(ops.worker_status()["stale"])
        self.assertIn("background worker has not reported", self.client.get("/").text)
        ops.heartbeat("loop")
        status = ops.worker_status()
        self.assertFalse(status["stale"])
        self.assertEqual(status["minutes_ago"], 0)
        self.assertNotIn("background worker has not reported", self.client.get("/").text)
        ops.set_state("worker_heartbeat", "loop")
        with get_connection() as conn:
            conn.execute("UPDATE app_state SET updated_at = ? WHERE key = 'worker_heartbeat'",
                         ((datetime.now() - timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S"),))
        self.assertTrue(ops.worker_status()["stale"])
        self.assertIn("for 40 minutes", self.client.get("/").text)

    def test_backup_creates_one_file_per_day_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ops, "BACKUP_DIR", Path(tmp)):
            for i in range(20):
                (Path(tmp) / f"scheduler-2025{i:04d}.db").write_bytes(b"old")
            dest = ops.backup_database()
            if ops.BACKEND != "sqlite":
                self.assertIsNone(dest)                                    # PostgreSQL: pg_dump is the operator's job
            else:
                self.assertTrue(dest.exists() and dest.stat().st_size > 1000)
                self.assertEqual(ops.backup_database(), dest)             # same day: no second copy
                self.assertEqual(len(list(Path(tmp).glob("scheduler-*.db"))), ops.KEEP_BACKUPS)
            with mock.patch.object(pages, "check_all_tokens", return_value={}) as chk:
                self.assertEqual(ops.run_daily(), 0)
                self.assertEqual(ops.run_daily(), 0)                     # once a day
            chk.assert_called_once()

    def test_token_health_check_and_warning(self):
        with get_connection() as conn:
            conn.execute("INSERT INTO pages (user_id, name, fb_page_id, token_encrypted) VALUES (?, 'Unexplained Greece', '111', ?)",
                         (self.uid, crypto.encrypt("tok")))
        page = pages.list_pages(self.uid)[0]
        soon = int((datetime.now() + timedelta(days=5)).timestamp())
        with mock.patch.object(pages.requests, "get", return_value=FakeResp({"data": {"is_valid": True, "expires_at": 0,
                                                                                       "data_access_expires_at": soon}})):
            r = pages.check_all_tokens()
        self.assertEqual(r["ok"], 1)
        warned = ops.expiring_pages(self.uid)
        self.assertEqual(warned[0]["name"], "Unexplained Greece")
        self.assertIn("expires soon", self.client.get("/").text)
        with mock.patch.object(pages.requests, "get", return_value=FakeResp({"data": {"is_valid": False}})):
            pages.check_token(pages.get_page(page["id"]))
        self.assertIn("is invalid", self.client.get("/").text)
        far = int((datetime.now() + timedelta(days=80)).timestamp())
        with mock.patch.object(pages.requests, "get", return_value=FakeResp({"data": {"is_valid": True, "expires_at": far}})):
            pages.check_token(pages.get_page(page["id"]))
        self.assertEqual(ops.expiring_pages(self.uid), [])


class RetryAndStoryTests(Base):
    def setUp(self):
        super().setUp()
        self.menv = mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t", "META_PAGE_ID": "111", "META_INSTAGRAM_ACCOUNT_ID": "222"})
        self.menv.start()
        self.addCleanup(self.menv.stop)

    def test_retry_cap_then_publish_again_resets(self):
        pid = queries.create_draft("x", "c", "", user_id=self.uid, platforms=["facebook"])
        queries.update_post(pid, image_url="https://i/x.jpg")
        queries.set_status(pid, "approved", "")
        bad = lambda m, u, **kw: FakeResp({"error": {"code": 190, "message": "token"}}, 400)   # noqa: E731
        with mock.patch.object(publisher.requests, "request", side_effect=bad), \
             mock.patch.object(publisher, "notify") if False else mock.patch("src.notify.send", return_value=True) as sent:
            for _ in range(publisher.MAX_PUBLISH_ATTEMPTS + 2):
                with get_connection() as conn:                     # skip the back-off wait between tries
                    conn.execute("UPDATE posts SET retry_after = NULL WHERE id = ?", (pid,))
                publisher.main([])
        post = queries.get_post(pid)
        self.assertEqual(post["publish_attempts"], publisher.MAX_PUBLISH_ATTEMPTS)
        self.assertIn("automatic retries stopped", post["last_error"])
        self.assertEqual(sent.call_count, 2)        # first failure and the give-up, not every attempt
        self.client.post(f"/posts/{pid}/reset", follow_redirects=False)
        self.assertEqual(queries.get_post(pid)["publish_attempts"], 0)
        self.client.post(f"/posts/{pid}/approve", data={"platforms": "facebook"}, follow_redirects=False)
        with mock.patch.object(publisher.requests, "request", return_value=FakeResp({"post_id": "fb1"})):
            publisher.main([])
        self.assertEqual(queries.get_post(pid)["status"], "published")

    def test_story_is_posted_after_the_feed_post(self):
        pid = queries.create_draft("x", "c", "", user_id=self.uid, platforms=["instagram"])
        queries.update_post(pid, image_url="https://i/x.jpg")
        self.client.post(f"/posts/{pid}/approve", data={"platforms": "instagram", "also_story": "1"}, follow_redirects=False)
        self.assertEqual(queries.get_post(pid)["also_story"], 1)
        calls = []

        def fake(method, url, **kw):
            data = kw.get("data") or {}
            calls.append((url, data))
            if url.endswith("/media"):
                return FakeResp({"id": "story_c" if data.get("media_type") == "STORIES" else "feed_c"})
            if method == "GET":
                return FakeResp({"status_code": "FINISHED"})
            if url.endswith("/media_publish"):
                return FakeResp({"id": "story1" if data.get("creation_id") == "story_c" else "feed1"})
            raise AssertionError(url)

        with mock.patch.object(publisher.requests, "request", side_effect=fake), \
             mock.patch.object(publisher.time, "sleep", lambda s: None):
            self.assertEqual(publisher.main([]), 0)
        post = queries.get_post(pid)
        self.assertEqual((post["ig_post_id"], post["ig_story_id"], post["status"]), ("feed1", "story1", "published"))
        story_call = next(c for c in calls if c[1].get("media_type") == "STORIES")
        self.assertEqual(story_call[1]["image_url"], "https://i/x.jpg")
        self.assertNotIn("caption", story_call[1])
        self.assertIn("Instagram Story", self.client.get(f"/posts/{pid}").text)


class IdeasTests(Base):
    def test_orthodox_easter_and_holidays(self):
        self.assertEqual(ideas.orthodox_easter(2026), date(2026, 4, 12))
        self.assertEqual(ideas.orthodox_easter(2027), date(2027, 5, 2))
        self.assertEqual(ideas.orthodox_easter(2025), date(2025, 4, 20))
        names = {h["name"]: h["date"] for h in ideas.holidays(2026)}
        self.assertEqual(names["Clean Monday (Kathara Deftera), kites and Lenten food"], date(2026, 2, 23))
        self.assertEqual(names["Whit Monday (Agiou Pnevmatos), long weekend"], date(2026, 6, 1))
        self.assertEqual(names["Tsiknopempti, grilled-meat Thursday of Carnival"], date(2026, 2, 12))
        self.assertEqual(names["Mother's Day (Greece)"], date(2026, 5, 10))
        self.assertEqual(names["Black Friday"], date(2026, 11, 27))
        self.assertEqual(names["Ochi Day"], date(2026, 10, 28))
        up = ideas.upcoming(10, today=date(2026, 12, 20))
        self.assertEqual([h["name"][:9] for h in up], ["Christmas", "Christmas", "Boxing Da"])
        self.assertEqual(up[0]["in_days"], 4)

    def test_suggestions_and_page(self):
        page = self.client.get("/ideas").text
        self.assertIn("Next 60 days", page)
        self.assertIn("Draft a post", page)
        payload = {"ideas": [{"topic": "Tsiknopempti grill night", "why": "Carnival Thursday", "best_day": "2026-02-12", "format": "video"},
                             {"topic": "Staff story", "why": "people", "best_day": "", "format": "image"}, {"nope": 1}]}
        with mock.patch.object(providers, "text", return_value=json.dumps(payload)) as t:
            r = self.client.post("/ideas/suggest")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Tsiknopempti grill night", r.text)
        self.assertIn("Make a draft with AI", r.text)
        self.assertIn("Taverna", t.call_args.args[0])
        with mock.patch.object(providers, "text", side_effect=providers.QuotaError("quota")):
            r = self.client.post("/ideas/suggest")
        self.assertIn("Could not get ideas", r.text)


class CSRFTests(Base):
    def test_cookie_posts_need_the_token_when_enabled(self):
        pid = queries.create_draft("x", "c", "", user_id=self.uid)
        with mock.patch.dict(os.environ, {"CSRF_DISABLED": "0"}):
            r = self.client.get("/")                      # sets the csrf cookie
            token = r.cookies.get("csrf") or self.client.cookies.get("csrf")
            self.assertTrue(token)
            self.assertIn(f'window.CSRF = "{token}"', r.text)
            r = self.client.post(f"/posts/{pid}/reject", data={}, follow_redirects=False)
            self.assertEqual(r.status_code, 403)
            self.assertEqual(queries.get_post(pid)["status"], "draft")
            r = self.client.post(f"/posts/{pid}/reject", data={"csrf": token}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(queries.get_post(pid)["status"], "rejected")
            r = self.client.post(f"/posts/{pid}/unapprove", headers={"X-CSRF-Token": token}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            # Bearer API clients are not subject to it; login is exempt
            self.client.cookies.delete(auth.COOKIE_NAME)
            bearer = auth.create_token(self.uid)
            r = self.client.post(f"/api/posts/{pid}/status", json={"status": "rejected"},
                                 headers={"Authorization": f"Bearer {bearer}"})
            self.assertEqual(r.status_code, 200)
            # login is protected too (login CSRF), with the same cookie token
            r = self.client.post("/login", data={"email": "ops@example.com", "password": "password123"}, follow_redirects=False)
            self.assertEqual(r.status_code, 403)
            r = self.client.post("/login", data={"email": "ops@example.com", "password": "password123", "csrf": token},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303)

    def test_form_fields_reach_the_route_when_csrf_is_enabled(self):
        """Regression: the middleware used to consume the form body while reading the
        csrf field, so routes with Form() parameters saw an empty form."""
        pid = queries.create_draft("x", "c", "", user_id=self.uid)
        with mock.patch.dict(os.environ, {"CSRF_DISABLED": "0"}):
            r = self.client.get("/queue")
            token = self.client.cookies.get("csrf")
            self.assertIn(f'<input type="hidden" name="csrf" value="{token}">', r.text)   # server-rendered, no JS needed
            r = self.client.post(f"/posts/{pid}/approve", data={"csrf": token, "platforms": ["facebook", "instagram"],
                                                                "scheduled_time": "", "also_story": "1"},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertTrue(r.headers["location"].startswith("/queue?msg="), r.headers["location"])
            post = queries.get_post(pid)
            self.assertEqual((post["status"], post["platforms"], post["also_story"]), ("approved", "facebook,instagram", 1))
            csv = "topic,caption,hashtags,scheduled_time,platforms\nSunset,Hello,#a,,facebook\n"
            r = self.client.post("/posts/import", data={"csrf": token, "page_id": ""},
                                 files={"file": ("posts.csv", csv.encode(), "text/csv")}, follow_redirects=False)
            self.assertEqual(r.status_code, 303, r.text[:200])
            self.assertNotIn("err=", r.headers["location"])
            # edit form with text fields
            r = self.client.post(f"/posts/{pid}/edit", data={"csrf": token, "caption": "Edited", "hashtags": "#x", "topic": "x"},
                                 follow_redirects=False)
            self.assertIn(r.status_code, (303, 200))
            self.assertEqual(queries.get_post(pid)["caption"], "Edited")

    def test_multipart_token_in_query_and_oversize_guard(self):
        with mock.patch.dict(os.environ, {"CSRF_DISABLED": "0"}):
            self.client.get("/queue")
            token = self.client.cookies.get("csrf")
            csv = "topic,caption,hashtags,scheduled_time,platforms\nSunset,Hello,#a,,facebook\n"
            r = self.client.post(f"/posts/import?csrf={token}", data={"page_id": ""},
                                 files={"file": ("posts.csv", csv.encode(), "text/csv")}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            r = self.client.post("/posts/import", data={"page_id": ""}, headers={"content-length": str(200 * 1024 * 1024)},
                                 files={"file": ("posts.csv", csv.encode(), "text/csv")}, follow_redirects=False)
            self.assertEqual(r.status_code, 413)

    def test_clear_error_resets_the_retry_counter(self):
        pid = queries.create_draft("x", "c", "", user_id=self.uid)
        with get_connection() as conn:
            conn.execute("UPDATE posts SET last_error = 'boom', publish_attempts = 5 WHERE id = ?", (pid,))
        queries.clear_error(pid)
        post = queries.get_post(pid)
        self.assertEqual((post["last_error"], post["publish_attempts"]), (None, 0))


if __name__ == "__main__":
    unittest.main()
