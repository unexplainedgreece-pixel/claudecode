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
from pathlib import Path
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
            if "/photos" in url or "/feed" in url or "/videos" in url:
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
           "status": "approved", "scheduled_time": None,
           "image_url": "https://example.com/a.jpg", "video_url": None}
    row.update(overrides)
    cols, params = ",".join(row), ",".join(f":{k}" for k in row)
    with get_connection() as conn:
        return conn.execute(f"INSERT INTO posts ({cols}) VALUES ({params})", row).lastrowid


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
        self.assertEqual(post["status"], "approved")       # not published yet
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
        self.assertEqual(post["status"], "approved")
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

    def test_drafts_are_never_published(self):
        draft = insert_post(status="draft")
        rejected = insert_post(status="rejected")
        self.assertEqual(run(fake_api()), 0)
        self.assertEqual(get_post(draft)["status"], "draft")
        self.assertIsNone(get_post(draft)["fb_post_id"])
        self.assertEqual(get_post(rejected)["status"], "rejected")

    def test_dry_run_changes_nothing(self):
        pid = insert_post()
        self.assertEqual(run(fake_api(), ["--dry-run"]), 0)
        post = get_post(pid)
        self.assertEqual(post["status"], "approved")
        self.assertIsNone(post["fb_post_id"])

    def test_video_post_goes_out_as_fb_video_and_ig_reel(self):
        pid = insert_post(video_url="https://media.example.com/posts/post_1.mp4")
        calls = []
        base = fake_api()

        def recording(method, url, **kw):
            calls.append((method, url, kw.get("data") or {}))
            return base(method, url, **kw)

        self.assertEqual(run(recording), 0)
        fb = next(c for c in calls if "/111/videos" in c[1])
        self.assertEqual(fb[2]["file_url"], "https://media.example.com/posts/post_1.mp4")
        self.assertIn("description", fb[2])
        ig = next(c for c in calls if c[1].endswith("/222/media"))
        self.assertEqual(ig[2]["media_type"], "REELS")
        self.assertEqual(ig[2]["video_url"], "https://media.example.com/posts/post_1.mp4")
        self.assertNotIn("image_url", ig[2])
        tt = next(c for c in calls if "/video/init/" in c[1])
        self.assertTrue(tt)
        self.assertEqual(get_post(pid)["status"], "published")

    def test_local_video_is_uploaded_directly_to_facebook(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4 = Path(tmp) / "clip.mp4"
            mp4.write_bytes(b"\x00" * 100)
            pid = insert_post(video_path=str(mp4), video_url=None, image_url=None, platforms="facebook",
                              media_type="video")
            calls = []
            base = fake_api()

            def recording(method, url, **kw):
                calls.append((url, kw))
                return base(method, url, **kw)

            self.assertEqual(run(recording), 0)
            url, kw = next(c for c in calls if "/111/videos" in c[0])
            self.assertIn("source", kw["files"])
            self.assertEqual(kw["files"]["source"][0], "clip.mp4")
            self.assertNotIn("file_url", kw["data"])
            self.assertEqual(kw["timeout"], publisher.UPLOAD_TIMEOUT)
            self.assertTrue(kw["files"]["source"][1].closed)
            self.assertEqual(get_post(pid)["status"], "published")

    def test_local_image_is_uploaded_directly_to_facebook(self):
        with tempfile.TemporaryDirectory() as tmp:
            jpg = Path(tmp) / "post.jpg"
            jpg.write_bytes(b"\xff\xd8" * 50)
            pid = insert_post(jpeg_path=str(jpg), image_url=None, platforms="facebook")
            calls = []
            base = fake_api()

            def recording(method, url, **kw):
                calls.append((url, kw))
                return base(method, url, **kw)

            self.assertEqual(run(recording), 0)
            url, kw = next(c for c in calls if "/111/photos" in c[0])
            self.assertEqual(kw["files"]["source"][0], "post.jpg")
            self.assertEqual(get_post(pid)["status"], "published")

    def test_empty_text_post_is_refused_and_instagram_explains_hosting(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4 = Path(tmp) / "clip.mp4"
            mp4.write_bytes(b"\x00" * 100)
            empty = insert_post(caption="", hashtags="", image_url=None, platforms="facebook")
            ig_video = insert_post(video_path=str(mp4), image_url=None, platforms="instagram", media_type="video")
            self.assertEqual(run(fake_api()), 1)
            self.assertIn("Nothing to post", get_post(empty)["last_error"])
            self.assertIn("hosted video link", get_post(ig_video)["last_error"])
            self.assertIn("MEDIA_HOST=r2", get_post(ig_video)["last_error"])

    def test_post_platform_choice_is_honoured(self):
        fb_only = insert_post(platforms="facebook")
        both = insert_post(platforms="instagram,tiktok")
        calls = []
        base = fake_api()

        def recording(method, url, **kw):
            calls.append(url)
            return base(method, url, **kw)

        self.assertEqual(run(recording), 0)
        a, b = get_post(fb_only), get_post(both)
        self.assertEqual(a["status"], "published")
        self.assertEqual(a["fb_post_id"], "111_999")
        self.assertIsNone(a["ig_post_id"])
        self.assertIsNone(a["tiktok_publish_id"])
        self.assertEqual(b["status"], "published")
        self.assertIsNone(b["fb_post_id"])
        self.assertEqual(b["ig_post_id"], "ig_777")
        self.assertEqual(b["tiktok_publish_id"], "tt_555")
        # unconfigured TikTok no longer blocks a Facebook-only post
        c = insert_post(platforms="facebook")
        with mock.patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": "your_tiktok_token_here"}):
            self.assertEqual(run(fake_api(), ["--id", str(c)]), 0)
        self.assertEqual(get_post(c)["status"], "published")

    def test_platform_filter_intersects_with_post_choice(self):
        pid = insert_post(platforms="instagram")
        self.assertEqual(run(fake_api(), ["--platforms", "facebook"]), 1)   # nothing to do for it
        self.assertEqual(get_post(pid)["status"], "approved")
        self.assertIsNone(get_post(pid)["fb_post_id"])

    def test_parse_platforms(self):
        self.assertEqual(publisher.parse_platforms(" TikTok, facebook ,bogus"), ["facebook", "tiktok"])
        self.assertEqual(publisher.parse_platforms(["instagram"]), ["instagram"])
        self.assertEqual(publisher.parse_platforms(None), [])

    def test_configured_platforms(self):
        with mock.patch.dict(os.environ, REAL_ENV):
            self.assertEqual(publisher.configured_platforms(), ["facebook", "instagram", "tiktok"])
        with mock.patch.dict(os.environ, dict(REAL_ENV, TIKTOK_ACCESS_TOKEN="your_tiktok_token_here")):
            self.assertEqual(publisher.configured_platforms(), ["facebook", "instagram"])

    def test_claimed_post_is_skipped_until_claim_is_stale(self):
        from datetime import datetime, timedelta
        fresh = insert_post()
        stale = insert_post()
        with get_connection() as conn:
            conn.execute("UPDATE posts SET claimed_at = ? WHERE id = ?",
                         (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), fresh))
            conn.execute("UPDATE posts SET claimed_at = ? WHERE id = ?",
                         ((datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"), stale))
        run(fake_api())
        self.assertEqual(get_post(fresh)["status"], "approved")   # left alone
        self.assertIsNone(get_post(fresh)["fb_post_id"])
        self.assertEqual(get_post(stale)["status"], "published")  # stale claim taken over
        self.assertIsNone(get_post(stale)["claimed_at"])           # released afterwards

    def test_claim_is_released_after_failure(self):
        pid = insert_post()
        run(fake_api(fail={"instagram"}))
        post = get_post(pid)
        self.assertIsNone(post["claimed_at"])
        self.assertEqual(post["status"], "approved")

    def test_platform_filter(self):
        pid = insert_post()
        self.assertEqual(run(fake_api(), ["--platforms", "facebook"]), 0)
        post = get_post(pid)
        self.assertEqual(post["status"], "published")
        self.assertIsNone(post["ig_post_id"])


if __name__ == "__main__":
    unittest.main()
