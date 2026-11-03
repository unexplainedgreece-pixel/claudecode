"""Tests for src/video/audio_generator.py with fake OpenAI and ElevenLabs."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

import openai                                           # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src.video import audio_generator as ag             # noqa: E402

MP3 = b"ID3fake-mp3-bytes"


def insert_post(**overrides):
    row = {"topic": "t", "caption": "Hello Rhodes! Come for brunch. #rhodes #brunch", "status": "draft",
           "media_type": "video"}
    row.update(overrides)
    cols, params = ",".join(row), ",".join(f":{k}" for k in row)
    with get_connection() as conn:
        return conn.execute(f"INSERT INTO posts ({cols}) VALUES ({params})", row).lastrowid


def get_post(pid):
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def fake_openai(content=MP3, raise_exc=None):
    def create(**kw):
        if raise_exc:
            raise raise_exc
        return SimpleNamespace(content=content)
    return SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create)))


def openai_error(cls, message):
    resp = mock.MagicMock(status_code=429, headers={})
    return cls(message, response=resp, body={"error": {"message": message}})


class SpokenTextTests(unittest.TestCase):
    def test_strips_hashtags_links_emojis(self):
        post = {"caption": "Golden hour 🌅 on the harbour!\n\nBook at https://example.com #rhodes #sunset"}
        self.assertEqual(ag.spoken_text(post), "Golden hour on the harbour!\nBook at")

    def test_keeps_words_with_hash_inside(self):
        self.assertEqual(ag.spoken_text({"caption": "Room #3 is ready"}), "Room is ready")
        self.assertEqual(ag.spoken_text({"caption": "C#major chord"}), "C#major chord")

    def test_empty(self):
        self.assertEqual(ag.spoken_text({"caption": None}), "")
        self.assertEqual(ag.spoken_text({"caption": "#only #tags 🎉"}), "")

    def test_truncates(self):
        self.assertEqual(len(ag.spoken_text({"caption": "x" * 9000})), ag.MAX_TTS_CHARS)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "ELEVENLABS_API_KEY": "el-test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_openai_success_passes_instructions_for_gpt_models(self):
        captured = {}
        def create(**kw):
            captured.update(kw)
            return SimpleNamespace(content=MP3)
        client = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create)))
        with mock.patch.object(ag, "OpenAI", return_value=client), \
             mock.patch.object(ag, "OPENAI_TTS_MODEL", "gpt-4o-mini-tts"):
            self.assertEqual(ag.synthesize_openai("hi"), MP3)
        self.assertEqual(captured["response_format"], "mp3")
        self.assertIn("instructions", captured)
        captured.clear()
        with mock.patch.object(ag, "OpenAI", return_value=client), \
             mock.patch.object(ag, "OPENAI_TTS_MODEL", "tts-1"):
            ag.synthesize_openai("hi")
        self.assertNotIn("instructions", captured)   # tts-1 does not accept it

    def test_openai_quota_is_quota_error(self):
        err = openai_error(openai.RateLimitError, "insufficient_quota")
        with mock.patch.object(ag, "OpenAI", return_value=fake_openai(raise_exc=err)):
            with self.assertRaises(ag.QuotaError):
                ag.synthesize_openai("hi")

    def test_openai_other_errors(self):
        err = openai_error(openai.BadRequestError, "voice not found")
        with mock.patch.object(ag, "OpenAI", return_value=fake_openai(raise_exc=err)):
            with self.assertRaisesRegex(ag.AudioGenError, "refused"):
                ag.synthesize_openai("hi")
        with mock.patch.object(ag, "OpenAI", return_value=fake_openai(content=b"")):
            with self.assertRaisesRegex(ag.AudioGenError, "no audio"):
                ag.synthesize_openai("hi")

    def test_elevenlabs_success_and_errors(self):
        def ok(url, headers, json, timeout):
            self.assertEqual(headers["xi-api-key"], "el-test")
            self.assertIn("/text-to-speech/", url)
            return SimpleNamespace(status_code=200, content=MP3)
        with mock.patch.object(ag.requests, "post", side_effect=ok):
            self.assertEqual(ag.synthesize_elevenlabs("hi"), MP3)

        quota = SimpleNamespace(status_code=401, content=b"", text="",
                                json=lambda: {"detail": {"status": "quota_exceeded", "message": "out of characters"}})
        with mock.patch.object(ag.requests, "post", return_value=quota):
            with self.assertRaisesRegex(ag.QuotaError, "out of characters"):
                ag.synthesize_elevenlabs("hi")

        unauth = SimpleNamespace(status_code=401, content=b"", text="", json=lambda: {"detail": "bad key"})
        with mock.patch.object(ag.requests, "post", return_value=unauth):
            with self.assertRaisesRegex(ag.AudioGenError, "rejected the API key"):
                ag.synthesize_elevenlabs("hi")

        with mock.patch.object(ag.requests, "post", side_effect=ag.requests.ConnectionError("down")):
            with self.assertRaisesRegex(ag.AudioGenError, "Could not reach"):
                ag.synthesize_elevenlabs("hi")

    def test_fallback_on_quota_only(self):
        calls = []
        def primary(text):
            calls.append("openai")
            raise ag.QuotaError("quota")
        def secondary(text):
            calls.append("elevenlabs")
            return MP3
        with mock.patch.dict(ag.PROVIDERS, {"openai": primary, "elevenlabs": secondary}):
            data, used = ag.synthesize("hi", provider="openai", fallback="elevenlabs")
            self.assertEqual((data, used), (MP3, "elevenlabs"))
            self.assertEqual(calls, ["openai", "elevenlabs"])
            # no fallback configured -> the quota error surfaces
            with self.assertRaises(ag.QuotaError):
                ag.synthesize("hi", provider="openai", fallback="")

    def test_no_fallback_for_refusals(self):
        def primary(text):
            raise ag.AudioGenError("refused")
        secondary = mock.MagicMock(return_value=MP3)
        with mock.patch.dict(ag.PROVIDERS, {"openai": primary, "elevenlabs": secondary}):
            with self.assertRaises(ag.AudioGenError):
                ag.synthesize("hi", provider="openai", fallback="elevenlabs")
        secondary.assert_not_called()

    def test_unknown_provider(self):
        with self.assertRaises(ag.AudioGenError):
            ag.synthesize("hi", provider="siri")


class FlowTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
            mock.patch.object(ag, "AUDIO_DIR", Path(self.tmp.name)),
            mock.patch.object(ag, "TTS_PROVIDER", "openai"),
            mock.patch.object(ag, "TTS_FALLBACK", ""),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_generates_for_video_posts_only(self):
        video = insert_post()
        image = insert_post(media_type="image")
        done = insert_post(audio_path="/x/done.mp3")
        published = insert_post(status="published")
        with mock.patch.object(ag, "OpenAI", return_value=fake_openai()):
            self.assertEqual(ag.main([]), 0)
        post = get_post(video)
        self.assertTrue(post["audio_path"].endswith(f"post_{video}.mp3"))
        self.assertEqual(Path(post["audio_path"]).read_bytes(), MP3)
        self.assertIsNone(get_post(image)["audio_path"])
        self.assertEqual(get_post(done)["audio_path"], "/x/done.mp3")
        self.assertIsNone(get_post(published)["audio_path"])

    def test_quota_error_recorded_and_others_continue(self):
        bad = insert_post(caption="quota please")
        good = insert_post()
        def create(**kw):
            if "quota" in kw["input"]:
                raise openai_error(openai.RateLimitError, "insufficient_quota")
            return SimpleNamespace(content=MP3)
        client = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create)))
        with mock.patch.object(ag, "OpenAI", return_value=client):
            self.assertEqual(ag.main([]), 1)
        self.assertIn("quota", get_post(bad)["last_error"])
        self.assertIsNone(get_post(bad)["audio_path"])
        self.assertIsNotNone(get_post(good)["audio_path"])

    def test_missing_key_is_recorded(self):
        pid = insert_post()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "your_openai_key_here"}):
            self.assertEqual(ag.main([]), 1)
        self.assertIn("OPENAI_API_KEY is not set", get_post(pid)["last_error"])

    def test_empty_script_is_recorded(self):
        pid = insert_post(caption="#only #hashtags")
        with mock.patch.object(ag, "OpenAI", return_value=fake_openai()):
            self.assertEqual(ag.main([]), 1)
        self.assertIn("nothing to say", get_post(pid)["last_error"])

    def test_dry_run_and_by_id(self):
        pid = insert_post(media_type="image")   # not a video post, but --id forces it
        with mock.patch.object(ag, "OpenAI", return_value=fake_openai()):
            self.assertEqual(ag.main(["--dry-run"]), 0)
            self.assertIsNone(get_post(pid)["audio_path"])
            self.assertEqual(ag.main(["--id", str(pid)]), 0)
            self.assertIsNotNone(get_post(pid)["audio_path"])

    def test_unexpected_exception_is_contained(self):
        pid = insert_post()
        with mock.patch.object(ag, "synthesize", side_effect=RuntimeError("boom")):
            self.assertEqual(ag.main([]), 1)
        self.assertIn("unexpected error: boom", get_post(pid)["last_error"])


if __name__ == "__main__":
    unittest.main()
