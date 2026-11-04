"""Tests for src/ai/providers.py: Gemini, OpenAI and Cloudflare behind fakes."""

import base64
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from config.db import init_db                           # noqa: E402
from src.ai import providers as ai                      # noqa: E402


class FakeResp:
    def __init__(self, payload=None, status=200, content=b"", ctype="application/json"):
        self._p, self.status_code, self.content = payload, status, content
        self.headers = {"content-type": ctype}
        self.text = ""

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def gemini_ok(text):
    return FakeResp({"candidates": [{"content": {"parts": [{"text": text}]}}]})


class PickTests(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_defaults_prefer_free_providers(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g", "OPENAI_API_KEY": "o", "CF_API_TOKEN": "c",
                                          "R2_ACCOUNT_ID": "acc"}, clear=False):
            self.assertEqual(ai.pick("text", None), "gemini")
            self.assertEqual(ai.pick("vision", None), "gemini")
            self.assertEqual(ai.pick("image", None), "cloudflare")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "your_gemini_key_here", "OPENAI_API_KEY": "o",
                                          "CF_API_TOKEN": ""}):
            self.assertEqual(ai.pick("text", None), "openai")
            self.assertEqual(ai.pick("image", None), "openai")
        with mock.patch.dict(os.environ, {"TEXT_PROVIDER": "openai", "GEMINI_API_KEY": "g"}):
            self.assertEqual(ai.pick("text", None), "openai")


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "gem-key"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_text_json_mode_and_key(self):
        with mock.patch.object(ai.requests, "post", return_value=gemini_ok('{"a": 1}')) as post:
            self.assertEqual(ai.text("hello", json_mode=True, provider="gemini"), '{"a": 1}')
        kw = post.call_args.kwargs
        self.assertEqual(kw["params"]["key"], "gem-key")
        self.assertEqual(kw["json"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("gemini-2.5-flash:generateContent", post.call_args.args[0])

    def test_vision_sends_inline_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "a.png"
            img.write_bytes(b"\x89PNGdata")
            with mock.patch.object(ai.requests, "post", return_value=gemini_ok("a harbour")) as post:
                self.assertEqual(ai.vision("describe", [img], provider="gemini"), "a harbour")
            parts = post.call_args.kwargs["json"]["contents"][0]["parts"]
            self.assertEqual(parts[0], {"text": "describe"})
            self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
            self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), b"\x89PNGdata")
        with self.assertRaises(ai.AIError):
            ai.vision("x", [], provider="gemini")

    def test_errors(self):
        with mock.patch.object(ai.requests, "post", return_value=FakeResp({"error": {"message": "quota"}}, 429)):
            with self.assertRaises(ai.QuotaError):
                ai.text("x", provider="gemini")
        with mock.patch.object(ai.requests, "post", return_value=FakeResp({"error": {"message": "bad key"}}, 403)):
            with self.assertRaisesRegex(ai.AIError, "rejected the API key"):
                ai.text("x", provider="gemini")
        with mock.patch.object(ai.requests, "post", return_value=FakeResp({"candidates": [{"finishReason": "SAFETY"}]})):
            with self.assertRaisesRegex(ai.AIError, "SAFETY"):
                ai.text("x", provider="gemini")
        with mock.patch.object(ai.requests, "post", side_effect=ai.requests.ConnectionError("down")):
            with self.assertRaisesRegex(ai.AIError, "Could not reach"):
                ai.text("x", provider="gemini")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "your_gemini_key_here"}):
            from src.security.secrets import ConfigError
            with self.assertRaises(ConfigError):
                ai.text("x", provider="gemini")

    def test_image(self):
        payload = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png",
                                                                          "data": base64.b64encode(b"PNG!").decode()}}]}}]}
        with mock.patch.object(ai.requests, "post", return_value=FakeResp(payload)) as post:
            self.assertEqual(ai.image("a cat", provider="gemini"), b"PNG!")
        self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["responseModalities"], ["IMAGE"])
        with mock.patch.object(ai.requests, "post", return_value=gemini_ok("sorry, text only")):
            with self.assertRaisesRegex(ai.AIError, "no image"):
                ai.image("a cat", provider="gemini")


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CF_API_TOKEN": "cf-tok", "R2_ACCOUNT_ID": "acc123"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_binary_and_json_responses(self):
        with mock.patch.object(ai.requests, "post", return_value=FakeResp(content=b"\xff\xd8jpeg", ctype="image/jpeg")) as post:
            self.assertEqual(ai.image("a beach", provider="cloudflare"), b"\xff\xd8jpeg")
        self.assertIn("/accounts/acc123/ai/run/@cf/black-forest-labs/flux-1-schnell", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer cf-tok")
        payload = {"success": True, "result": {"image": base64.b64encode(b"PNG!").decode()}}
        with mock.patch.object(ai.requests, "post", return_value=FakeResp(payload)):
            self.assertEqual(ai.image("a beach", provider="cloudflare"), b"PNG!")

    def test_errors(self):
        with mock.patch.object(ai.requests, "post", return_value=FakeResp({"success": False}, 429)):
            with self.assertRaises(ai.QuotaError):
                ai.image("x", provider="cloudflare")
        with mock.patch.object(ai.requests, "post",
                               return_value=FakeResp({"success": False, "errors": [{"message": "bad token"}]}, 401)):
            with self.assertRaisesRegex(ai.AIError, "bad token"):
                ai.image("x", provider="cloudflare")


class OpenAITests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_text_and_vision(self):
        created = {}

        def create(**kw):
            created.update(kw)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" {\"ok\": true} "))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch("openai.OpenAI", return_value=client):
            self.assertEqual(ai.text("hi", json_mode=True, provider="openai"), '{"ok": true}')
            self.assertEqual(created["response_format"], {"type": "json_object"})
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp) / "a.jpg"
                img.write_bytes(b"JPG")
                ai.vision("look", [img], provider="openai")
            self.assertEqual(created["messages"][0]["content"][1]["type"], "image_url")

    def test_error_mapping(self):
        import openai

        def boom(**kw):
            raise openai.RateLimitError("insufficient_quota", response=mock.MagicMock(status_code=429, headers={}),
                                        body=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
        with mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(ai.QuotaError):
                ai.text("hi", provider="openai")

    def test_unknown_provider(self):
        with self.assertRaises(ai.AIError):
            ai.text("x", provider="skynet")


if __name__ == "__main__":
    unittest.main()
