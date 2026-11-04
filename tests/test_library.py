"""Media library: scanning, vision analysis (mocked), planning posts, montage, and the Library page."""

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from fastapi.testclient import TestClient              # noqa: E402
from PIL import Image                                   # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import library, main as orch                   # noqa: E402
from src.ai import providers                            # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
ANALYSIS = {"description": "A sunset over the harbour with boats", "caption": "Golden hour at the harbour. 🌅",
            "hook": "Harbour sunset", "hashtags": ["rhodes", "#sunset", "#harbour"], "mood": "calm", "quality": 5}


def make_png(path: Path, color=(30, 120, 200)):
    Image.new("RGB", (64, 64), color).save(path, "PNG")
    return path


def make_mp4(path: Path, seconds=1.0):
    from moviepy import ImageClip
    png = path.with_suffix(".tmp.png")
    make_png(png)
    ImageClip(str(png)).with_duration(seconds).write_videofile(str(path), fps=10, codec="libx264", audio=False, logger=None)
    png.unlink()
    return path


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(webapp.app)
        cls.client.__enter__()
        cls.media = tempfile.TemporaryDirectory()
        cls.mp4 = make_mp4(Path(cls.media.name) / "clip.mp4") if HAVE_FFMPEG else None

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.media.cleanup()

    def setUp(self):
        init_db()
        with get_connection() as conn:
            for t in ("posts", "users", "assets", "pages"):
                conn.execute(f"DELETE FROM {t}")
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = Path(self.tmp.name) / "library"
        self.patches = [
            mock.patch.object(library, "LIBRARY_DIR", self.lib),
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "gem", "JWT_SECRET": "lib-tests",
                                         "APP_SECRET_KEY": "c3RyZXNzLXRlc3Qta2V5LXN0cmVzcy10ZXN0LWtleS0="}),
        ]
        for p in self.patches:
            p.start()
        self.user = auth.create_user("lib@example.com", "password123", "Taverna")
        with get_connection() as conn:
            conn.execute("UPDATE users SET tier = 'enterprise' WHERE id = ?", (self.user["id"],))
        self.uid = self.user["id"]
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def add_files(self, images=2, videos=0):
        folder = library.user_dir(self.uid)
        for i in range(images):
            make_png(folder / f"photo_{i}.png", (i * 40, 100, 150))
        for i in range(videos):
            shutil.copy(self.mp4, folder / f"clip_{i}.mp4")
        (folder / "notes.txt").write_text("ignored")
        return folder

    # ----- scan -----

    def test_scan_registers_media_and_forgets_removed(self):
        folder = self.add_files(2, 1 if HAVE_FFMPEG else 0)
        r = library.scan(self.uid)
        self.assertEqual(r["added"], 3 if HAVE_FFMPEG else 2)
        assets = library.list_assets(self.uid)
        kinds = sorted(a["kind"] for a in assets)
        self.assertEqual(kinds, (["image", "image", "video"] if HAVE_FFMPEG else ["image", "image"]))
        photo = next(a for a in assets if a["kind"] == "image")
        self.assertEqual((photo["width"], photo["height"]), (64, 64))
        if HAVE_FFMPEG:
            clip = next(a for a in assets if a["kind"] == "video")
            self.assertAlmostEqual(clip["duration"], 1.0, delta=0.3)
        self.assertEqual(library.scan(self.uid)["skipped"], len(assets))      # unchanged
        (folder / "photo_0.png").unlink()
        self.assertEqual(library.scan(self.uid)["removed"], 1)
        self.assertEqual(len(library.list_assets(self.uid)), len(assets) - 1)

    # ----- analysis -----

    def test_analyze_stores_caption_and_hashtags(self):
        self.add_files(1)
        library.scan(self.uid)
        library.save_brand(self.uid, brand_name="Taverna Nikos", brand_brief="Fish by the harbour", brand_language="Greek")
        seen = {}

        def fake_vision(prompt, images, user_id=None, json_mode=False, provider=None):
            seen.update(prompt=prompt, images=images, user_id=user_id)
            return "```json\n" + json.dumps(ANALYSIS) + "\n```"

        with mock.patch.object(providers, "vision", side_effect=fake_vision):
            r = library.analyze_pending(self.uid)
        self.assertEqual(r, {"analyzed": 1, "failed": 0, "pending": 0})
        a = library.list_assets(self.uid)[0]
        self.assertEqual(a["caption"], "Golden hour at the harbour. 🌅")
        self.assertEqual(a["hashtags"], "#rhodes #sunset #harbour")      # '#' added where missing
        self.assertIn("Taverna Nikos", seen["prompt"])
        self.assertIn("Fish by the harbour", seen["prompt"])
        self.assertIn("Greek", seen["prompt"])
        self.assertEqual(seen["user_id"], self.uid)
        self.assertEqual(len(seen["images"]), 1)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_analyze_video_sends_frames(self):
        self.add_files(0, 1)
        library.scan(self.uid)
        seen = {}

        def fake_vision(prompt, images, user_id=None, json_mode=False, provider=None):
            seen["n"] = len(images)
            seen["all_exist"] = all(Path(i).exists() for i in images)
            return json.dumps(ANALYSIS)

        with mock.patch.object(providers, "vision", side_effect=fake_vision):
            library.analyze_pending(self.uid)
        self.assertEqual(seen["n"], library.FRAMES_PER_VIDEO)
        self.assertTrue(seen["all_exist"])

    def test_analysis_errors_recorded_and_quota_stops_run(self):
        self.add_files(3)
        library.scan(self.uid)
        calls = []

        def fake_vision(prompt, images, *args, **kw):
            calls.append(1)
            if len(calls) == 1:
                return "not json at all"
            raise providers.QuotaError("free tier used up")

        with mock.patch.object(providers, "vision", side_effect=fake_vision):
            r = library.analyze_pending(self.uid)
        self.assertEqual(r["analyzed"], 0)
        self.assertEqual(r["failed"], 2)          # bad json, then quota -> stop
        self.assertEqual(len(calls), 2)
        errors = [a["analysis_error"] for a in library.list_assets(self.uid) if a["analysis_error"]]
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("JSON" in e for e in errors))

    # ----- planning -----

    def test_plan_creates_image_posts_from_least_used(self):
        self.add_files(3)
        library.save_brand(self.uid, auto_platforms="facebook,instagram")
        with mock.patch.object(providers, "vision", return_value=json.dumps(ANALYSIS)):
            made = library.plan(self.uid, 2, montage=False)
        self.assertEqual(len(made), 2)
        for pid in made:
            post = queries.get_post(pid)
            self.assertEqual((post["source"], post["media_type"], post["status"]), ("library", "image", "draft"))
            self.assertEqual(post["caption"], ANALYSIS["caption"])
            self.assertEqual(post["hashtags"], "#rhodes #sunset #harbour")
            self.assertEqual(post["topic"], "Harbour sunset")
            self.assertEqual(post["platforms"], "facebook,instagram")
            self.assertTrue(Path(post["image_path"]).exists())
            self.assertEqual(post["user_id"], self.uid)
        used = sorted(a["times_used"] for a in library.list_assets(self.uid))
        self.assertEqual(used, [0, 1, 1])
        with mock.patch.object(providers, "vision", return_value=json.dumps(ANALYSIS)):
            library.plan(self.uid, 1, montage=False)
        # the third post used the one asset that had not been used yet
        self.assertEqual(sorted(a["times_used"] for a in library.list_assets(self.uid)), [1, 1, 1])

    def test_plan_skips_excluded_and_low_quality(self):
        self.add_files(2)
        library.scan(self.uid)
        a, b = library.list_assets(self.uid)
        library.set_excluded(a["id"], self.uid, True)
        low = dict(ANALYSIS, quality=1)
        with mock.patch.object(providers, "vision", return_value=json.dumps(low)):
            made = library.plan(self.uid, 2, montage=False)
        self.assertEqual(made, [])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_plan_uses_short_clip_directly(self):
        self.add_files(0, 1)
        with mock.patch.object(providers, "vision", return_value=json.dumps(ANALYSIS)):
            made = library.plan(self.uid, 1, montage=False)
        post = queries.get_post(made[0])
        self.assertEqual(post["media_type"], "video")
        self.assertTrue(post["video_path"].endswith("clip_0.mp4"))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_montage_from_three_clips(self):
        self.add_files(0, 3)
        library.scan(self.uid)
        with mock.patch.object(providers, "vision", return_value=json.dumps(ANALYSIS)):
            library.analyze_pending(self.uid)
        out_dir = Path(self.tmp.name) / "videos"
        with mock.patch.object(providers, "vision", return_value=json.dumps(ANALYSIS)), \
             mock.patch.object(providers, "text", return_value=json.dumps(dict(ANALYSIS, caption="Three moments.", hook="Harbour life"))), \
             mock.patch.object(library, "MONTAGE_SIZE", (180, 320)), mock.patch.object(library, "MONTAGE_FPS", 10), \
             mock.patch.object(library, "MONTAGE_SECONDS_PER_CLIP", 0.5), \
             mock.patch("src.video.video_compiler.VIDEO_DIR", out_dir):
            pid = library.make_montage_post(self.uid, library.brand_profile(self.uid))
        post = queries.get_post(pid)
        self.assertEqual(post["media_type"], "video")
        self.assertEqual(post["caption"], "Three moments.")
        self.assertEqual(post["topic"], "Harbour life")
        self.assertTrue(Path(post["video_path"]).exists())
        self.assertGreater(Path(post["video_path"]).stat().st_size, 1000)
        self.assertEqual(len(post["asset_ids"].split(",")), 3)
        self.assertTrue(all(a["times_used"] == 1 for a in library.list_assets(self.uid)))

    def test_run_daily_only_for_due_users(self):
        other = auth.create_user("o@example.com", "password123")
        library.save_brand(self.uid, posts_per_day=2)
        library.save_brand(other["id"], posts_per_day=0)
        planned = []
        with mock.patch.object(library, "plan", side_effect=lambda uid, n, montage=None: planned.append((uid, n)) or []):
            self.assertEqual(library.run_daily(), 0)
            self.assertEqual(planned, [(self.uid, 2)])
            # today's plan is recorded by plan(); simulate it and check it is not repeated
            with get_connection() as conn:
                conn.execute("UPDATE users SET last_plan_date = date('now', 'localtime') WHERE id = ?", (self.uid,))
            library.run_daily()
            self.assertEqual(planned, [(self.uid, 2)])
        self.assertIn("plan", orch.STEP_ORDER)

    # ----- Library page -----

    def test_library_page_upload_exclude_delete_and_file_tenancy(self):
        buf = io.BytesIO()
        Image.new("RGB", (20, 20)).save(buf, "JPEG")
        r = self.client.post("/library/upload", files=[("files", ("a.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")),
                                                        ("files", ("b.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")),
                                                        ("files", ("c.exe", io.BytesIO(b"x"), "application/octet-stream"))],
                             follow_redirects=False)
        self.assertIn("2%20file", r.headers["location"])
        self.assertIn("c.exe", r.headers["location"])
        assets = library.list_assets(self.uid)
        self.assertEqual(len(assets), 2)
        page = self.client.get("/library").text
        self.assertIn("a.jpg", page)
        self.assertIn("not analyzed", page)
        self.assertIn(str(library.user_dir(self.uid)), page)
        self.assertEqual(self.client.get(f"/library/file/{assets[0]['id']}").status_code, 200)
        self.client.post(f"/library/{assets[0]['id']}/exclude", data={"excluded": "1"}, follow_redirects=False)
        self.assertTrue(library.get_asset(assets[0]["id"], self.uid)["excluded"])
        self.client.post(f"/library/{assets[1]['id']}/delete", data={"remove_file": "1"}, follow_redirects=False)
        self.assertIsNone(library.get_asset(assets[1]["id"], self.uid))
        self.assertFalse(Path(assets[1]["path"]).exists())
        other = auth.create_user("o2@example.com", "password123")
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(other["id"]))
        self.assertEqual(self.client.get(f"/library/file/{assets[0]['id']}").status_code, 404)
        self.assertEqual(self.client.post(f"/library/{assets[0]['id']}/delete", follow_redirects=False).status_code, 404)
        self.assertNotIn("a.jpg", self.client.get("/library").text)

    def test_plan_button_runs_in_background_and_brand_form_saves(self):
        self.add_files(1)
        with mock.patch.object(library, "plan") as plan:
            r = self.client.post("/library/plan", data={"count": "3"}, follow_redirects=False)
        self.assertIn("msg=", r.headers["location"])
        plan.assert_called_once_with(self.uid, 3)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "your_gemini_key_here", "OPENAI_API_KEY": ""}):
            r = self.client.post("/library/plan", data={"count": "1"}, follow_redirects=False)
            self.assertIn("Gemini", r.headers["location"])
        r = self.client.post("/profile/brand", data={"brand_name": "Nikos", "brand_brief": "Fish", "brand_language": "Greek",
                                                     "posts_per_day": "2", "auto_platforms": ["facebook"], "montage_enabled": "1"},
                             follow_redirects=False)
        self.assertIn("2%20post", r.headers["location"])
        brand = library.brand_profile(self.uid)
        self.assertEqual((brand["brand_name"], brand["posts_per_day"], brand["auto_platforms"], brand["montage_enabled"]),
                         ("Nikos", 2, "facebook", 1))
        self.assertIn("Nikos", self.client.get("/profile").text)
        r = self.client.post("/profile/brand", data={"posts_per_day": "lots"}, headers={"referer": "http://x/profile"},
                             follow_redirects=False)
        self.assertIn("err=", r.headers["location"])


class GeneratorBrandTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM users")
        self.user = auth.create_user("gen@example.com", "password123")
        library.save_brand(self.user["id"], brand_name="Nikos", brand_brief="Fish by the harbour", brand_language="Greek")

    def test_generate_post_uses_provider_and_brand(self):
        from src import generator
        seen = {}

        def fake_text(prompt, user_id=None, json_mode=False, provider=None):
            seen["prompt"] = prompt
            return json.dumps({"caption": "Καλημέρα!", "hashtags": ["#rhodes", "#fish"], "image_prompt": "fish"})

        with mock.patch.object(providers, "text", side_effect=fake_text):
            post = generator.generate_post("Fresh catch", user_id=self.user["id"])
        self.assertEqual(post["caption"], "Καλημέρα!")
        self.assertEqual(post["hashtags"], "#rhodes #fish")
        self.assertIn("Fish by the harbour", seen["prompt"])
        self.assertIn("Greek", seen["prompt"])


if __name__ == "__main__":
    unittest.main()
