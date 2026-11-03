"""
Integration tests for the dashboard (src/web/app.py) using FastAPI's TestClient.

Run from the project root:
    python3 -m unittest tests.test_web -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src.security import crypto                         # noqa: E402
from src.web import app as webapp                       # noqa: E402

KEY = crypto.generate_key()


def insert_post(**overrides):
    row = {"topic": "Topic", "caption": "Caption text", "hashtags": "#a", "status": "draft"}
    row.update(overrides)
    cols = ",".join(row)
    params = ",".join(f":{k}" for k in row)
    with get_connection() as conn:
        return conn.execute(f"INSERT INTO posts ({cols}) VALUES ({params})", row).lastrowid


def get_post(pid):
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


class WebTests(unittest.TestCase):
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
            conn.execute("DELETE FROM posts")
            conn.execute("DELETE FROM credentials")
            conn.execute("DELETE FROM pipeline_runs")
        self.env = mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    # ----- pages -----

    def test_dashboard_metrics(self):
        insert_post(status="draft")
        insert_post(status="draft", last_error="upload: boom")
        insert_post(status="scheduled", scheduled_time="2099-01-01 10:00")
        insert_post(status="published", published_at="2026-01-01 10:00")
        insert_post(status="published", published_at="2026-01-02 10:00")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["draft"], 2)
        self.assertEqual(stats["scheduled"], 1)
        self.assertEqual(stats["published"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["awaiting_image"], 2)
        self.assertEqual(stats["total"], 5)
        for key in ("draft", "scheduled", "published", "failed"):
            self.assertIn(f'data-stat="{key}"', r.text)

    def test_queue_lists_drafts_ready_first(self):
        no_img = insert_post(topic="NoImage")
        ready = insert_post(topic="Ready", hosted_at="2026-01-01 10:00",
                            image_url="https://i.imgur.com/x.jpeg")
        insert_post(topic="Done", status="published")
        r = self.client.get("/queue")
        self.assertEqual(r.status_code, 200)
        self.assertLess(r.text.index("Ready"), r.text.index("NoImage"))
        self.assertNotIn(">Done<", r.text)
        self.assertIn(f"/posts/{ready}/approve", r.text)
        self.assertIn("Approve for publishing", r.text)
        self.assertIn("https://i.imgur.com/x.jpeg", r.text)   # hosted JPEG shown
        self.assertIn(f"/posts/{no_img}/approve", r.text)

    def test_queue_is_paginated(self):
        for i in range(60):
            insert_post(topic=f"Draft{i:03d}")
        r = self.client.get("/queue")
        self.assertIn("page 1 of 3", r.text)
        self.assertIn("Draft059", r.text)
        self.assertNotIn("Draft000", r.text)
        r = self.client.get("/queue?page=3")
        self.assertIn("Draft000", r.text)
        self.assertIn("/queue?page=2", r.text)
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["draft"], 60)

    def test_calendar_shows_scheduled_and_published(self):
        insert_post(topic="SchedTopic", status="scheduled", scheduled_time="2026-11-20 09:30")
        insert_post(topic="PubTopic", status="published", published_at="2026-11-05 14:00:00")
        insert_post(topic="OtherMonth", status="scheduled", scheduled_time="2026-12-01 09:00")
        r = self.client.get("/calendar?year=2026&month=11")
        self.assertEqual(r.status_code, 200)
        self.assertIn("November 2026", r.text)
        self.assertIn("09:30 SchedTopic", r.text)
        self.assertIn("14:00 PubTopic", r.text)
        self.assertNotIn("OtherMonth", r.text)
        self.assertIn("/calendar?year=2026&month=12", r.text)
        self.assertEqual(self.client.get("/calendar").status_code, 200)
        self.assertEqual(self.client.get("/calendar?year=2026&month=13").status_code, 400)

    def test_logs_page_shows_errors_and_runs(self):
        pid = insert_post(last_error="instagram: Meta API error 190: Bad token")
        with get_connection() as conn:
            conn.execute("INSERT INTO pipeline_runs (started_at, finished_at, steps, ok, summary, log) "
                         "VALUES ('2026-11-03 10:00:00','2026-11-03 10:00:05','publish',0,'publish=exit 1','LOGLINE')")
        r = self.client.get("/logs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Meta API error 190", r.text)
        self.assertIn(f"/posts/{pid}/clear-error", r.text)
        self.assertIn("publish=exit 1", r.text)
        self.assertEqual(self.client.get("/api/errors").json()[0]["id"], pid)
        run = self.client.get("/logs/runs/1")
        self.assertIn("LOGLINE", run.text)
        self.assertEqual(self.client.get("/logs/runs/999").status_code, 404)

    def test_post_detail_and_404(self):
        pid = insert_post(topic="DetailTopic", fb_post_id="111_222")
        r = self.client.get(f"/posts/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("DetailTopic", r.text)
        self.assertIn("111_222", r.text)
        self.assertEqual(self.client.get("/posts/9999").status_code, 404)
        self.assertEqual(self.client.get("/api/posts/9999").status_code, 404)

    # ----- approval workflow -----

    def test_approve_now(self):
        pid = insert_post()
        r = self.client.post(f"/posts/{pid}/approve", data={}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        post = get_post(pid)
        self.assertEqual(post["status"], "approved")
        self.assertIsNotNone(post["approved_at"])
        self.assertIsNone(post["scheduled_time"])

    def test_approve_with_future_time_becomes_scheduled(self):
        pid = insert_post()
        when = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")  # datetime-local
        self.client.post(f"/posts/{pid}/approve", data={"scheduled_time": when}, follow_redirects=False)
        post = get_post(pid)
        self.assertEqual(post["status"], "scheduled")
        self.assertEqual(post["scheduled_time"], when.replace("T", " "))

    def test_approve_with_past_time_is_approved_now(self):
        pid = insert_post()
        self.client.post(f"/posts/{pid}/approve", data={"scheduled_time": "2020-01-01T10:00"}, follow_redirects=False)
        self.assertEqual(get_post(pid)["status"], "approved")

    def test_approve_bad_time_is_rejected(self):
        pid = insert_post()
        r = self.client.post(f"/posts/{pid}/approve", data={"scheduled_time": "tomorrow"},
                             headers={"referer": "http://x/queue"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("err=", r.headers["location"])
        self.assertEqual(get_post(pid)["status"], "draft")

    def test_reject_unapprove_delete(self):
        pid = insert_post()
        self.client.post(f"/posts/{pid}/reject", follow_redirects=False)
        self.assertEqual(get_post(pid)["status"], "rejected")
        self.client.post(f"/posts/{pid}/unapprove", follow_redirects=False)
        self.assertEqual(get_post(pid)["status"], "draft")
        self.client.post(f"/posts/{pid}/delete", follow_redirects=False)
        self.assertEqual(self.client.get(f"/api/posts/{pid}").status_code, 404)

    def test_published_post_cannot_be_deleted(self):
        pid = insert_post(status="published")
        r = self.client.post(f"/posts/{pid}/delete", follow_redirects=False)
        self.assertIn("err=", r.headers["location"])
        self.assertEqual(get_post(pid)["status"], "published")

    def test_api_status_endpoint(self):
        pid = insert_post()
        r = self.client.post(f"/api/posts/{pid}/status", json={"status": "approved"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "approved")
        self.assertEqual(self.client.post(f"/api/posts/{pid}/status", json={"status": "published"}).status_code, 400)

    def test_edit_post(self):
        pid = insert_post()
        self.client.post(f"/posts/{pid}/edit", data={"topic": "New", "caption": "Edited caption",
                                                      "hashtags": "#x", "scheduled_time": "2026-12-01T08:00"},
                         follow_redirects=False)
        post = get_post(pid)
        self.assertEqual(post["caption"], "Edited caption")
        self.assertEqual(post["scheduled_time"], "2026-12-01 08:00")
        r = self.client.post(f"/posts/{pid}/edit", data={"scheduled_time": "nope"},
                             headers={"referer": "http://x/posts/1"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])

    def test_clear_error(self):
        pid = insert_post(last_error="x")
        self.client.post(f"/posts/{pid}/clear-error", follow_redirects=False)
        self.assertIsNone(get_post(pid)["last_error"])

    # ----- creating posts and running the pipeline -----

    def test_manual_draft_creation(self):
        r = self.client.post("/posts/new", data={"topic": "Manual", "caption": "My own words",
                                                 "hashtags": "#me"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        posts = self.client.get("/api/posts?status=draft").json()
        self.assertEqual(posts[0]["caption"], "My own words")

    def test_ai_draft_runs_generate_in_background(self):
        with mock.patch.object(webapp.pipeline, "run_pipeline") as run:
            self.client.post("/posts/new", data={"topic": "AI topic", "business": "Nikos"},
                             follow_redirects=False)
        run.assert_called_once_with(steps=["generate"], topics=["AI topic"],
                                    platform="instagram", business="Nikos")

    def test_empty_topic_rejected(self):
        r = self.client.post("/posts/new", data={"topic": "  "}, headers={"referer": "http://x/"},
                             follow_redirects=False)
        self.assertIn("err=", r.headers["location"])

    def test_pipeline_run_button(self):
        with mock.patch.object(webapp.pipeline, "run_pipeline") as run:
            r = self.client.post("/pipeline/run", data={"steps": "images,publish"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/logs?msg="))
        run.assert_called_once_with(steps=["images", "publish"], dry_run=False)
        with mock.patch.object(webapp.pipeline, "run_pipeline") as run:
            self.client.post("/pipeline/run", data={"steps": "upload", "dry_run": "1"}, follow_redirects=False)
        run.assert_called_once_with(steps=["upload"], dry_run=True)
        r = self.client.post("/pipeline/run", data={"steps": "fly"}, headers={"referer": "http://x/logs"},
                             follow_redirects=False)
        self.assertIn("err=", r.headers["location"])

    # ----- settings -----

    def test_settings_saves_encrypted_and_masks(self):
        r = self.client.post("/settings", data={"name": "META_ACCESS_TOKEN", "value": "EAABsecrettoken9Qz"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with get_connection() as conn:
            raw = conn.execute("SELECT value_encrypted FROM credentials").fetchone()[0]
        self.assertTrue(raw.startswith("v1:"))
        self.assertNotIn("secrettoken", raw)
        page = self.client.get("/settings").text
        self.assertIn("EAA...n9Qz", page)
        self.assertNotIn("EAABsecrettoken9Qz", page)
        self.assertIn("encrypted", page)
        self.client.post("/settings/delete", data={"name": "META_ACCESS_TOKEN"}, follow_redirects=False)
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0], 0)

    def test_settings_without_key_shows_warning_and_refuses(self):
        with mock.patch.dict(os.environ, {"APP_SECRET_KEY": ""}):
            page = self.client.get("/settings").text
            self.assertIn("Encryption key missing", page)
            r = self.client.post("/settings", data={"name": "META_PAGE_ID", "value": "123"},
                                 headers={"referer": "http://x/settings"}, follow_redirects=False)
            self.assertIn("err=", r.headers["location"])

    def test_settings_rejects_unknown_and_placeholder(self):
        r = self.client.post("/settings", data={"name": "EVIL", "value": "x"},
                             headers={"referer": "http://x/settings"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])
        r = self.client.post("/settings", data={"name": "META_PAGE_ID", "value": "your_facebook_page_id_here"},
                             headers={"referer": "http://x/settings"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])

    def test_api_posts_filters(self):
        insert_post(topic="Alpha", status="draft")
        insert_post(topic="Beta", status="published")
        self.assertEqual(len(self.client.get("/api/posts?status=published").json()), 1)
        self.assertEqual(self.client.get("/api/posts?q=Alp").json()[0]["topic"], "Alpha")
        self.assertEqual(self.client.get("/api/posts?status=bogus").status_code, 400)
        self.assertTrue(self.client.get("/api/health").json()["ok"])


if __name__ == "__main__":
    unittest.main()
