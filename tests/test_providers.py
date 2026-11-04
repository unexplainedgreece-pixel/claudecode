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
os.environ["CSRF_DISABLED"] = "1"

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
        self.assertIn("gemini-3.8-flash:generateContent", post.call_args.args[0])

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


class GeminiModelSwitchTests(unittest.TestCase):
    """Google retires model names; the tool follows Google's hint or the model list."""

    def setUp(self):
        from config.db import init_db
        init_db()
        ai._MODEL_SWITCH.clear()
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(ai._MODEL_SWITCH.clear)

    def test_follows_the_name_google_suggests_and_remembers_it(self):
        gone = FakeResp({"error": {"code": 404, "message": "This model models/gemini-3.8-flash is no longer available to new users. "
                                                             "Please update your code to use models/gemini-4.2-flash for the latest features."}}, 404)
        with mock.patch.object(ai.requests, "post", side_effect=[gone, gemini_ok("hi"), gemini_ok("again")]) as post, \
             mock.patch.object(ai.requests, "get", return_value=FakeResp({"models": []})):
            self.assertEqual(ai.text("x", provider="gemini"), "hi")
            self.assertEqual(ai.text("y", provider="gemini"), "again")
        urls = [c.args[0] for c in post.call_args_list]
        self.assertIn("gemini-3.8-flash:generateContent", urls[0])
        self.assertIn("gemini-4.2-flash:generateContent", urls[1])
        self.assertIn("gemini-4.2-flash:generateContent", urls[2])          # no second 404 round trip

    def test_picks_the_newest_image_model_from_the_list_when_no_hint(self):
        gone = FakeResp({"error": {"code": 404, "message": "models/gemini-2.5-flash-image is not found for API version v1beta"}}, 404)
        listing = FakeResp({"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.9-flash-image-preview", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.9-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-4-flash-image-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
        ]})
        ok = FakeResp({"candidates": [{"content": {"parts": [{"inlineData": {"data": "UE5HIQ=="}}]}}]})
        with mock.patch.object(ai.requests, "post", side_effect=[gone, ok]) as post, \
             mock.patch.object(ai.requests, "get", return_value=listing):
            self.assertEqual(ai.image("a cave", provider="gemini"), b"PNG!")
        self.assertIn("gemini-3.9-flash-image:generateContent", post.call_args_list[1].args[0])

    def test_gives_up_cleanly_when_nothing_replaces_it(self):
        gone = FakeResp({"error": {"code": 404, "message": "models/gemini-3.8-flash is not found"}}, 404)
        with mock.patch.object(ai.requests, "post", return_value=gone), \
             mock.patch.object(ai.requests, "get", return_value=FakeResp({"models": []})):
            with self.assertRaises(ai.AIError) as cm:
                ai.text("x", provider="gemini")
        self.assertIn("404", str(cm.exception))


class GeminiBusyTests(unittest.TestCase):
    def setUp(self):
        from config.db import init_db
        init_db()
        ai._MODEL_SWITCH.clear()
        ai._BUSY_ATTEMPTS.clear()
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "", "OPENAI_API_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_busy_answers_are_retried_then_succeed(self):
        busy = FakeResp({"error": {"code": 503, "message": "high demand"}}, 503)
        with mock.patch.object(ai.requests, "post", side_effect=[busy, busy, gemini_ok("ok")]) as post, \
             mock.patch.object(ai.time, "sleep") as sleep:
            self.assertEqual(ai.text("x", provider="gemini"), "ok")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [4, 10])

    def test_busy_for_good_falls_back_to_groq_when_a_key_exists_else_raises_busy(self):
        busy = FakeResp({"error": {"code": 503, "message": "high demand"}}, 503)
        with mock.patch.object(ai.requests, "post", return_value=busy), mock.patch.object(ai.time, "sleep"):
            with self.assertRaises(ai.BusyError) as cm:
                ai.text("x", provider="gemini")
        self.assertIn("overloaded", str(cm.exception))
        self.assertIsInstance(cm.exception, ai.AIError)
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "grq"}), \
             mock.patch.object(ai.requests, "post", return_value=busy), mock.patch.object(ai.time, "sleep"), \
             mock.patch.object(ai, "groq_text", return_value="from groq") as groq:
            self.assertEqual(ai.text("x", provider="gemini"), "from groq")
        groq.assert_called_once()


class GrokTests(unittest.TestCase):
    def setUp(self):
        from config.db import init_db
        init_db()
        ai._GROK_MODELS.clear()
        self.addCleanup(ai._GROK_MODELS.clear)
        self.env = mock.patch.dict(os.environ, {"XAI_API_KEY": "xk", "GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "GROQ_API_KEY": "",
                                                "CF_API_TOKEN": "", "GROK_TEXT_MODEL": "", "GROK_IMAGE_MODEL": "", "GROK_VISION_MODEL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.listing = FakeResp({"data": [{"id": "grok-4"}, {"id": "grok-4.6"}, {"id": "grok-4.6-fast"}, {"id": "grok-3-mini"},
                                          {"id": "grok-2-image-1212"}, {"id": "grok-2-vision-1212"}, {"id": "grok-code-fast-1"}]})

    def test_grok_is_picked_when_only_its_key_exists(self):
        self.assertEqual((ai.pick("text", None), ai.pick("vision", None), ai.pick("image", None)), ("grok", "grok", "grok"))

    def test_models_are_discovered_cheap_text_first_newest_image(self):
        with mock.patch.object(ai.requests, "request", return_value=self.listing) as req:
            self.assertEqual(ai.grok_model("text", None), "grok-4.6-fast")
            self.assertEqual(ai.grok_model("image", None), "grok-2-image-1212")
            self.assertEqual(ai.grok_model("vision", None), "grok-4.6")
            self.assertEqual(ai.grok_model("text", None), "grok-4.6-fast")      # cached
        self.assertEqual(req.call_count, 3)
        with mock.patch.dict(os.environ, {"GROK_TEXT_MODEL": "grok-9"}):
            self.assertEqual(ai.grok_model("text", None), "grok-9")

    def test_text_vision_and_image_calls(self):
        chat = FakeResp({"choices": [{"message": {"content": '{"caption": "hi"}'}}]})
        img = FakeResp({"data": [{"b64_json": "UE5HIQ=="}]})

        def fake(method, url, headers=None, timeout=None, json=None):
            if url.endswith("/models"):
                return self.listing
            if url.endswith("/images/generations"):
                self.assertEqual(json["model"], "grok-2-image-1212")
                return img
            self.assertEqual(headers["Authorization"], "Bearer xk")
            if isinstance(json["messages"][0]["content"], list):
                self.assertEqual(json["model"], "grok-4.6")
                self.assertEqual(json["messages"][0]["content"][1]["type"], "image_url")
            else:
                self.assertEqual(json["model"], "grok-4.6-fast")
                self.assertEqual(json["response_format"], {"type": "json_object"})
            return chat
        import tempfile
        from PIL import Image
        pic = Path(tempfile.mkdtemp()) / "p.png"
        Image.new("RGB", (8, 8)).save(pic)
        with mock.patch.object(ai.requests, "request", side_effect=fake):
            self.assertEqual(ai.text("x", json_mode=True), '{"caption": "hi"}')
            self.assertEqual(ai.vision("describe", [pic], json_mode=True), '{"caption": "hi"}')
            self.assertEqual(ai.image("a cave"), b"PNG!")

    def test_errors_are_explained(self):
        with mock.patch.object(ai.requests, "request", return_value=FakeResp({"error": {"message": "bad key"}}, 401)):
            with self.assertRaises(ai.AIError) as cm:
                ai.grok_text("x", None)
        self.assertIn("rejected the API key", str(cm.exception))
        with mock.patch.object(ai.requests, "request", return_value=FakeResp({"error": {"message": "no credits"}}, 402)):
            with self.assertRaises(ai.QuotaError):
                ai.grok_text("x", None)
        with mock.patch.object(ai.requests, "request", return_value=FakeResp({"error": {"message": "busy"}}, 503)):
            with self.assertRaises(ai.BusyError):
                ai.grok_text("x", None)

    def test_gemini_busy_falls_back_to_grok(self):
        busy = FakeResp({"error": {"code": 503, "message": "high demand"}}, 503)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g"}), \
             mock.patch.object(ai.requests, "post", return_value=busy), mock.patch.object(ai.time, "sleep"), \
             mock.patch.object(ai, "grok_text", return_value="from grok") as grok:
            ai._BUSY_ATTEMPTS.clear()
            self.assertEqual(ai.text("x"), "from grok")
        grok.assert_called_once()


class GroqModelSwitchTests(unittest.TestCase):
    def setUp(self):
        from config.db import init_db
        init_db()
        ai._GROQ_MODEL.clear()
        self.addCleanup(ai._GROQ_MODEL.clear)
        self.env = mock.patch.dict(os.environ, {"GROQ_API_KEY": "grq"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_retired_model_is_replaced_from_the_list(self):
        gone = FakeResp({"error": {"message": "The model `llama-3.3-70b-versatile` does not exist or you do not have access to it."}}, 404)
        ok = FakeResp({"choices": [{"message": {"content": "written"}}]})
        listing = FakeResp({"data": [{"id": "whisper-large-v3"}, {"id": "llama-guard-4-12b"}, {"id": "gemma2-9b-it"},
                                     {"id": "llama-4-maverick-17b-128e-instruct"}, {"id": "meta-llama/llama-4-scout-17b-16e-instruct"},
                                     {"id": "openai/gpt-oss-120b"}]})
        with mock.patch.object(ai.requests, "post", side_effect=[gone, ok, ok]) as post, \
             mock.patch.object(ai.requests, "get", return_value=listing):
            self.assertEqual(ai.groq_text("x", None), "written")
            self.assertEqual(ai.groq_text("y", None), "written")
        models = [c.kwargs["json"]["model"] for c in post.call_args_list]
        self.assertEqual(models[0], "llama-3.3-70b-versatile")
        self.assertTrue(models[1].startswith("llama-4") or "llama-4" in models[1], models[1])
        self.assertEqual(models[2], models[1])                                   # remembered


class GeminiSilenceTests(unittest.TestCase):
    def test_timeout_hands_the_job_to_groq(self):
        from config.db import init_db
        init_db()
        ai._BUSY_ATTEMPTS.clear()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "grq"}), \
             mock.patch.object(ai.requests, "post", side_effect=ai.requests.Timeout("read timed out")), \
             mock.patch.object(ai, "groq_text", return_value="from groq") as groq:
            self.assertEqual(ai.text("x"), "from groq")
        groq.assert_called_once()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "", "OPENAI_API_KEY": "", "XAI_API_KEY": ""}), \
             mock.patch.object(ai.requests, "post", side_effect=ai.requests.ConnectionError("no route")):
            with self.assertRaises(ai.BusyError):
                ai.text("x")
