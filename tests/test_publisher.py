"""
Tests for src/publisher.py using a fake HTTP layer.

Run from the project root:
    python3 -m unittest tests.test_publisher -v

No real network calls are made. A temporary database is used, so the real
data/scheduler.db is never touched.
"""

import os
import tempfile
import unittest
from unittest import mock

# Point the database at a temp file BEFORE importing the project modules.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from config.db import get_connection, init_db          # noqa: E402
from src import publisher                               # noqa: E402

REAL_ENV = {
    "META_ACCESS_TOKEN": "EAAtest",
    "META_PAGE_ID": "111",
    "META_INSTAGRAM_ACCOUNT_ID": "222",
    "TIKTOK_ACCESS_TOKEN": "act.test",
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._payload


def fake_api(fail=None):
    """Return a requests.request/requests.post replacement that fakes all three APIs."""
    fail = fail or set()

    def handler(method, url, **kw):
        if "graph.facebook.com" in url:
            if "/photos" in url or "/feed" in url:
                if "facebook" in fail:
                    return FakeResponse({"error": {"code": 190, "message": "Bad token"}}, 400)
                return FakeResponse({"id": "fb_1", "post_id": "111_999"})
            if url.endswith("/media"):
                if "instagram" in fail:
                    return FakeResponse({"error": {"code": 9004, "message": "Bad image URL"}}, 400)
                return FakeResponse({"id": "container_1"})
            if "container_1" in url and method == "GET":
                return FakeResponse({"status_code": "FINISHED"})
            if url.endswith("/media_publish"):
                return FakeResponse({"id": "ig_777"})
        if "tiktokapis.com" in url:
            if url.endswith("/init/"):
                if "tiktok" in fail:
                    return FakeResponse({"error": {"code": "access_token_invalid", "message": "expired"}}, 401)
                return FakeResponse({"data": {"publish_id": "tt_555"}, "error": {"code": "ok"}})
            if url.endswith("/status/fetch/"):
                return FakeResponse({"data": {"status": "PROCESSING_DOWNLOAD"}, "error": {"code": "ok"}})
        raise AssertionError(f"unexpected call {method} {url}")

    return handler


def insert_post(**overrides):
    row = {"topic": "t", "caption": "c", "hashtags": "#h", "image_prompt": "p",
           "status": "draft", "scheduled_time": None,
           "image_url": "https://example.com/a.jpg", "video_url": None}
    row.update(overrides)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO posts (topic,caption,hashtags,image_prompt,status,scheduled_time,"
            "image_url,video_url) VALUES (:topic,:caption,:hashtags,:image_prompt,:status,"
            ":scheduled_time,:image_url,:video_url)", row)
        return cur.lastrowid


def get_post(pid):
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def run(handler, argv=()):
    with mock.patch.dict(os.environ, REAL_ENV), \
         mock.patch("src.publisher.requests.request", side_effect=handler), \
         mock.patch("src.publisher.requests.post",
                    side_effect=lambda url, **kw: handler("POST", url, **kw)), \
         mock.patch("src.publisher.time.sleep"):
        return publisher.main(list(argv))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")

    def test_all_platforms_succeed(self):
        pid = insert_post()
        self.assertEqual(run(fake_api()), 0)
        post = get_post(pid)
        self.assertEqual(post["status"], "published")
        self.assertEqual(post["fb_post_id"], "111_999")
        self.assertEqual(post["ig_post_id"], "ig_777")
        self.assertEqual(post["tiktok_publish_id"], "tt_555")
        self.assertIsNone(post["last_error"])
        self.assertIsNotNone(post["published_at"])

    def test_partial_failure_then_retry(self):
        pid = insert_post()
        # First run: Instagram rejects the image. Facebook and TikTok succeed.
        self.assertEqual(run(fake_api(fail={"instagram"})), 1)
        post = get_post(pid)
        self.assertEqual(post["status"], "draft")          # not published yet
        self.assertEqual(post["fb_post_id"], "111_999")     # but FB id kept
        self.assertEqual(post["tiktok_publish_id"], "tt_555")
        self.assertIn("Bad image URL", post["last_error"])

        # Second run: everything works. Facebook/TikTok must NOT be called again.
        calls = []
        base = fake_api()

        def counting(method, url, **kw):
            calls.append(url)
            return base(method, url, **kw)

        self.assertEqual(run(counting), 0)
        self.assertFalse(any("/photos" in u or "/feed" in u for u in calls), "Facebook double-posted")
        self.assertFalse(any("/video/init/" in u or "/content/init/" in u for u in calls), "TikTok double-posted")
        self.assertEqual(get_post(pid)["status"], "published")

    def test_placeholder_token_is_clean_error(self):
        pid = insert_post()
        env = dict(REAL_ENV, TIKTOK_ACCESS_TOKEN="your_tiktok_token_here")
        with mock.patch.dict(os.environ, env), \
             mock.patch("src.publisher.requests.request", side_effect=fake_api()), \
             mock.patch("src.publisher.time.sleep"):
            self.assertEqual(publisher.main([]), 1)
        post = get_post(pid)
        self.assertEqual(post["status"], "draft")
        self.assertIn("TIKTOK_ACCESS_TOKEN is not set", post["last_error"])
        self.assertEqual(post["fb_post_id"], "111_999")

    def test_scheduled_posts_only_when_due(self):
        due = insert_post(status="scheduled", scheduled_time="2000-01-01 09:00")
        future = insert_post(status="scheduled", scheduled_time="2099-01-01 09:00")
        already = insert_post(status="published")
        run(fake_api())
        self.assertEqual(get_post(due)["status"], "published")
        self.assertEqual(get_post(future)["status"], "scheduled")
        self.assertIsNone(get_post(already)["fb_post_id"])

    def test_missing_media_is_reported_not_crashed(self):
        pid = insert_post(image_url=None)
        self.assertEqual(run(fake_api()), 1)
        post = get_post(pid)
        self.assertEqual(post["fb_post_id"], "111_999")     # text-only FB post is fine
        self.assertIsNone(post["ig_post_id"])
        self.assertIn("Instagram needs image_url", post["last_error"])
        self.assertIn("TikTok needs video_url or image_url", post["last_error"])

    def test_dry_run_changes_nothing(self):
        pid = insert_post()
        self.assertEqual(run(fake_api(), ["--dry-run"]), 0)
        post = get_post(pid)
        self.assertEqual(post["status"], "draft")
        self.assertIsNone(post["fb_post_id"])

    def test_platform_filter(self):
        pid = insert_post()
        self.assertEqual(run(fake_api(), ["--platforms", "facebook"]), 0)
        post = get_post(pid)
        self.assertEqual(post["status"], "published")
        self.assertIsNone(post["ig_post_id"])


if __name__ == "__main__":
    unittest.main()
