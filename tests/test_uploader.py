"""
Tests for src/uploader.py: real Pillow conversion, fake Imgur and fake R2.

Run from the project root:
    python3 -m unittest tests.test_uploader -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from PIL import Image                                   # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import uploader as up                          # noqa: E402

IMGUR_LINK = "https://i.imgur.com/AbC123.jpeg"


def make_png(path: Path, mode="RGBA", size=(64, 64)):
    color = (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)
    Image.new(mode, size, color).save(path, "PNG")
    return path


def insert_post(**overrides):
    row = {"topic": "t", "caption": "c", "status": "draft",
           "image_url": "https://openai.example/temp.png", "image_path": None, "hosted_at": None}
    row.update(overrides)
    cols, params = ",".join(row), ",".join(f":{k}" for k in row)
    with get_connection() as conn:
        return conn.execute(f"INSERT INTO posts ({cols}) VALUES ({params})", row).lastrowid


def get_post(pid):
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._payload


def imgur_ok(url, headers=None, files=None, data=None, timeout=None):
    assert headers["Authorization"] == "Client-ID abc123"
    assert files["image"][2] == "image/jpeg"
    return FakeResponse({"success": True, "status": 200,
                         "data": {"link": "http://i.imgur.com/AbC123.jpeg", "deletehash": "del999"}})


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rgba_png_becomes_rgb_jpeg_on_white(self):
        png = make_png(self.dir / "a.png", "RGBA")
        jpg = up.convert_to_jpeg(png)
        self.assertEqual(jpg.suffix, ".jpg")
        with Image.open(jpg) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.mode, "RGB")
            r, g, b = img.getpixel((10, 10))
            self.assertGreater(r, 200)      # half-transparent red over white -> light red
            self.assertGreater(g, 100)

    def test_rgb_png_converts(self):
        jpg = up.convert_to_jpeg(make_png(self.dir / "b.png", "RGB"))
        with Image.open(jpg) as img:
            self.assertEqual(img.format, "JPEG")

    def test_missing_file(self):
        with self.assertRaises(up.UploadError) as cm:
            up.convert_to_jpeg(self.dir / "nope.png")
        self.assertIn("not found", str(cm.exception))

    def test_corrupt_file(self):
        bad = self.dir / "bad.png"
        bad.write_bytes(b"this is not an image")
        with self.assertRaises(up.UploadError) as cm:
            up.convert_to_jpeg(bad)
        self.assertIn("not a valid image", str(cm.exception))


class UploaderFlowTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"IMGUR_CLIENT_ID": "abc123"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_imgur(self, handler=imgur_ok, argv=()):
        with mock.patch.object(up, "MEDIA_HOST", "imgur"), \
             mock.patch.object(up.requests, "post", side_effect=handler):
            return up.main(list(argv))

    def test_full_flow_overwrites_image_url(self):
        png = make_png(self.dir / "post_1.png")
        pid = insert_post(image_path=str(png))
        self.assertEqual(self.run_imgur(), 0)
        post = get_post(pid)
        self.assertEqual(post["image_url"], IMGUR_LINK)          # https enforced
        self.assertTrue(post["jpeg_path"].endswith("post_1.jpg"))
        self.assertTrue(Path(post["jpeg_path"]).exists())
        self.assertEqual(post["host_ref"], "del999")
        self.assertIsNotNone(post["hosted_at"])
        self.assertIsNone(post["last_error"])
        self.assertEqual(post["status"], "draft")

    def test_only_unhosted_posts_are_picked(self):
        png = make_png(self.dir / "p.png")
        hosted = insert_post(image_path=str(png), hosted_at="2026-01-01 10:00:00",
                             image_url="https://i.imgur.com/old.jpeg")
        no_image = insert_post(image_path=None)
        published = insert_post(image_path=str(png), status="published")
        needs = insert_post(image_path=str(png))
        self.run_imgur()
        self.assertEqual(get_post(hosted)["image_url"], "https://i.imgur.com/old.jpeg")
        self.assertIsNone(get_post(no_image)["hosted_at"])
        self.assertIsNone(get_post(published)["hosted_at"])
        self.assertEqual(get_post(needs)["image_url"], IMGUR_LINK)

    def test_imgur_error_is_recorded_and_url_untouched(self):
        png = make_png(self.dir / "p.png")
        pid = insert_post(image_path=str(png))

        def imgur_fail(url, **kw):
            return FakeResponse({"success": False, "status": 400,
                                 "data": {"error": "File type invalid"}}, 400)

        self.assertEqual(self.run_imgur(imgur_fail), 1)
        post = get_post(pid)
        self.assertEqual(post["image_url"], "https://openai.example/temp.png")
        self.assertIsNone(post["hosted_at"])
        self.assertIn("File type invalid", post["last_error"])

    def test_rate_limit_message(self):
        pid = insert_post(image_path=str(make_png(self.dir / "p.png")))
        self.run_imgur(lambda url, **kw: FakeResponse({"success": False}, 429))
        self.assertIn("rate limit", get_post(pid)["last_error"])

    def test_placeholder_client_id_is_clean_error(self):
        pid = insert_post(image_path=str(make_png(self.dir / "p.png")))
        with mock.patch.dict(os.environ, {"IMGUR_CLIENT_ID": "your_imgur_client_id_here"}):
            self.assertEqual(self.run_imgur(), 1)
        self.assertIn("IMGUR_CLIENT_ID is not set", get_post(pid)["last_error"])

    def test_corrupt_image_does_not_stop_other_posts(self):
        bad = self.dir / "bad.png"
        bad.write_bytes(b"nope")
        bad_id = insert_post(image_path=str(bad))
        good_id = insert_post(image_path=str(make_png(self.dir / "good.png")))
        self.assertEqual(self.run_imgur(), 1)
        self.assertIn("not a valid image", get_post(bad_id)["last_error"])
        self.assertEqual(get_post(good_id)["image_url"], IMGUR_LINK)

    def test_dry_run_changes_nothing(self):
        pid = insert_post(image_path=str(make_png(self.dir / "p.png")))
        self.assertEqual(self.run_imgur(argv=["--dry-run"]), 0)
        self.assertIsNone(get_post(pid)["hosted_at"])
        self.assertFalse((self.dir / "p.jpg").exists())

    def test_unknown_host(self):
        with mock.patch.object(up, "MEDIA_HOST", "dropbox"):
            self.assertEqual(up.main([]), 2)

    def test_video_post_uploads_mp4_via_r2(self):
        mp4 = self.dir / "post_7.mp4"
        mp4.write_bytes(b"\x00" * 4096)
        pid = insert_post(media_type="video", video_path=str(mp4), image_path=str(make_png(self.dir / "p.png")))
        env = {"R2_ACCOUNT_ID": "acc", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
               "R2_BUCKET": "media", "R2_PUBLIC_URL": "https://media.apexdigital.gr"}
        fake_client = mock.MagicMock()
        with mock.patch.dict(os.environ, env), mock.patch.object(up, "MEDIA_HOST", "r2"), \
             mock.patch("boto3.client", return_value=fake_client):
            self.assertEqual(up.main([]), 0)
        put = fake_client.put_object.call_args.kwargs
        self.assertEqual(put["ContentType"], "video/mp4")
        self.assertTrue(put["Key"].endswith(".mp4"))
        post = get_post(pid)
        self.assertTrue(post["video_url"].endswith(".mp4"))
        self.assertEqual(post["image_url"], "https://openai.example/temp.png")   # image untouched
        self.assertIsNotNone(post["hosted_at"])
        self.assertFalse((self.dir / "p.jpg").exists())                         # no JPEG conversion

    def test_video_post_on_imgur_is_refused_clearly(self):
        mp4 = self.dir / "post_8.mp4"
        mp4.write_bytes(b"x" * 100)
        pid = insert_post(media_type="video", video_path=str(mp4))
        self.assertEqual(self.run_imgur(), 1)
        self.assertIn("MEDIA_HOST=r2", get_post(pid)["last_error"])
        self.assertIsNone(get_post(pid)["video_url"])

    def test_video_post_without_render_is_skipped(self):
        pid = insert_post(media_type="video", image_path=str(make_png(self.dir / "p.png")))
        self.assertEqual(self.run_imgur(), 0)      # not picked up at all: nothing to host yet
        self.assertIsNone(get_post(pid)["hosted_at"])
        self.assertEqual(self.run_imgur(argv=["--id", str(pid)]), 1)
        self.assertIn("no video_path", get_post(pid)["last_error"])

    def test_r2_backend(self):
        pid = insert_post(image_path=str(make_png(self.dir / "p.png")))
        env = {"R2_ACCOUNT_ID": "acc", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
               "R2_BUCKET": "media", "R2_PUBLIC_URL": "https://media.apexdigital.gr/"}
        fake_client = mock.MagicMock()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(up, "MEDIA_HOST", "r2"), \
             mock.patch("boto3.client", return_value=fake_client) as boto_client:
            self.assertEqual(up.main([]), 0)
        boto_client.assert_called_once()
        self.assertEqual(boto_client.call_args.kwargs["endpoint_url"],
                         "https://acc.r2.cloudflarestorage.com")
        put = fake_client.put_object.call_args.kwargs
        self.assertEqual(put["Bucket"], "media")
        self.assertEqual(put["ContentType"], "image/jpeg")
        post = get_post(pid)
        self.assertTrue(post["image_url"].startswith(f"https://media.apexdigital.gr/posts/post_{pid}_"))
        self.assertTrue(post["image_url"].endswith(".jpg"))
        self.assertEqual(post["host_ref"], put["Key"])


if __name__ == "__main__":
    unittest.main()
