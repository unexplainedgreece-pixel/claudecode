"""TikTok: Login Kit connect flow with PKCE, token refresh, file upload to inbox / direct post."""
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ["SCHEDULER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import publisher, tiktok                        # noqa: E402
from src.security import crypto                         # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

KEY = crypto.generate_key()


class FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._p, self.status_code, self.text = payload, status, text

    def json(self):
        return self._p


class TikTokBase(unittest.TestCase):
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
            for t in ("posts", "users", "credentials", "pages"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY, "JWT_SECRET": "tt", "TIKTOK_CLIENT_KEY": "ck",
                                                "TIKTOK_CLIENT_SECRET": "cs"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.uid = auth.create_user("tt@example.com", "password123", "T")["id"]
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))

    def _connect(self, expires_in=86400, access="acc1"):
        tiktok._store(self.uid, tiktok._record_from_tokens(
            {"access_token": access, "expires_in": expires_in, "refresh_token": "ref1", "refresh_expires_in": 31536000,
             "open_id": "oid"}) | {"display_name": "unexplained.greece", "connected_at": "2026-11-04 10:00:00"})

class TikTokTests(TikTokBase):
    def test_auth_url_uses_hex_pkce_and_connect_flow_stores_tokens_encrypted(self):
        url = tiktok.auth_url(self.uid, "http://127.0.0.1:8000/", "st", "verifier123")
        self.assertIn("client_key=ck", url)
        self.assertIn("code_challenge=" + hashlib.sha256(b"verifier123").hexdigest(), url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("settings%2Ftiktok%2Fcallback%2F", url)
        self.assertIn("video.upload", url)
        self.assertNotIn("video.publish", url)        # only after TikTok approves direct posting
        r = self.client.post("/settings/tiktok/connect", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith(tiktok.AUTH_URL))
        cookies = r.headers.get_list("set-cookie")
        state = next(c for c in cookies if c.startswith("tt_state=")).split("=")[1].split(";")[0]
        verifier = next(c for c in cookies if c.startswith("tt_verifier=")).split("=")[1].split(";")[0]
        self.assertIn("code_challenge=" + tiktok.code_challenge(verifier), r.headers["location"])
        posted = {}

        def fake_post(url, data=None, timeout=None, headers=None):
            posted.update(data)
            return FakeResp({"access_token": "acc", "expires_in": 86400, "refresh_token": "ref",
                             "refresh_expires_in": 31536000, "open_id": "oid", "scope": tiktok.SCOPES})

        def fake_get(url, params=None, headers=None, timeout=None):
            return FakeResp({"data": {"user": {"display_name": "unexplained.greece"}}})
        with mock.patch.object(tiktok.requests, "post", side_effect=fake_post), \
             mock.patch.object(tiktok.requests, "get", side_effect=fake_get):
            self.client.cookies.set("tt_state", state)
            self.client.cookies.set("tt_verifier", verifier)
            r = self.client.get(f"/settings/tiktok/callback/?code=abc&state={state}", follow_redirects=False)
        self.assertIn("TikTok%20connected%3A%20unexplained.greece", r.headers["location"])
        self.assertEqual((posted["grant_type"], posted["code_verifier"], posted["client_key"]), ("authorization_code", verifier, "ck"))
        info = tiktok.connection(self.uid)
        self.assertEqual((info["refresh_token"], info["display_name"]), ("ref", "unexplained.greece"))
        with get_connection() as conn:
            raw = conn.execute("SELECT value_encrypted FROM credentials WHERE name = 'TT_CONNECTION'").fetchone()["value_encrypted"]
        self.assertTrue(raw.startswith("v1:"))
        self.assertIn("tiktok", publisher.configured_platforms(self.uid))
        self.assertIn("unexplained.greece", self.client.get("/settings").text)
        self.client.post("/settings/tiktok/disconnect", follow_redirects=False)
        self.assertIsNone(tiktok.connection(self.uid))
        self.assertNotIn("tiktok", publisher.configured_platforms(self.uid))

    def test_callback_rejects_bad_state_or_cancel(self):
        r = self.client.get("/settings/tiktok/callback?code=abc&state=wrong", follow_redirects=False)
        self.assertIn("security%20check", r.headers["location"])
        r = self.client.get("/settings/tiktok/callback?error=access_denied&error_description=User%20declined", follow_redirects=False)
        self.assertIn("cancelled", r.headers["location"])

    def test_access_token_refreshes_only_when_about_to_expire(self):
        self._connect(expires_in=3600)
        with mock.patch.object(tiktok.requests, "post") as post:
            self.assertEqual(tiktok.access_token(self.uid), "acc1")
        post.assert_not_called()
        self._connect(expires_in=60)
        with mock.patch.object(tiktok.requests, "post", return_value=FakeResp(
                {"access_token": "acc2", "expires_in": 86400, "refresh_token": "ref2", "refresh_expires_in": 31536000})) as post:
            self.assertEqual(tiktok.access_token(self.uid), "acc2")
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        info = tiktok.connection(self.uid)
        self.assertEqual((info["refresh_token"], info["display_name"]), ("ref2", "unexplained.greece"))
        with get_connection() as conn:
            conn.execute("DELETE FROM credentials")
        with self.assertRaises(tiktok.ConfigError):
            tiktok.access_token(self.uid)

    def test_chunk_plan(self):
        mb = 1024 * 1024
        self.assertEqual(tiktok.chunk_plan(3 * mb), (3 * mb, 1))
        self.assertEqual(tiktok.chunk_plan(64 * mb), (64 * mb, 1))
        self.assertEqual(tiktok.chunk_plan(150 * mb), (64 * mb, 2))     # 64 + 86 (remainder merged into the last)

    def _video(self, size=2048):
        fh = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        fh.write(b"v" * size)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return Path(fh.name)

    def test_inbox_upload_single_chunk(self):
        self._connect()
        calls, puts = [], []

        def fake_post(url, headers=None, json=None, timeout=None, data=None):
            calls.append((url, json))
            if url.endswith("/inbox/video/init/"):
                return FakeResp({"data": {"publish_id": "pub1", "upload_url": "https://up/x"}, "error": {"code": "ok"}})
            return FakeResp({"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}})

        def fake_put(url, data=None, headers=None, timeout=None):
            puts.append((url, len(data), headers))
            return FakeResp({}, 201)
        with mock.patch.object(tiktok.requests, "post", side_effect=fake_post), \
             mock.patch.object(tiktok.requests, "put", side_effect=fake_put):
            self.assertEqual(tiktok.upload_video(self.uid, self._video(), "Hello #rhodes"), "pub1")
        init = calls[0][1]["source_info"]
        self.assertEqual((init["source"], init["video_size"], init["chunk_size"], init["total_chunk_count"]), ("FILE_UPLOAD", 2048, 2048, 1))
        self.assertNotIn("post_info", calls[0][1])
        self.assertEqual(puts[0][2]["Content-Range"], "bytes 0-2047/2048")
        self.assertEqual(puts[0][2]["Content-Type"], "video/mp4")
        self.assertTrue(calls[1][0].endswith("/status/fetch/"))

    def test_direct_post_uses_creator_privacy_options_and_multi_chunk(self):
        self._connect()
        calls, puts = [], []

        def fake_post(url, headers=None, json=None, timeout=None, data=None):
            calls.append((url, json))
            if url.endswith("/creator_info/query/"):
                return FakeResp({"data": {"creator_username": "ug", "privacy_level_options": ["SELF_ONLY"],
                                          "max_video_post_duration_sec": 600}, "error": {"code": "ok"}})
            if url.endswith("/video/init/"):
                return FakeResp({"data": {"publish_id": "pub2", "upload_url": "https://up/y"}, "error": {"code": "ok"}})
            return FakeResp({"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}})

        def fake_put(url, data=None, headers=None, timeout=None):
            puts.append((len(data), headers["Content-Range"]))
            return FakeResp({}, 206 if len(puts) < 2 else 201)
        with mock.patch.object(tiktok, "MAX_CHUNK", 1000), mock.patch.object(tiktok, "PRIVACY", "PUBLIC_TO_EVERYONE"), \
             mock.patch.object(tiktok.requests, "post", side_effect=fake_post), \
             mock.patch.object(tiktok.requests, "put", side_effect=fake_put):
            self.assertEqual(tiktok.upload_video(self.uid, self._video(2500), "Hi", mode="direct"), "pub2")
        init = next(j for u, j in calls if u.endswith("/video/init/"))
        self.assertEqual(init["post_info"]["privacy_level"], "SELF_ONLY")      # unaudited: public not offered
        self.assertEqual((init["source_info"]["chunk_size"], init["source_info"]["total_chunk_count"]), (1000, 2))
        self.assertEqual(puts, [(1000, "bytes 0-999/2500"), (1500, "bytes 1000-2499/2500")])

    def test_api_errors_are_explained_and_failed_status_raises(self):
        self._connect()
        with mock.patch.object(tiktok.requests, "post", return_value=FakeResp(
                {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "nope"}}, 400)):
            with self.assertRaises(tiktok.TikTokError) as cm:
                tiktok.creator_info("acc1")
        self.assertIn("TIKTOK_POST_MODE=inbox", str(cm.exception))
        answers = [FakeResp({"data": {"publish_id": "p", "upload_url": "https://u"}, "error": {"code": "ok"}}),
                   FakeResp({"data": {"status": "FAILED", "fail_reason": "video_too_short"}, "error": {"code": "ok"}})]
        with mock.patch.object(tiktok.requests, "post", side_effect=answers), \
             mock.patch.object(tiktok.requests, "put", return_value=FakeResp({}, 201)):
            with self.assertRaises(tiktok.TikTokError) as cm:
                tiktok.upload_video(self.uid, self._video(), "x")
        self.assertIn("video_too_short", str(cm.exception))

    def test_publisher_routes_videos_through_the_connection(self):
        self._connect()
        video = self._video()
        pid = queries.create_draft("v", "Caption here", "#a", media_type="video", user_id=self.uid, platforms=["tiktok"])
        with get_connection() as conn:
            conn.execute("UPDATE posts SET video_path = ? WHERE id = ?", (str(video), pid))
        with mock.patch.object(tiktok, "upload_video", return_value="pub9") as up:
            self.assertEqual(publisher.publish_tiktok(queries.get_post(pid)), "pub9")
        self.assertEqual(up.call_args.args[1], video)
        self.assertEqual(up.call_args.args[2], "Caption here\n\n#a")
        with get_connection() as conn:
            conn.execute("UPDATE posts SET video_path = '/nope.mp4' WHERE id = ?", (pid,))
        with self.assertRaises(publisher.PublishError):
            publisher.publish_tiktok(queries.get_post(pid))


if __name__ == "__main__":
    unittest.main()


class StatusFollowUpTests(TikTokBase):
    def test_check_pending_records_inbox_and_failures(self):
        self._connect()
        with get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            a = conn.execute("INSERT INTO posts (user_id, topic, status, published_at, tiktok_publish_id) VALUES (?, 'a', 'published', ?, 'p1')",
                             (self.uid, now)).lastrowid
            b = conn.execute("INSERT INTO posts (user_id, topic, status, published_at, tiktok_publish_id) VALUES (?, 'b', 'published', ?, 'p2')",
                             (self.uid, now)).lastrowid
            old = conn.execute("INSERT INTO posts (user_id, topic, status, published_at, tiktok_publish_id, tiktok_status) "
                               "VALUES (?, 'c', 'published', ?, 'p3', 'PUBLISH_COMPLETE')", (self.uid, now)).lastrowid

        def fake_post(url, headers=None, json=None, timeout=None, data=None):
            pid = json["publish_id"]
            if pid == "p1":
                return FakeResp({"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
            return FakeResp({"data": {"status": "FAILED", "fail_reason": "video_too_short"}, "error": {"code": "ok"}})
        with mock.patch.object(tiktok.requests, "post", side_effect=fake_post) as post, mock.patch("src.notify.send") as sent:
            r = tiktok.check_pending(self.uid)
        self.assertEqual(r, {"checked": 2, "final": 2, "failed": 1})
        self.assertEqual(post.call_count, 2)                               # the finished one is not asked again
        self.assertEqual(queries.get_post(a)["tiktok_status"], "SEND_TO_USER_INBOX")
        self.assertIn("video_too_short", queries.get_post(b)["last_error"])
        sent.assert_called_once()
        with mock.patch.object(tiktok.requests, "post") as post:
            tiktok.check_pending(self.uid)
        post.assert_not_called()                                           # all final now
        html = self.client.get("/posts?status=published").text
        self.assertIn("TikTok 📥", html)
        self.assertIn("TikTok ✗", html)
        self.assertIn("in your TikTok inbox", self.client.get(f"/posts/{a}").text)
