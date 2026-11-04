"""Queue slots, first-comment hashtags, Groq, Pexels stock, analytics + reports, Telegram, evergreen recycling."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import analytics, image_generator, notify, publisher, recycle, slots, stock   # noqa: E402
from src.ai import providers                            # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402


class FakeResp:
    def __init__(self, payload=None, status=200, content=b"", text=""):
        self._p, self.status_code, self.content, self.text = payload, status, content, text
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._p is None:
            raise ValueError
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise stock.requests.HTTPError(str(self.status_code))

    def iter_content(self, n):
        yield self.content


class SlotTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")

    def test_parse_and_upcoming(self):
        parsed = slots.parse_slots("Mon,Wed 09:00,18:30; Sat 11:00")
        self.assertEqual(parsed, [(0, 9, 0), (0, 18, 30), (2, 9, 0), (2, 18, 30), (5, 11, 0)])
        monday = datetime(2026, 11, 2, 10, 0)   # a Monday, 10:00
        nxt = slots.upcoming(parsed, monday, 3)
        self.assertEqual([w.strftime("%a %H:%M") for w in nxt], ["Mon 18:30", "Wed 09:00", "Wed 18:30"])
        for bad in ("Funday 09:00", "Mon 25:00", "Mon", ""):
            if bad == "":
                self.assertTrue(slots.parse_slots(bad))          # empty -> defaults
            else:
                with self.assertRaises(slots.SlotsError):
                    slots.parse_slots(bad)

    def test_next_free_slot_skips_taken(self):
        now = datetime(2026, 11, 2, 8, 0)
        first = slots.next_free_slot(1, "Mon 09:00,18:00", now)
        self.assertEqual(first, "2026-11-02 09:00")
        with get_connection() as conn:
            conn.execute("INSERT INTO posts (user_id, status, scheduled_time, topic) VALUES (1, 'scheduled', ?, 'x')", (first,))
        self.assertEqual(slots.next_free_slot(1, "Mon 09:00,18:00", now), "2026-11-02 18:00")
        self.assertEqual(slots.next_free_slot(2, "Mon 09:00,18:00", now), "2026-11-02 09:00")   # other user
        self.assertEqual(len(slots.preview("Mon 09:00", 3, now)), 3)


class FirstCommentTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")
            conn.execute("DELETE FROM users")
        self.user = auth.create_user("fc@example.com", "password123")
        self.env = mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t", "META_PAGE_ID": "111",
                                                "META_INSTAGRAM_ACCOUNT_ID": "222"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.env.stop()

    def test_hashtags_go_to_first_comment_when_enabled(self):
        with get_connection() as conn:
            conn.execute("UPDATE users SET hashtags_as_comment = 1 WHERE id = ?", (self.user["id"],))
            pid = conn.execute("INSERT INTO posts (user_id, status, topic, caption, hashtags, image_url, platforms) "
                               "VALUES (?, 'approved', 'x', 'Hello harbour', '#rhodes #sunset', 'https://i/x.jpg', 'facebook,instagram')",
                               (self.user["id"],)).lastrowid
        calls = []

        def fake(method, url, **kw):
            calls.append((url, kw.get("data") or kw.get("params") or {}))
            if url.endswith("/media"):
                return FakeResp({"id": "cont"})
            if "cont" in url and method == "GET":
                return FakeResp({"status_code": "FINISHED"})
            if url.endswith("/media_publish"):
                return FakeResp({"id": "ig1"})
            if url.endswith("/comments"):
                return FakeResp({"id": "cmt_" + url.split("/")[-2]})
            return FakeResp({"id": "x", "post_id": "fb1"})

        with mock.patch.object(publisher.requests, "request", side_effect=fake), \
             mock.patch.object(publisher.time, "sleep", lambda s: None):
            self.assertEqual(publisher.main([]), 0)
        photo = next(c for c in calls if "/photos" in c[0])
        self.assertEqual(photo[1]["message"], "Hello harbour")                  # clean caption
        ig = next(c for c in calls if c[0].endswith("/222/media"))
        self.assertEqual(ig[1]["caption"], "Hello harbour")
        comments = [c for c in calls if c[0].endswith("/comments")]
        self.assertEqual(len(comments), 2)
        self.assertTrue(all(c[1]["message"] == "#rhodes #sunset" for c in comments))
        post = queries.get_post(pid)
        self.assertEqual(post["fb_comment_id"], "cmt_fb1")
        self.assertEqual(post["ig_comment_id"], "cmt_ig1")
        self.assertEqual(post["status"], "published")

    def test_default_keeps_hashtags_in_caption(self):
        with get_connection() as conn:
            conn.execute("INSERT INTO posts (user_id, status, topic, caption, hashtags, image_url, platforms) "
                         "VALUES (?, 'approved', 'x', 'Hello', '#a', 'https://i/x.jpg', 'facebook')", (self.user["id"],))
        calls = []
        with mock.patch.object(publisher.requests, "request",
                               side_effect=lambda m, u, **kw: calls.append((u, kw["data"])) or FakeResp({"post_id": "fb1"})):
            publisher.main([])
        self.assertEqual(calls[0][1]["message"], "Hello\n\n#a")
        self.assertFalse(any(u.endswith("/comments") for u, _ in calls))


class GroqAndStockTests(unittest.TestCase):
    def test_groq_text_and_pick(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk", "GEMINI_API_KEY": "", "OPENAI_API_KEY": ""}):
            self.assertEqual(providers.pick("text", None), "groq")
            ok = FakeResp({"choices": [{"message": {"content": ' {"caption": "hi"} '}}]})
            with mock.patch.object(providers.requests, "post", return_value=ok) as post:
                self.assertEqual(providers.text("x", json_mode=True), '{"caption": "hi"}')
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer gsk")
            self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})
            with mock.patch.object(providers.requests, "post", return_value=FakeResp({"error": {"message": "limit"}}, 429)):
                with self.assertRaises(providers.QuotaError):
                    providers.text("x")

    def test_pexels_photo_and_video(self):
        photos = {"photos": [{"id": 1, "photographer": "Maria", "url": "https://pexels.com/p/1",
                              "src": {"large2x": "https://img/1.jpg", "original": "https://img/1o.jpg"}}]}
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "pex"}):
            with mock.patch.object(stock.requests, "get", return_value=FakeResp(photos)) as get:
                found = stock.search_photo("rhodes harbour sunset")
            self.assertEqual(found["url"], "https://img/1.jpg")
            self.assertEqual(found["credit"], "Photo by Maria on Pexels")
            self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "pex")
            videos = {"videos": [{"id": 9, "duration": 12, "url": "https://pexels.com/v/9", "user": {"name": "Nikos"},
                                  "video_files": [{"link": "https://v/4k", "height": 3840}, {"link": "https://v/hd", "height": 1920}]}]}
            with mock.patch.object(stock.requests, "get", return_value=FakeResp(videos)):
                clip = stock.search_video("beach")
            self.assertEqual(clip["url"], "https://v/hd")
            with mock.patch.object(stock.requests, "get", return_value=FakeResp({"photos": []})):
                with self.assertRaisesRegex(stock.StockError, "no photo"):
                    stock.search_photo("zzz")
            with mock.patch.object(stock.requests, "get", return_value=FakeResp({}, 429)):
                with self.assertRaisesRegex(stock.StockError, "rate limit"):
                    stock.search_photo("x")

    def test_image_generator_falls_back_to_stock(self):
        photos = {"photos": [{"id": 1, "photographer": "Maria", "url": "https://pexels.com/p/1", "src": {"large": "https://img/1.jpg"}}]}

        def get(url, **kw):
            return FakeResp(photos) if "api.pexels.com" in url else FakeResp(content=b"\xff\xd8jpeg")

        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "pex", "OPENAI_API_KEY": "", "GEMINI_API_KEY": "", "CF_API_TOKEN": ""}), \
             mock.patch.object(stock.requests, "get", side_effect=get), mock.patch.object(providers.requests, "get", side_effect=get):
            data, credit = image_generator.generate_image("A photo of the old harbour of Rhodes at sunset with boats")
        self.assertEqual(data, b"\xff\xd8jpeg")
        self.assertIn("Maria", credit)
        self.assertEqual(image_generator.stock_query("A photorealistic photo of the old harbour of Rhodes at sunset"),
                         "old harbour Rhodes sunset")
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "", "OPENAI_API_KEY": "", "GEMINI_API_KEY": "", "CF_API_TOKEN": ""}):
            with self.assertRaisesRegex(image_generator.ImageGenError, "No image provider"):
                image_generator.generate_image("x")


class AnalyticsTests(unittest.TestCase):
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
            for t in ("posts", "users", "post_metrics", "pages"):
                conn.execute(f"DELETE FROM {t}")
        self.user = auth.create_user("an@example.com", "password123", "Apex")
        self.uid = self.user["id"]
        self.env = mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t", "META_PAGE_ID": "111", "JWT_SECRET": "growth"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        other = auth.create_user("x@example.com", "password123")
        with get_connection() as conn:
            self.a = conn.execute("INSERT INTO posts (user_id, status, topic, published_at, fb_post_id, ig_post_id, platforms) "
                                  "VALUES (?, 'published', 'Harbour', ?, '111_1', '171', 'facebook,instagram')", (self.uid, now)).lastrowid
            self.b = conn.execute("INSERT INTO posts (user_id, status, topic, published_at, fb_post_id) "
                                  "VALUES (?, 'published', 'Brunch', ?, '111_2')", (self.uid, now)).lastrowid
            conn.execute("INSERT INTO posts (user_id, status, topic, published_at, fb_post_id) VALUES (?, 'published', 'Theirs', ?, '999')",
                         (other["id"], now))

    def tearDown(self):
        self.env.stop()

    def fake_meta(self, method, url, **kw):
        if url.endswith("/171/insights"):
            return FakeResp({"data": [{"name": "views", "values": [{"value": 500}]}, {"name": "reach", "values": [{"value": 400}]},
                                      {"name": "likes", "values": [{"value": 40}]}, {"name": "comments", "values": [{"value": 5}]},
                                      {"name": "shares", "values": [{"value": 3}]}, {"name": "saved", "values": [{"value": 7}]},
                                      {"name": "total_interactions", "values": [{"value": 55}]}]})
        if url.endswith("/insights"):
            return FakeResp({"data": [{"name": "post_total_media_view_unique", "values": [{"value": 1200}]},
                                      {"name": "post_media_view", "values": [{"value": 1500}]}]})
        return FakeResp({"likes": {"summary": {"total_count": 30}}, "comments": {"summary": {"total_count": 4}},
                         "shares": {"count": 2}, "id": url.split("/")[-1]})

    def test_refresh_stores_metrics_and_skips_fresh_ones(self):
        with mock.patch.object(publisher.requests, "request", side_effect=self.fake_meta):
            r = analytics.refresh(self.uid)
        self.assertEqual((r["ok"], r["failed"], r["posts"]), (3, 0, 2))
        with get_connection() as conn:
            rows = {(m["post_id"], m["platform"]): dict(m) for m in conn.execute("SELECT * FROM post_metrics")}
        fb = rows[(self.a, "facebook")]
        self.assertEqual((fb["likes"], fb["comments"], fb["shares"], fb["reach"], fb["views"], fb["engagement"]), (30, 4, 2, 1200, 1500, 36))
        ig = rows[(self.a, "instagram")]
        self.assertEqual((ig["views"], ig["reach"], ig["likes"], ig["saved"], ig["engagement"]), (500, 400, 40, 7, 55))
        self.assertNotIn((3, "facebook"), rows)                      # other user's post untouched
        with mock.patch.object(publisher.requests, "request", side_effect=AssertionError("must not refetch")):
            self.assertEqual(analytics.refresh(self.uid)["posts"], 0)
        with mock.patch.object(publisher.requests, "request", side_effect=self.fake_meta):
            self.assertEqual(analytics.refresh(self.uid, force=True)["ok"], 3)

    def test_insights_failure_is_tolerated_and_token_errors_counted(self):
        def no_insights(method, url, **kw):
            if url.endswith("/insights"):
                return FakeResp({"error": {"code": 100, "message": "not allowed"}}, 400)
            return self.fake_meta(method, url, **kw)
        with mock.patch.object(publisher.requests, "request", side_effect=no_insights):
            r = analytics.refresh(self.uid)
        self.assertEqual(r["ok"], 2)     # 2 facebook ok (without insights), instagram failed
        self.assertEqual(r["failed"], 1)

    def test_performance_page_report_and_csv(self):
        with mock.patch.object(publisher.requests, "request", side_effect=self.fake_meta):
            analytics.refresh(self.uid)
        month = datetime.now().strftime("%Y-%m")
        data = analytics.performance(self.uid, month)
        self.assertEqual(data["totals"]["posts"], 2)
        self.assertEqual(data["totals"]["likes"], 30 + 40 + 30)
        self.assertEqual(data["top"][0]["id"], self.a)
        page = self.client.get(f"/performance?month={month}").text
        self.assertIn("Harbour", page)
        self.assertIn("2,800", page)
        self.assertNotIn("Theirs", page)
        report = self.client.get(f"/performance/report?month={month}").text
        self.assertIn("social media report", report)
        self.assertIn("Top posts", report)
        self.assertIn("Apex", report)
        csv = self.client.get(f"/performance/report.csv?month={month}")
        self.assertEqual(csv.headers["content-type"].split(";")[0], "text/csv")
        self.assertIn("Harbour,facebook,1500,1200,30,4,2", csv.text)
        self.assertEqual(self.client.get("/performance?month=nope").status_code, 400)
        empty = analytics.performance(self.uid, "2020-01")
        self.assertEqual(empty["totals"]["posts"], 0)
        self.assertEqual(empty["prev_month"], "2019-12")


class NotifyAndRecycleTests(unittest.TestCase):
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
            for t in ("posts", "users"):
                conn.execute(f"DELETE FROM {t}")
        self.user = auth.create_user("nr@example.com", "password123")
        self.uid = self.user["id"]
        self.env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "bot1", "TELEGRAM_CHAT_ID": "42", "JWT_SECRET": "growth"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))

    def tearDown(self):
        self.env.stop()

    def test_telegram_send_and_opt_out(self):
        with mock.patch.object(notify.requests, "post", return_value=FakeResp({"ok": True})) as post:
            self.assertTrue(notify.drafts_waiting(self.uid, 3))
        self.assertIn("/botbot1/sendMessage", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "42")
        self.assertIn("3 new post", post.call_args.kwargs["json"]["text"])
        with mock.patch.object(notify.requests, "post", side_effect=notify.requests.ConnectionError("down")):
            self.assertFalse(notify.send(self.uid, "x"))               # never raises
        with get_connection() as conn:
            conn.execute("UPDATE users SET notify_telegram = 0 WHERE id = ?", (self.uid,))
        with mock.patch.object(notify.requests, "post", side_effect=AssertionError("must not send")):
            self.assertFalse(notify.send(self.uid, "x"))
        self.assertFalse(notify.send(None, "x"))

    def test_evergreen_recycling(self):
        old = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        with get_connection() as conn:
            due = conn.execute("INSERT INTO posts (user_id, status, topic, caption, hashtags, image_path, platforms, published_at, "
                               "evergreen, recycle_days, fb_post_id) VALUES (?, 'published', 'Classic', 'c', '#a', '/img.jpg', "
                               "'facebook', ?, 1, 30, 'fb1')", (self.uid, old)).lastrowid
            conn.execute("INSERT INTO posts (user_id, status, topic, published_at, evergreen, recycle_days) "
                         "VALUES (?, 'published', 'TooNew', ?, 1, 30)", (self.uid, fresh))
            conn.execute("INSERT INTO posts (user_id, status, topic, published_at, evergreen) VALUES (?, 'published', 'NotEver', ?, 0)",
                         (self.uid, old))
        self.assertEqual([p["id"] for p in recycle.due_posts()], [due])
        self.assertEqual(recycle.run_step(), 0)
        drafts = queries.list_posts(status="draft", user_id=self.uid)
        self.assertEqual(len(drafts), 1)
        new = drafts[0]
        self.assertEqual((new["topic"], new["caption"], new["image_path"], new["platforms"], new["recycled_from"]),
                         ("Classic", "c", "/img.jpg", "facebook", due))
        self.assertIsNone(new["fb_post_id"])
        self.assertEqual(new["evergreen"], 1)
        self.assertEqual(recycle.due_posts(), [])          # not recycled twice
        # toggle from the post page
        r = self.client.post(f"/posts/{new['id']}/evergreen", data={"evergreen": "1", "recycle_days": "14"}, follow_redirects=False)
        self.assertIn("14%20days", r.headers["location"])
        self.assertEqual(queries.get_post(new["id"])["recycle_days"], 14)

    def test_approve_next_slot_and_posting_settings(self):
        pid = queries.create_draft("x", "c", user_id=self.uid)
        r = self.client.post("/profile/posting", data={"posting_slots": "Mon,Tue 09:00", "hashtags_as_comment": "1",
                                                       "notify_telegram": "1"}, follow_redirects=False)
        self.assertIn("msg=", r.headers["location"])
        u = auth.get_user(self.uid)
        self.assertEqual((u["posting_slots"], u["hashtags_as_comment"], u["notify_telegram"]), ("Mon,Tue 09:00", 1, 1))
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))
        r = self.client.post(f"/posts/{pid}/approve", data={"platforms": "facebook", "scheduled_time": "next"}, follow_redirects=False)
        self.assertIn("approved", r.headers["location"])
        post = queries.get_post(pid)
        self.assertEqual(post["status"], "scheduled")
        self.assertRegex(post["scheduled_time"], r"\d{4}-\d{2}-\d{2} 09:00")
        self.assertIn(datetime.strptime(post["scheduled_time"], "%Y-%m-%d %H:%M").strftime("%a"), ("Mon", "Tue"))
        page = self.client.get("/profile").text
        self.assertIn("Next slots:", page)
        r = self.client.post("/profile/posting", data={"posting_slots": "Blursday 09:00"}, headers={"referer": "http://x/profile"},
                             follow_redirects=False)
        self.assertIn("err=", r.headers["location"])


if __name__ == "__main__":
    unittest.main()


class MetricsFailureBackoffTests(unittest.TestCase):
    """A failed insights call is remembered, so the worker does not retry it every 5 minutes."""

    def test_failed_fetch_is_recorded_and_not_retried_until_stale(self):
        from src import analytics
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")
            conn.execute("DELETE FROM post_metrics")
            conn.execute("DELETE FROM users WHERE email = 'mf@example.com'")
        uid = auth.create_user("mf@example.com", "password123", "MF")["id"]     # outside the write transaction
        with get_connection() as conn:
            pid = conn.execute("INSERT INTO posts (user_id, topic, status, published_at, ig_post_id) VALUES (?, 'x', 'published', ?, '171')",
                               (uid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
        bad = FakeResp({"error": {"code": 100, "message": "Unsupported get request"}}, 400)
        with mock.patch.dict(os.environ, {"META_ACCESS_TOKEN": "t", "META_PAGE_ID": "1", "META_INSTAGRAM_ACCOUNT_ID": "2"}), \
             mock.patch.object(publisher.requests, "request", return_value=bad) as req:
            self.assertEqual(analytics.run_step(uid), 0)               # not a pipeline failure
            self.assertEqual(req.call_count, 1)
            self.assertEqual(analytics.run_step(uid), 0)
            self.assertEqual(req.call_count, 1)                        # not asked again this cycle
        with get_connection() as conn:
            row = conn.execute("SELECT fetched_at, raw FROM post_metrics WHERE post_id = ?", (pid,)).fetchone()
        self.assertTrue(row["fetched_at"])
        self.assertIn("Unsupported get request", row["raw"])
