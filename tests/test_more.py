"""YouTube Shorts, Threads, auto-captions, CSV import, best-time suggestions, content pillars, feed preview."""

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
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import analytics, importer, library, publisher, subtitles, youtube   # noqa: E402
from src.ai import providers                            # noqa: E402
from src.security import crypto                         # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
KEY = "c3RyZXNzLXRlc3Qta2V5LXN0cmVzcy10ZXN0LWtleS0="


class FakeResp:
    def __init__(self, payload=None, status=200, headers=None, text=""):
        self._p, self.status_code, self.headers, self.text = payload, status, headers or {}, text

    def json(self):
        if self._p is None:
            raise ValueError
        return self._p


class Base(unittest.TestCase):
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
            for t in ("posts", "users", "credentials", "pages", "assets"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY, "JWT_SECRET": "more", "YT_CLIENT_ID": "cid",
                                                "YT_CLIENT_SECRET": "csec", "GEMINI_API_KEY": "gem"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.user = auth.create_user("more@example.com", "password123", "Taverna")
        self.uid = self.user["id"]
        with get_connection() as conn:
            conn.execute("UPDATE users SET tier = 'enterprise' WHERE id = ?", (self.uid,))
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))


class YouTubeTests(Base):
    def test_auth_url_and_connect_flow(self):
        url = youtube.auth_url(self.uid, "http://127.0.0.1:8000/", "st4te")
        self.assertIn("client_id=cid", url)
        self.assertIn("settings%2Fyoutube%2Fcallback", url)
        self.assertIn("youtube.upload", url)
        r = self.client.post("/settings/youtube/connect", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth"))
        self.assertIn("yt_state", r.headers["set-cookie"])
        state = r.headers["set-cookie"].split("yt_state=")[1].split(";")[0]

        def fake_post(url, data=None, timeout=None):
            return FakeResp({"access_token": "acc", "refresh_token": "ref"})

        def fake_get(url, params=None, headers=None, timeout=None):
            return FakeResp({"items": [{"id": "UC123", "snippet": {"title": "Unexplained Greece"}}]})

        with mock.patch.object(youtube.requests, "post", side_effect=fake_post), \
             mock.patch.object(youtube.requests, "get", side_effect=fake_get):
            self.client.cookies.set("yt_state", state)
            r = self.client.get(f"/settings/youtube/callback?code=abc&state={state}", follow_redirects=False)
        self.assertIn("YouTube%20connected", r.headers["location"])
        conn_info = youtube.connection(self.uid)
        self.assertEqual((conn_info["refresh_token"], conn_info["channel_title"]), ("ref", "Unexplained Greece"))
        with get_connection() as conn:
            raw = conn.execute("SELECT value_encrypted FROM credentials WHERE name = 'YT_CONNECTION'").fetchone()["value_encrypted"]
        self.assertTrue(raw.startswith("v1:"))
        self.assertIn("Unexplained Greece", self.client.get("/settings").text)
        self.assertIn("youtube", publisher.configured_platforms(self.uid))
        self.client.post("/settings/youtube/disconnect", follow_redirects=False)
        self.assertIsNone(youtube.connection(self.uid))
        self.assertNotIn("youtube", publisher.configured_platforms(self.uid))

    def test_callback_rejects_bad_state_and_missing_refresh(self):
        r = self.client.get("/settings/youtube/callback?code=abc&state=wrong", follow_redirects=False)
        self.assertIn("security%20check", r.headers["location"])
        self.client.cookies.set("yt_state", "ok")
        with mock.patch.object(youtube.requests, "post", return_value=FakeResp({"access_token": "acc"})):
            r = self.client.get("/settings/youtube/callback?code=abc&state=ok", follow_redirects=False)
        self.assertIn("refresh%20token", r.headers["location"])
        r = self.client.get("/settings/youtube/callback?error=access_denied", follow_redirects=False)
        self.assertIn("cancelled", r.headers["location"])

    def test_upload_short_resumable(self):
        with get_connection() as conn:
            conn.execute("INSERT INTO credentials (user_id, name, value_encrypted, updated_at) VALUES (?, 'YT_CONNECTION', ?, 'x')",
                         (self.uid, crypto.encrypt(json.dumps({"refresh_token": "ref", "channel_id": "UC1"}))))
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "short.mp4"
            video.write_bytes(b"\x00" * 2048)
            calls = []

            def fake_post(url, **kw):
                calls.append(("post", url, kw))
                if url == youtube.TOKEN_URL:
                    return FakeResp({"access_token": "fresh"})
                return FakeResp(None, 200, {"Location": "https://upload.session/1"})

            def fake_put(url, **kw):
                calls.append(("put", url, kw))
                return FakeResp({"id": "vid123"})

            with mock.patch.object(youtube.requests, "post", side_effect=fake_post), \
                 mock.patch.object(youtube.requests, "put", side_effect=fake_put):
                vid = youtube.upload_short(self.uid, video, "Harbour sunset", "Golden hour. #rhodes", ["#rhodes"])
            self.assertEqual(vid, "vid123")
            start = next(c for c in calls if c[0] == "post" and c[1] == youtube.UPLOAD_URL)
            self.assertEqual(start[2]["headers"]["Authorization"], "Bearer fresh")
            self.assertEqual(start[2]["json"]["snippet"]["title"], "Harbour sunset")
            self.assertIn("#Shorts", start[2]["json"]["snippet"]["description"])
            self.assertEqual(start[2]["json"]["snippet"]["tags"], ["rhodes"])
            put = next(c for c in calls if c[0] == "put")
            self.assertEqual(put[1], "https://upload.session/1")
            # quota exhausted
            with mock.patch.object(youtube.requests, "post", side_effect=lambda url, **kw: FakeResp({"access_token": "a"}) if url == youtube.TOKEN_URL else FakeResp(None, 403)):
                with self.assertRaisesRegex(youtube.YouTubeError, "quota"):
                    youtube.upload_short(self.uid, video, "t", "d", [])
            # the publisher platform uses it
            pid = queries.create_draft("Short", "cap", "#a", media_type="video", user_id=self.uid, platforms=["youtube"],
                                       video_path=str(video))
            queries.set_status(pid, "approved", "")
            with mock.patch.object(youtube, "upload_short", return_value="yt_9") as up:
                self.assertEqual(publisher.main([]), 0)
            up.assert_called_once()
            post = queries.get_post(pid)
            self.assertEqual((post["youtube_video_id"], post["status"]), ("yt_9", "published"))
            self.assertIn("youtube.com/shorts/yt_9", self.client.get(f"/posts/{pid}").text)


class ThreadsTests(Base):
    def test_text_and_image_posts(self):
        with mock.patch.dict(os.environ, {"THREADS_ACCESS_TOKEN": "th"}):
            self.assertIn("threads", publisher.configured_platforms(self.uid))
            text_post = queries.create_draft("Hello", "Hello Threads", "#rhodes", user_id=self.uid, platforms=["threads"])
            img_post = queries.create_draft("Pic", "A picture", "", user_id=self.uid, platforms=["threads"])
            queries.update_post(img_post, image_url="https://img/x.jpg")
            for pid in (text_post, img_post):
                queries.set_status(pid, "approved", "")
            calls = []

            def fake(method, url, params=None, timeout=None):
                calls.append((method, url, params))
                if url.endswith("/me"):
                    return FakeResp({"id": "T1", "username": "unexplained"})
                if url.endswith("/T1/threads"):
                    return FakeResp({"id": "c_" + params["media_type"]})
                if "/c_IMAGE" in url:
                    return FakeResp({"status": "FINISHED"})
                if url.endswith("/threads_publish"):
                    return FakeResp({"id": "tp_" + params["creation_id"]})
                raise AssertionError(url)

            with mock.patch.object(publisher.requests, "request", side_effect=fake), \
                 mock.patch.object(publisher.time, "sleep", lambda s: None):
                self.assertEqual(publisher.main([]), 0)
            self.assertEqual(queries.get_post(text_post)["threads_post_id"], "tp_c_TEXT")
            self.assertEqual(queries.get_post(img_post)["threads_post_id"], "tp_c_IMAGE")
            text_call = next(c for c in calls if c[1].endswith("/T1/threads") and c[2]["media_type"] == "TEXT")
            self.assertEqual(text_call[2]["text"], "Hello Threads\n\n#rhodes")
            # local-only media is refused with the hosting hint
            local = queries.create_draft("Local", "c", "", media_type="video", user_id=self.uid, platforms=["threads"],
                                         video_path=__file__)
            queries.set_status(local, "approved", "")
            with mock.patch.object(publisher.requests, "request", side_effect=fake):
                self.assertEqual(publisher.main(["--id", str(local)]), 1)
            self.assertIn("hosted link", queries.get_post(local)["last_error"])


class SubtitleTests(Base):
    def test_chunks_and_gemini_transcript(self):
        words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate("one two three four five".split())]
        chunks = subtitles.chunks_from_words(words, 3)
        self.assertEqual([c.text for c in chunks], ["one two three", "four five"])
        self.assertLessEqual(chunks[0].end, chunks[1].start)
        raw = json.dumps({"segments": [{"text": "hello there", "start": 0, "end": 1.2}, {"text": "bad", "start": "x"},
                                       {"text": "friends", "start": 1.2, "end": 9.0}]})
        with mock.patch.object(providers, "_gemini_call", return_value={"candidates": [{"content": {"parts": [{"text": raw}]}}]}), \
             tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(b"x" * 10)
            f.flush()
            got = subtitles.transcribe_gemini(Path(f.name), self.uid, 3.0)
        self.assertEqual([(w["word"], w["end"]) for w in got], [("hello there", 1.2), ("friends", 3.0)])   # clipped to duration

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_subtitle_step_burns_captions_for_opted_in_users(self):
        from PIL import Image
        from moviepy import ImageClip
        from pydub.generators import Sine
        from moviepy import AudioFileClip
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            Image.new("RGB", (64, 64), (10, 10, 120)).save(d / "f.png")
            Sine(440).to_audio_segment(duration=1500).apply_gain(-6).export(d / "tone.mp3", format="mp3")
            clip = ImageClip(str(d / "f.png")).with_duration(1.5).with_audio(AudioFileClip(str(d / "tone.mp3")))
            clip.write_videofile(str(d / "clip.mp4"), fps=10, codec="libx264", audio_codec="aac", logger=None)
            clip.close()
            library.save_brand(self.uid, auto_subtitles=True)
            pid = queries.create_draft("Clip", "c", "", media_type="video", user_id=self.uid, source="upload",
                                       video_path=str(d / "clip.mp4"))
            ai_post = queries.create_draft("AI", "c", "", media_type="video", user_id=self.uid, video_path=str(d / "clip.mp4"))
            words = [{"word": "hello", "start": 0.1, "end": 0.6}, {"word": "world", "start": 0.6, "end": 1.2}]
            with mock.patch.object(subtitles, "transcribe", return_value=words), \
                 mock.patch.object(subtitles, "burn", wraps=lambda v, c, o, size=(1080, 1920), fps=30: subtitles.burn.__wrapped__(v, c, o, (180, 320), 10)
                                   if hasattr(subtitles.burn, "__wrapped__") else None) as _:
                pass
            original_burn = subtitles.burn
            with mock.patch.object(subtitles, "transcribe", return_value=words), \
                 mock.patch.object(subtitles, "burn", side_effect=lambda v, c, o, size=(1080, 1920), fps=30: original_burn(v, c, o, (180, 320), 10)):
                self.assertEqual(subtitles.run_step(self.uid), 0)
            post = queries.get_post(pid)
            self.assertTrue(post["video_path"].endswith("_captioned.mp4"))
            self.assertTrue(Path(post["video_path"]).exists())
            self.assertEqual(post["transcript"], "hello world")
            self.assertEqual(post["subtitled_path"], post["video_path"])
            self.assertIsNone(queries.get_post(ai_post)["subtitled_path"])     # AI reels already have captions
            self.assertEqual(subtitles.fetch_pending(user_id=self.uid), [])      # done once
            self.assertIn("hello world", self.client.get(f"/posts/{pid}").text)

    def test_failures_are_recorded_and_skipped_next_time(self):
        library.save_brand(self.uid, auto_subtitles=True)
        pid = queries.create_draft("Gone", "c", "", media_type="video", user_id=self.uid, source="upload", video_path="/nope.mp4")
        self.assertEqual(subtitles.run_step(self.uid), 1)
        post = queries.get_post(pid)
        self.assertIn("not found", post["last_error"])
        self.assertEqual(post["subtitled_path"], "skipped")
        self.assertEqual(subtitles.fetch_pending(user_id=self.uid), [])


class ImportAndPlanningTests(Base):
    def test_csv_import(self):
        text = importer.template_csv() + "\nBad row,x,,,,hologram,,\n"
        result = importer.import_csv(text, self.uid)
        self.assertEqual(result["created"], 2)
        self.assertTrue(any("hologram" in e or "media_type" in e for e in result["errors"]))
        posts = {p["topic"]: p for p in queries.list_posts(user_id=self.uid)}
        self.assertEqual(posts["Sunset drinks on the harbour"]["status"], "scheduled")
        self.assertEqual(posts["Sunset drinks on the harbour"]["scheduled_time"], "2026-11-20 18:30")
        self.assertEqual(posts["Sunset drinks on the harbour"]["platforms"], "facebook,instagram")
        self.assertEqual(posts["Weekend brunch"]["status"], "draft")
        self.assertEqual(importer.import_csv("", self.uid)["created"], 0)
        self.assertIn("topic", importer.import_csv("a,b\n1,2\n", self.uid)["errors"][0])

    def test_import_routes(self):
        r = self.client.get("/posts/import/template.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("topic,caption", r.text)
        csv_bytes = ("topic,caption,scheduled_time\nFrom sheet,Hello,\nBad time,x,tomorrow\n").encode("utf-8-sig")
        r = self.client.post("/posts/import", files={"file": ("posts.csv", io.BytesIO(csv_bytes), "text/csv")},
                             headers={"referer": "http://x/posts"}, follow_redirects=False)
        self.assertIn("Imported%201", r.headers["location"])
        self.assertIn("Row%203", r.headers["location"])
        self.assertIn("From sheet", self.client.get("/posts").text)
        r = self.client.post("/posts/import", files={"file": ("posts.csv", io.BytesIO(b"\xff\xfe\x00bad"), "text/csv")},
                             headers={"referer": "http://x/posts"}, follow_redirects=False)
        self.assertIn("UTF-8", r.headers["location"])

    def test_suggest_slots_from_online_followers(self):
        from src import pages
        with get_connection() as conn:
            conn.execute("INSERT INTO pages (user_id, name, fb_page_id, ig_account_id, token_encrypted) VALUES (?, 'P', '111', '222', ?)",
                         (self.uid, crypto.encrypt("tok")))
        def fake(method, url, params=None, **kw):
            if "/222/insights" in url:
                return FakeResp({"data": [{"name": "online_followers", "values": [
                    {"end_time": "2026-11-03T08:00:00+0000", "value": {"9": 50, "18": 90, "21": 70, "3": 200}},   # Mon 2 Nov
                    {"end_time": "2026-11-04T08:00:00+0000", "value": {"12": 40, "19": 80}}]}]})                  # Tue 3 Nov
            raise AssertionError(url)
        with mock.patch.object(publisher.requests, "request", side_effect=fake):
            suggestion = analytics.suggest_slots(self.uid, per_day=2)
        self.assertEqual(suggestion, "Mon 18:00,21:00; Tue 12:00,19:00")          # 03:00 ignored (night)
        with mock.patch.object(analytics, "suggest_slots", return_value="Mon 18:00"):
            r = self.client.post("/profile/suggest-times", data={}, follow_redirects=False)
        self.assertIn("Mon%2018", r.headers["location"])
        self.assertEqual(auth.get_user(self.uid)["posting_slots"], "Mon 18:00")
        with mock.patch.object(publisher.requests, "request", return_value=FakeResp({"error": {"code": 100, "message": "no"}}, 400)):
            self.assertIsNone(analytics.suggest_slots(self.uid))

    def test_content_pillars_rotate_and_save(self):
        library.save_brand(self.uid, content_pillars="food, view, staff", auto_subtitles=True)
        brand = library.brand_profile(self.uid)
        self.assertEqual(brand["auto_subtitles"], 1)
        hint = library.pillar_hint(brand)
        self.assertIn("food, view, staff", hint)
        self.assertIn("angle today's post towards", hint)
        self.assertEqual(library.pillar_hint({"content_pillars": ""}), "")
        prompt = library.analysis_prompt(brand, "image", 1)
        self.assertIn("Content pillars", prompt)
        r = self.client.post("/profile/brand", data={"content_pillars": "a, b", "auto_subtitles": "1", "posts_per_day": "0"},
                             follow_redirects=False)
        self.assertIn("msg=", r.headers["location"])
        self.assertEqual(library.brand_profile(self.uid)["content_pillars"], "a, b")
        from src import generator
        with mock.patch.object(providers, "text", side_effect=lambda p, *a, **k: json.dumps({"caption": "c", "hashtags": [], "image_prompt": "p"})) as t:
            generator.generate_post("x", user_id=self.uid)
        self.assertIn("Content pillars", t.call_args.args[0])

    def test_post_page_preview(self):
        pid = queries.create_draft("Preview", "A caption that is long " * 10, "#tag", user_id=self.uid)
        page = self.client.get(f"/posts/{pid}").text
        self.assertIn("How it roughly looks", page)
        self.assertIn("… more", page)
        self.assertIn("no media", page)


if __name__ == "__main__":
    unittest.main()
