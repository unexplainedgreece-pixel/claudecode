"""Tests for src/image_generator.py with the AI provider layer mocked."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from config.db import get_connection, init_db          # noqa: E402
from src import image_generator as ig                   # noqa: E402
from src.ai import providers                            # noqa: E402

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakeimagebytes"


def insert_post(**overrides):
    row = {"topic": "t", "caption": "c", "hashtags": "#h", "status": "draft",
           "image_prompt": "A sunset over Rhodes harbour", "image_url": None}
    row.update(overrides)
    cols, params = ",".join(row), ",".join(f":{k}" for k in row)
    with get_connection() as conn:
        return conn.execute(f"INSERT INTO posts ({cols}) VALUES ({params})", row).lastrowid


def get_post(pid):
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


class ImageGeneratorTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")
        self.tmp_images = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
            mock.patch.object(ig, "IMAGES_DIR", Path(self.tmp_images.name)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp_images.cleanup()

    def run_with(self, image_fn, argv=()):
        with mock.patch.object(providers, "image", side_effect=image_fn):
            return ig.main(list(argv))

    def test_generates_and_saves_local_file(self):
        pid = insert_post()
        seen = {}

        def fake(prompt, user_id=None, provider=None):
            seen["prompt"] = prompt
            return PNG_BYTES

        self.assertEqual(self.run_with(fake), 0)
        post = get_post(pid)
        self.assertTrue(post["image_path"].endswith(f"post_{pid}.png"))
        self.assertEqual(open(post["image_path"], "rb").read(), PNG_BYTES)
        self.assertIsNone(post["image_url"])                 # hosting is the uploader's job
        self.assertIsNone(post["last_error"])
        self.assertIn("Rhodes harbour", seen["prompt"])
        self.assertIn("no text or watermarks", seen["prompt"])

    def test_only_ai_drafts_missing_image_are_picked(self):
        has_url = insert_post(image_url="https://x/already.jpg")
        published = insert_post(status="published")
        no_prompt = insert_post(image_prompt="")
        upload = insert_post(source="upload")
        needs = insert_post()
        self.run_with(lambda *a, **k: PNG_BYTES)
        for pid in (has_url, published, no_prompt, upload):
            self.assertIsNone(get_post(pid)["image_path"], pid)
        self.assertIsNotNone(get_post(needs)["image_path"])

    def test_refusal_is_recorded_and_others_continue(self):
        bad = insert_post(image_prompt="something refused")
        good = insert_post()

        def fake(prompt, user_id=None, provider=None):
            if "refused" in prompt:
                raise providers.AIError("OpenAI refused the prompt: safety")
            return PNG_BYTES

        self.assertEqual(self.run_with(fake), 1)
        self.assertIsNone(get_post(bad)["image_path"])
        self.assertIn("refused the prompt", get_post(bad)["last_error"])
        self.assertIsNotNone(get_post(good)["image_path"])

    def test_quota_and_missing_key_are_recorded(self):
        a = insert_post()
        with mock.patch.object(providers, "image", side_effect=providers.QuotaError("free tier used up")):
            self.assertEqual(ig.main([]), 1)
        self.assertIn("Out of quota", get_post(a)["last_error"])
        b = insert_post()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "your_openai_key_here"}):
            self.assertEqual(ig.main(["--id", str(b)]), 1)
        self.assertIn("is not set", get_post(b)["last_error"])

    def test_dry_run_changes_nothing(self):
        pid = insert_post()
        self.assertEqual(self.run_with(lambda *a, **k: PNG_BYTES, ["--dry-run"]), 0)
        self.assertIsNone(get_post(pid)["image_path"])

    def test_by_id_regenerates_even_if_not_draft(self):
        pid = insert_post(status="scheduled")
        self.assertEqual(self.run_with(lambda *a, **k: PNG_BYTES, ["--id", str(pid)]), 0)
        self.assertIsNotNone(get_post(pid)["image_path"])

    def test_unexpected_exception_is_contained(self):
        pid = insert_post()
        with mock.patch.object(ig, "generate_image", side_effect=RuntimeError("boom")):
            self.assertEqual(ig.main([]), 1)
        self.assertIn("unexpected error: boom", get_post(pid)["last_error"])


if __name__ == "__main__":
    unittest.main()
