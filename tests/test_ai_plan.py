"""Daily AI posts: planner, title overlay, profile settings, pipeline hook."""
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ["SCHEDULER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import ai_plan, image_generator, library, overlay   # noqa: E402
from src import main as orch                            # noqa: E402
from src.ai import providers                            # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402


def fake_text(prompt, user_id=None, json_mode=False):
    if "Suggest" in prompt:
        return json.dumps({"ideas": [
            {"topic": "Το μυστήριο της Σπηλιάς του Δράκου", "why": "x", "best_day": "2026-11-05", "format": "image"},
            {"topic": "Λίνδος: ο θρύλος της Ακρόπολης", "why": "x", "best_day": "2026-11-06", "format": "image"},
            {"topic": "Already used topic", "why": "x", "best_day": "2026-11-07", "format": "image"},
            {"topic": "Τα φαντάσματα του Μοναστηριού", "why": "x", "best_day": "2026-11-08", "format": "image"},
        ]})
    return json.dumps({"title": "Η Σπηλιά του Δράκου", "caption": "Ελληνικά.\n\nEnglish.", "hashtags": ["#rhodes", "#mystery"],
                       "image_prompt": "a misty sea cave at dawn"})


class AiPlanBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(webapp.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        init_db()
        with get_connection() as conn:
            for t in ("posts", "users", "pages", "pipeline_runs"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"JWT_SECRET": "ai", "GEMINI_API_KEY": "gem"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.uid = auth.create_user("ai@example.com", "password123", "Unexplained Greece")["id"]
        library.save_brand(self.uid, brand_brief="Mysteries of Greece", brand_language="Greek and English",
                           content_pillars="legends, archaeology", auto_platforms="facebook,instagram,tiktok,youtube",
                           ai_posts_per_day=2)
        queries.create_draft("Already used topic", "c", "", user_id=self.uid)
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))

class AiPlanTests(AiPlanBase):
    def test_plan_makes_posts_with_titles_skips_used_topics_and_image_channels_only(self):
        with mock.patch.object(providers, "text", side_effect=fake_text):
            made = ai_plan.plan(self.uid, today=date(2026, 11, 5))
        self.assertEqual(len(made), 2)
        posts = [queries.get_post(p) for p in made]
        self.assertEqual([p["topic"] for p in posts], ["Το μυστήριο της Σπηλιάς του Δράκου", "Λίνδος: ο θρύλος της Ακρόπολης"])
        p = posts[0]
        self.assertEqual((p["status"], p["source"], p["media_type"], p["platforms"]), ("draft", "ai", "image", "facebook,instagram"))
        self.assertEqual(p["title_overlay"], "Η Σπηλιά του Δράκου")
        self.assertIn("English.", p["caption"])
        self.assertEqual(p["image_prompt"], "a misty sea cave at dawn")
        self.assertEqual(ai_plan.settings(self.uid)["last_ai_plan_date"], "2026-11-05")
        self.assertEqual(ai_plan.due_users(date(2026, 11, 5)), [])                 # done for today
        self.assertEqual(ai_plan.due_users(date(2026, 11, 6)), [self.uid])          # due again tomorrow

    def test_auto_approve_schedules_into_posting_slots(self):
        library.save_brand(self.uid, ai_auto_approve=True)
        with get_connection() as conn:
            conn.execute("UPDATE users SET posting_slots = 'Mon,Tue,Wed,Thu,Fri,Sat,Sun 09:30,18:00' WHERE id = ?", (self.uid,))
        with mock.patch.object(providers, "text", side_effect=fake_text):
            made = ai_plan.plan(self.uid, count=1)
        p = queries.get_post(made[0])
        self.assertIn(p["status"], ("approved", "scheduled"))
        self.assertTrue(p["scheduled_time"])
        self.assertTrue(p["scheduled_time"].endswith("09:30") or p["scheduled_time"].endswith("18:00"))

    def test_pipeline_plan_step_runs_the_planner_once_a_day(self):
        with mock.patch.object(providers, "text", side_effect=fake_text), \
             mock.patch.object(library, "run_daily", return_value=0), mock.patch("src.notify.send", return_value=False):
            self.assertEqual(orch.run_step("plan", user_id=self.uid), 0)
            self.assertEqual(orch.run_step("plan", user_id=self.uid), 0)
        with get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM posts WHERE source = 'ai' AND title_overlay IS NOT NULL").fetchone()["n"]
        self.assertEqual(n, 2)
        # no key: a clear config error, not a crash
        with get_connection() as conn:
            conn.execute("UPDATE users SET last_ai_plan_date = NULL WHERE id = ?", (self.uid,))
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), mock.patch.object(library, "run_daily", return_value=0):
            self.assertEqual(orch.run_step("plan", user_id=self.uid), 1)

    def test_title_overlay_renders_greek_on_the_generated_picture(self):
        from PIL import Image
        path = Path(tempfile.mkdtemp()) / "pic.png"
        Image.new("RGB", (640, 640), (40, 60, 90)).save(path)
        before = path.read_bytes()
        out = overlay.render_title(path, "Η Σπηλιά του Δράκου στη Ρόδο")
        self.assertEqual(out, path)
        self.assertNotEqual(path.read_bytes(), before)
        with Image.open(path) as img:
            self.assertEqual(img.size, (640, 640))
            px = img.convert("RGB").getpixel((320, 600))
            self.assertNotEqual(px, (40, 60, 90))                 # the band changed the bottom of the picture
        self.assertEqual(overlay.render_title(path, ""), path)      # nothing to draw: untouched

    def test_image_step_draws_the_title_unless_switched_off(self):
        from PIL import Image
        pid = queries.create_draft("x", "c", "", "a cave", user_id=self.uid, source="ai")
        with get_connection() as conn:
            conn.execute("UPDATE posts SET title_overlay = 'Μυστήριο' WHERE id = ?", (pid,))
        buf = Image.new("RGB", (256, 256), (10, 10, 10))
        import io
        raw = io.BytesIO()
        buf.save(raw, format="PNG")
        with mock.patch.object(image_generator, "generate_image", return_value=(raw.getvalue(), "AI image (gemini)")), \
             mock.patch("src.overlay.render_title") as draw:
            self.assertTrue(image_generator.process_post(queries.get_post(pid)))
        draw.assert_called_once()
        self.assertEqual(draw.call_args.args[1], "Μυστήριο")
        library.save_brand(self.uid, ai_title_overlay=False)
        pid2 = queries.create_draft("y", "c", "", "a cave", user_id=self.uid, source="ai")
        with get_connection() as conn:
            conn.execute("UPDATE posts SET title_overlay = 'Μυστήριο' WHERE id = ?", (pid2,))
        with mock.patch.object(image_generator, "generate_image", return_value=(raw.getvalue(), "AI image (gemini)")), \
             mock.patch("src.overlay.render_title") as draw:
            image_generator.process_post(queries.get_post(pid2))
        draw.assert_not_called()

    def test_profile_form_saves_the_ai_settings(self):
        r = self.client.post("/profile/brand", data={"brand_name": "UG", "brand_language": "Greek and English",
                                                     "ai_posts_per_day": "3", "ai_auto_approve": "1", "ai_title_overlay": "1",
                                                     "platforms": ["facebook", "instagram"]}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("3%20AI%20post", r.headers["location"])
        brand = library.brand_profile(self.uid)
        self.assertEqual((brand["ai_posts_per_day"], brand["ai_auto_approve"], brand["ai_title_overlay"]), (3, 1, 1))
        html = self.client.get("/profile").text
        self.assertIn('name="ai_posts_per_day"', html)
        self.assertIn('value="3"', html)


if __name__ == "__main__":
    unittest.main()


class BrandFormPlatformTests(AiPlanBase):
    def test_platform_ticks_on_the_brand_form_are_saved(self):
        r = self.client.post("/profile/brand", data={"brand_name": "UG", "platforms": ["facebook", "instagram"],
                                                     "ai_posts_per_day": "3"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(library.brand_profile(self.uid)["auto_platforms"], "facebook,instagram")
        html = self.client.get("/profile").text
        # the brand form's TikTok box is not ticked any more
        form = html.split('action="/profile/brand"')[1].split("</form>")[0]
        tiktok_box = [line for line in form.splitlines() if 'value="tiktok"' in line]
        self.assertTrue(tiktok_box and "checked" not in tiktok_box[0])

    def test_ai_posts_never_target_video_channels_even_with_no_choice(self):
        library.save_brand(self.uid, auto_platforms=None)
        with mock.patch.object(providers, "text", side_effect=fake_text), \
             mock.patch("src.publisher.configured_platforms", return_value=["facebook", "instagram", "tiktok", "youtube"]):
            pid = ai_plan.plan(self.uid, count=1)[0]
        self.assertEqual(queries.get_post(pid)["platforms"], "facebook,instagram")
