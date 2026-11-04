"""Custom video upload: validation, tier gating, storage, and the pipeline skipping AI steps."""

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import image_generator, uploader               # noqa: E402
from src.video import audio_generator, video_compiler   # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def real_mp4(path: Path) -> Path:
    from PIL import Image
    from moviepy import ImageClip
    png = path.with_suffix(".png")
    Image.new("RGB", (48, 48), (10, 200, 90)).save(png)
    ImageClip(str(png)).with_duration(0.4).write_videofile(str(path), fps=10, codec="libx264",
                                                           audio=False, logger=None)
    return path


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(webapp.app)
        cls.client.__enter__()
        cls.media = tempfile.TemporaryDirectory()
        cls.mp4 = real_mp4(Path(cls.media.name) / "real.mp4") if HAVE_FFMPEG else None

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.media.cleanup()

    def setUp(self):
        init_db()
        with get_connection() as conn:
            for t in ("posts", "users"):
                conn.execute(f"DELETE FROM {t}")
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = mock.patch.object(webapp, "UPLOAD_DIR", Path(self.tmp.name))
        self.upload_dir.start()
        self.user = auth.create_user("up@example.com", "password123")
        self.set_tier("pro")
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.user["id"]))

    def tearDown(self):
        self.upload_dir.stop()
        self.tmp.cleanup()

    def set_tier(self, tier):
        with get_connection() as conn:
            conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, self.user["id"]))

    def upload(self, name="clip.mp4", content=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200,
               ctype="video/mp4", **fields):
        return self.client.post("/api/posts/upload", data=fields,
                                files={"file": (name, io.BytesIO(content), ctype)})

    def test_requires_login(self):
        self.client.cookies.clear()
        self.assertEqual(self.upload().status_code, 401)

    def test_free_tier_is_refused_with_402_and_upgrade_url(self):
        self.set_tier("free")
        r = self.upload()
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["detail"]["code"], "no_uploads")
        self.assertEqual(r.json()["detail"]["upgrade_url"], "/billing")
        self.assertEqual(queries.list_posts(), [])

    def test_rejects_wrong_extension_and_type(self):
        self.assertEqual(self.upload(name="notes.txt", ctype="text/plain").status_code, 400)
        self.assertEqual(self.upload(name="clip.exe", ctype="application/x-msdownload").status_code, 400)
        self.assertEqual(self.upload(name="clip.mp4", ctype="image/png").status_code, 400)
        self.assertEqual(self.upload(content=b"").status_code, 400)
        self.assertEqual(queries.list_posts(), [])
        self.assertEqual([p for p in Path(self.tmp.name).rglob("*") if p.is_file()], [])   # nothing left on disk

    def test_too_large_is_413_and_file_removed(self):
        with mock.patch.object(webapp, "MAX_UPLOAD_MB", 1):
            r = self.upload(content=b"\x00" * (1024 * 1024 + 10))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(list(Path(self.tmp.name).rglob("*.mp4")), [])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_fake_video_bytes_are_rejected_by_ffprobe(self):
        r = self.upload(content=b"definitely not a video" * 50)
        self.assertEqual(r.status_code, 400)
        self.assertIn("video stream", r.json()["detail"])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_real_video_creates_an_upload_post(self):
        r = self.client.post("/api/posts/upload",
                             data={"topic": "Summer", "caption": "Our reel", "platforms": ["instagram", "tiktok"]},
                             files={"file": ("Summer Reel.mp4", io.BytesIO(self.mp4.read_bytes()), "video/mp4")})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        post = queries.get_post(body["id"])
        self.assertEqual(post["platforms"], "instagram,tiktok")
        self.assertEqual(post["source"], "upload")
        self.assertEqual(post["media_type"], "video")
        self.assertEqual(post["status"], "draft")
        self.assertEqual(post["user_id"], self.user["id"])
        self.assertEqual(post["upload_name"], "Summer Reel.mp4")
        self.assertEqual(post["topic"], "Summer")
        self.assertTrue(Path(post["video_path"]).exists())
        self.assertTrue(Path(post["video_path"]).parent.name == f"user_{self.user['id']}")
        self.assertEqual(body["url"], f"/posts/{post['id']}")
        self.assertEqual(self.client.get(f"/files/{post['id']}/video").status_code, 200)
        self.assertIn("your upload", self.client.get("/queue").text)

    def test_topic_defaults_to_filename(self):
        with mock.patch.object(webapp, "_probe_is_video", lambda p: True):
            r = self.upload(name="taverna-night.mov")
        self.assertEqual(queries.get_post(r.json()["id"])["topic"], "taverna-night")

    def test_monthly_post_limit_applies_to_uploads(self):
        for _ in range(50):
            queries.create_draft("x", user_id=self.user["id"])
        with mock.patch.object(webapp, "_probe_is_video", lambda p: True):
            r = self.upload()
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["detail"]["code"], "posts_limit")

    def test_uploaded_posts_skip_ai_steps_but_reach_hosting(self):
        with mock.patch.object(webapp, "_probe_is_video", lambda p: True):
            pid = self.upload().json()["id"]
        self.assertEqual(image_generator.fetch_posts_needing_images(), [])
        self.assertEqual(audio_generator.fetch_posts_needing_audio(), [])
        self.assertEqual(video_compiler.fetch_posts_needing_video(), [])
        self.assertEqual([p["id"] for p in uploader.fetch_posts_needing_hosting()], [pid])
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["awaiting_image"], 0)
        self.assertEqual(stats["awaiting_hosting"], 1)
        self.assertEqual(stats["videos_pending"], 0)

    def test_dashboard_shows_dropzone_only_when_plan_allows(self):
        self.assertIn('id="dropzone"', self.client.get("/").text)
        self.set_tier("free")
        page = self.client.get("/").text
        self.assertNotIn('id="dropzone"', page)
        self.assertIn("Pro and Enterprise", page)


if __name__ == "__main__":
    unittest.main()
