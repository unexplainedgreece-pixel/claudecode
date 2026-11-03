"""
Tests for src/image_generator.py with a fake OpenAI client and fake download.

Run from the project root:
    python3 -m unittest tests.test_image_generator -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

import openai                                           # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import image_generator as ig                   # noqa: E402

FAKE_URL = "https://oaidalleapiprodscus.blob.core.windows.net/private/img.png?sig=abc"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfakeimagebytes"


def fake_openai(url=FAKE_URL, b64=None, raise_exc=None):
    """A stand-in for OpenAI() whose images.generate returns a canned result."""
    def generate(**kwargs):
        if raise_exc:
            raise raise_exc
        return SimpleNamespace(data=[SimpleNamespace(url=url, b64_json=b64, revised_prompt="rp")])
    return SimpleNamespace(images=SimpleNamespace(generate=generate))


def fake_download(url, timeout=None):
    assert url == FAKE_URL
    return SimpleNamespace(content=PNG_BYTES, raise_for_status=lambda: None)


def insert_post(**overrides):
    row = {"topic": "t", "caption": "c", "hashtags": "#h", "status": "draft",
           "image_prompt": "A sunset over Rhodes harbour", "image_url": None}
    row.update(overrides)
    with get_connection() as conn:
        return conn.execute(
            "INSERT INTO posts (topic,caption,hashtags,status,image_prompt,image_url) "
            "VALUES (:topic,:caption,:hashtags,:status,:image_prompt,:image_url)", row
        ).lastrowid


def get_post(pid):
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def make_bad_request(message):
    resp = SimpleNamespace(status_code=400, headers={}, request=None)
    return openai.BadRequestError(message, response=mock.MagicMock(status_code=400, headers={}),
                                  body={"error": {"message": message}})


class ImageGeneratorTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")
        self.tmp_images = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
            mock.patch.object(ig, "IMAGES_DIR", ig.Path(self.tmp_images.name)),
            mock.patch.object(ig.requests, "get", side_effect=fake_download),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp_images.cleanup()

    def run_with(self, client, argv=()):
        with mock.patch.object(ig, "OpenAI", return_value=client):
            return ig.main(list(argv))

    def test_generates_and_saves_url_and_local_copy(self):
        pid = insert_post()
        self.assertEqual(self.run_with(fake_openai()), 0)
        post = get_post(pid)
        self.assertEqual(post["image_url"], FAKE_URL)
        self.assertTrue(post["image_path"].endswith(f"post_{pid}.png"))
        self.assertEqual(open(post["image_path"], "rb").read(), PNG_BYTES)
        self.assertIsNone(post["last_error"])
        self.assertEqual(post["status"], "draft")   # status untouched: publisher's job

    def test_only_drafts_missing_image_are_picked(self):
        has_image = insert_post(image_url="https://x/already.jpg")
        published = insert_post(status="published")
        no_prompt = insert_post(image_prompt="")
        needs = insert_post()
        self.run_with(fake_openai())
        self.assertEqual(get_post(has_image)["image_url"], "https://x/already.jpg")
        self.assertIsNone(get_post(published)["image_url"])
        self.assertIsNone(get_post(no_prompt)["image_url"])
        self.assertEqual(get_post(needs)["image_url"], FAKE_URL)

    def test_content_policy_refusal_is_recorded_not_crashed(self):
        bad = insert_post(image_prompt="something refused")
        good = insert_post()
        calls = {"n": 0}

        def generate(**kw):
            calls["n"] += 1
            if "refused" in kw["prompt"]:
                raise make_bad_request("Your request was rejected by the safety system")
            return SimpleNamespace(data=[SimpleNamespace(url=FAKE_URL, b64_json=None, revised_prompt="")])

        client = SimpleNamespace(images=SimpleNamespace(generate=generate))
        self.assertEqual(self.run_with(client), 1)
        self.assertIsNone(get_post(bad)["image_url"])
        self.assertIn("OpenAI refused the prompt", get_post(bad)["last_error"])
        self.assertEqual(get_post(good)["image_url"], FAKE_URL)   # other post still done
        self.assertEqual(calls["n"], 2)

    def test_download_failure_leaves_post_untouched(self):
        pid = insert_post()
        with mock.patch.object(ig.requests, "get",
                               side_effect=ig.requests.ConnectionError("boom")):
            self.assertEqual(self.run_with(fake_openai()), 1)
        post = get_post(pid)
        self.assertIsNone(post["image_url"])
        self.assertIn("download failed", post["last_error"])

    def test_base64_model_saves_file_but_no_url(self):
        pid = insert_post()
        import base64
        client = fake_openai(url=None, b64=base64.b64encode(PNG_BYTES).decode())
        self.assertEqual(self.run_with(client), 0)
        post = get_post(pid)
        self.assertIsNone(post["image_url"])
        self.assertEqual(open(post["image_path"], "rb").read(), PNG_BYTES)

    def test_placeholder_key_is_recorded_per_post(self):
        pid = insert_post()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "your_openai_key_here"}):
            self.assertEqual(ig.main([]), 1)
        self.assertIn("OPENAI_API_KEY is not set", get_post(pid)["last_error"])
        self.assertIsNone(get_post(pid)["image_url"])

    def test_dry_run_changes_nothing(self):
        pid = insert_post()
        self.assertEqual(self.run_with(fake_openai(), ["--dry-run"]), 0)
        self.assertIsNone(get_post(pid)["image_url"])

    def test_by_id_regenerates_even_if_not_draft(self):
        pid = insert_post(status="scheduled")
        self.assertEqual(self.run_with(fake_openai(), ["--id", str(pid)]), 0)
        self.assertEqual(get_post(pid)["image_url"], FAKE_URL)


if __name__ == "__main__":
    unittest.main()
