"""Multi-tenant isolation: user A must never see or touch user B's posts, runs, files or keys."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import main as orch                            # noqa: E402
from src import publisher, uploader, image_generator    # noqa: E402
from src.security import crypto, secrets                # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

KEY = crypto.generate_key()


def insert_post(user_id, **overrides):
    row = {"topic": f"Topic of {user_id}", "caption": "c", "status": "draft", "user_id": user_id}
    row.update(overrides)
    cols, params = ",".join(row), ",".join(f":{k}" for k in row)
    with get_connection() as conn:
        return conn.execute(f"INSERT INTO posts ({cols}) VALUES ({params})", row).lastrowid


class TenancyTests(unittest.TestCase):
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
            for t in ("posts", "users", "credentials", "pipeline_runs"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY})
        self.env.start()
        self.alice = auth.create_user("alice@example.com", "password123", "Alice")
        self.bob = auth.create_user("bob@example.com", "password123", "Bob")
        self.tmp = tempfile.TemporaryDirectory()
        video = Path(self.tmp.name) / "bob.mp4"
        video.write_bytes(b"\x00" * 64)
        self.alice_post = insert_post(self.alice["id"], topic="Alice secret launch")
        self.bob_post = insert_post(self.bob["id"], topic="Bob private video", media_type="video",
                                    video_path=str(video), last_error="bob: broken token")
        with get_connection() as conn:
            conn.execute("INSERT INTO pipeline_runs (started_at, steps, ok, summary, log, user_id) "
                         "VALUES ('2026-11-03 10:00:00', 'publish', 1, 'ok', 'BOB RUN LOG', ?)", (self.bob["id"],))
            self.bob_run = conn.execute("SELECT id FROM pipeline_runs").fetchone()["id"]
        self.login(self.alice)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def login(self, user):
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(user["id"]))

    # ----- pages and API -----

    def test_pages_show_only_own_posts(self):
        for path in ("/", "/queue", "/logs", "/calendar"):
            text = self.client.get(path).text
            self.assertNotIn("Bob private", text, path)
            self.assertNotIn("BOB RUN LOG", text, path)
        self.assertIn("Alice secret launch", self.client.get("/queue").text)

    def test_api_lists_only_own_posts(self):
        ids = {p["id"] for p in self.client.get("/api/posts").json()}
        self.assertEqual(ids, {self.alice_post})
        self.assertEqual(self.client.get("/api/stats").json()["total"], 1)
        self.assertEqual(self.client.get("/api/stats").json()["failed"], 0)
        self.assertEqual(self.client.get("/api/errors").json(), [])
        self.assertEqual(self.client.get("/api/runs").json(), [])
        self.assertEqual(self.client.get("/api/posts?q=Bob").json(), [])

    def test_direct_access_to_other_users_post_is_404(self):
        self.assertEqual(self.client.get(f"/posts/{self.bob_post}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/posts/{self.bob_post}").status_code, 404)
        self.assertEqual(self.client.get(f"/logs/runs/{self.bob_run}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/runs/{self.bob_run}").status_code, 404)
        # own post works
        self.assertEqual(self.client.get(f"/posts/{self.alice_post}").status_code, 200)

    def test_cannot_modify_other_users_post(self):
        for action, data in (("approve", {}), ("reject", {}), ("unapprove", {}), ("clear-error", {}),
                             ("edit", {"caption": "hacked"}), ("delete", {})):
            r = self.client.post(f"/posts/{self.bob_post}/{action}", data=data, follow_redirects=False)
            self.assertEqual(r.status_code, 404, action)
        self.assertEqual(self.client.post(f"/api/posts/{self.bob_post}/status", json={"status": "approved"}).status_code, 404)
        bob = queries.get_post(self.bob_post)
        self.assertEqual(bob["status"], "draft")
        self.assertEqual(bob["caption"], "c")
        self.assertEqual(bob["last_error"], "bob: broken token")

    def test_cannot_download_other_users_files(self):
        self.assertEqual(self.client.get(f"/files/{self.bob_post}/video").status_code, 404)
        self.login(self.bob)
        self.assertEqual(self.client.get(f"/files/{self.bob_post}/video").status_code, 200)

    def test_settings_are_per_user(self):
        self.client.post("/settings", data={"name": "META_ACCESS_TOKEN", "value": "alice-secret-token"},
                         follow_redirects=False)
        self.assertIn("yours", self.client.get("/settings").text)
        self.login(self.bob)
        page = self.client.get("/settings").text
        self.assertNotIn("yours · encrypted", page)
        self.assertNotIn("alice-secret-token", page)       # neither the value...
        self.assertNotIn("ali...oken", page)               # ...nor its masked form
        with self.assertRaises(secrets.ConfigError):
            secrets.get_secret("META_ACCESS_TOKEN", self.bob["id"])
        self.assertEqual(secrets.get_secret("META_ACCESS_TOKEN", self.alice["id"]), "alice-secret-token")

    def test_pipeline_runs_are_scoped_to_user(self):
        with mock.patch.object(webapp.pipeline, "run_pipeline") as run:
            self.client.post("/pipeline/run", data={"steps": "publish"}, follow_redirects=False)
        self.assertEqual(run.call_args.kwargs["user_id"], self.alice["id"])

    def test_new_posts_belong_to_the_creator(self):
        with get_connection() as conn:
            conn.execute("UPDATE users SET tier = 'enterprise' WHERE id = ?", (self.alice["id"],))
        self.client.post("/posts/new", data={"topic": "Mine", "caption": "x"}, follow_redirects=False)
        mine = [p for p in queries.list_posts() if p["topic"] == "Mine"]
        self.assertEqual(mine[0]["user_id"], self.alice["id"])
        self.login(self.bob)
        self.assertNotIn("Mine", self.client.get("/queue").text)

    # ----- pipeline scripts -----

    def test_scripts_use_the_posts_owners_credentials(self):
        secrets.set_secret("META_ACCESS_TOKEN", "alice-token", user_id=self.alice["id"])
        secrets.set_secret("META_PAGE_ID", "alice-page", user_id=self.alice["id"])
        secrets.set_secret("META_ACCESS_TOKEN", "bob-token", user_id=self.bob["id"])
        secrets.set_secret("META_PAGE_ID", "bob-page", user_id=self.bob["id"])
        with get_connection() as conn:
            conn.execute("UPDATE posts SET status = 'approved', media_type = 'image', video_path = NULL")
        seen = {}

        def fake(method, url, **kw):
            seen[kw["data"]["access_token"]] = url
            return mock.MagicMock(status_code=200, json=lambda: {"id": "x", "post_id": "p"})

        with mock.patch.object(publisher.requests, "request", side_effect=fake):
            publisher.main(["--platforms", "facebook"])
        self.assertIn("/alice-page/", seen["alice-token"])
        self.assertIn("/bob-page/", seen["bob-token"])

    def test_user_filter_limits_each_step(self):
        with get_connection() as conn:
            conn.execute("UPDATE posts SET status = 'approved'")
        self.assertEqual([p["id"] for p in publisher.fetch_due_posts(user_id=self.alice["id"])], [self.alice_post])
        self.assertEqual(len(publisher.fetch_due_posts()), 2)
        self.assertEqual([p["id"] for p in uploader.fetch_posts_needing_hosting(user_id=self.alice["id"])], [])
        self.assertEqual([p["id"] for p in uploader.fetch_posts_needing_hosting(user_id=self.bob["id"])], [self.bob_post])

    def test_orchestrator_passes_user_to_every_step(self):
        calls = []
        with mock.patch.object(orch.image_generator, "main", lambda a: calls.append(("images", a)) or 0), \
             mock.patch.object(orch.publisher, "main", lambda a: calls.append(("publish", a)) or 0):
            result = orch.run_pipeline(steps=["images", "publish"], user_id=self.alice["id"])
        self.assertEqual(calls, [("images", ["--user", str(self.alice["id"])]),
                                 ("publish", ["--user", str(self.alice["id"])])])
        with get_connection() as conn:
            run = conn.execute("SELECT user_id FROM pipeline_runs WHERE id = ?", (result["id"],)).fetchone()
        self.assertEqual(run["user_id"], self.alice["id"])
        self.assertEqual(queries.get_run(result["id"], self.bob["id"]), None)

    def test_loop_runs_once_per_user(self):
        seen = []
        with mock.patch.object(orch, "run_pipeline", lambda **kw: seen.append(kw["user_id"]) or {"ok": True}):
            orch.run_loop(interval_minutes=0.0001, max_runs=1)
        self.assertEqual(sorted(seen), sorted([self.alice["id"], self.bob["id"]]))


if __name__ == "__main__":
    unittest.main()
