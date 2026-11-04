"""Connected Facebook Pages: import from a token, per-post page choice, publisher credentials, tenancy."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from PIL import Image                                   # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import pages, publisher                        # noqa: E402
from src.security import crypto                         # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

KEY = crypto.generate_key()
ME_ACCOUNTS = {"data": [
    {"id": "1410574492136860", "name": "Unexplained Greece", "access_token": "EAApageA",
     "instagram_business_account": {"id": "17841425261682291", "username": "unexplained.greece"}},
    {"id": "1220614701127481", "name": "Rhodes Travel Guide", "access_token": "EAApageB"},
]}


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, ""

    def json(self):
        return self._p


class PagesTests(unittest.TestCase):
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
            for t in ("posts", "users", "pages", "credentials"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY, "JWT_SECRET": "pages-tests"})
        self.env.start()
        self.user = auth.create_user("pg@example.com", "password123")
        with get_connection() as conn:
            conn.execute("UPDATE users SET tier = 'enterprise' WHERE id = ?", (self.user["id"],))
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.user["id"]))

    def tearDown(self):
        self.env.stop()

    def connect(self, token="EAAusertoken"):
        with mock.patch.object(pages.requests, "get", return_value=FakeResp(ME_ACCOUNTS)) as get:
            r = self.client.post("/settings/pages/connect", data={"token": token}, follow_redirects=False)
        return r, get

    # ----- connecting -----

    def test_connect_imports_pages_with_tokens_encrypted(self):
        r, get = self.connect()
        self.assertIn("Connected%202%20Page", r.headers["location"])
        self.assertEqual(get.call_args.kwargs["params"]["access_token"], "EAAusertoken")
        self.assertIn("instagram_business_account", get.call_args.kwargs["params"]["fields"])
        stored = pages.list_pages(self.user["id"])
        self.assertEqual([p["name"] for p in stored], ["Unexplained Greece", "Rhodes Travel Guide"])
        self.assertEqual(stored[0]["ig_username"], "unexplained.greece")
        self.assertIsNone(stored[1]["ig_account_id"])
        with get_connection() as conn:
            raw = conn.execute("SELECT token_encrypted FROM pages").fetchone()["token_encrypted"]
        self.assertTrue(raw.startswith("v1:"))
        self.assertNotIn("EAApage", raw)
        self.assertEqual(pages.get_page(stored[0]["id"], self.user["id"])["token"], "EAApageA")
        page = self.client.get("/settings").text
        self.assertIn("Unexplained Greece", page)
        self.assertIn("@unexplained.greece", page)
        self.assertIn("none linked", page)

    def test_reconnect_updates_instead_of_duplicating(self):
        self.connect()
        self.connect("EAAnewer")
        self.assertEqual(len(pages.list_pages(self.user["id"])), 2)

    def test_connect_errors_are_clean(self):
        r = self.client.post("/settings/pages/connect", data={"token": "nope"}, follow_redirects=False)
        self.assertIn("start%20with%20EAA", r.headers["location"])
        bad = FakeResp({"error": {"message": "Invalid OAuth access token", "code": 190}}, 400)
        with mock.patch.object(pages.requests, "get", return_value=bad):
            r = self.client.post("/settings/pages/connect", data={"token": "EAAbad"}, follow_redirects=False)
        self.assertIn("Invalid%20OAuth", r.headers["location"])
        with mock.patch.object(pages.requests, "get", return_value=FakeResp({"data": []})):
            r = self.client.post("/settings/pages/connect", data={"token": "EAAempty"}, follow_redirects=False)
        self.assertIn("can%27t%20manage%20any%20Page", r.headers["location"])
        self.assertEqual(pages.list_pages(self.user["id"]), [])

    def test_disconnect_clears_posts_page(self):
        self.connect()
        pid_page = pages.list_pages(self.user["id"])[0]["id"]
        post = queries.create_draft("t", "c", user_id=self.user["id"], page_id=pid_page)
        self.client.post(f"/settings/pages/{pid_page}/delete", follow_redirects=False)
        self.assertEqual(len(pages.list_pages(self.user["id"])), 1)
        self.assertIsNone(queries.get_post(post)["page_id"])

    # ----- per-post choice -----

    def test_forms_offer_page_choice_and_store_it(self):
        self.connect()
        a, b = [p["id"] for p in pages.list_pages(self.user["id"])]
        dash = self.client.get("/").text
        self.assertIn('name="page_id"', dash)
        self.assertIn("Rhodes Travel Guide", dash)
        r = self.client.post("/posts/new", data={"topic": "x", "caption": "c", "page_id": str(b)}, follow_redirects=False)
        pid = int(r.headers["location"].split("/posts/")[1].split("?")[0])
        self.assertEqual(queries.get_post(pid)["page_id"], b)
        self.client.post(f"/posts/{pid}/approve", data={"platforms": "facebook", "page_id": str(a)}, follow_redirects=False)
        self.assertEqual(queries.get_post(pid)["page_id"], a)
        # a page id that is not mine falls back to my default page
        self.client.post(f"/posts/{pid}/edit", data={"topic": "x", "page_id": "999999"}, follow_redirects=False)
        self.assertEqual(queries.get_post(pid)["page_id"], a)

    def test_upload_accepts_photos_and_sets_page(self):
        self.connect()
        b = pages.list_pages(self.user["id"])[1]["id"]
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (200, 10, 10)).save(buf, "PNG")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(webapp, "UPLOAD_IMAGE_DIR", Path(tmp)):
            r = self.client.post("/api/posts/upload", data={"topic": "Photo", "page_id": str(b)},
                                 files={"file": ("sunset.png", io.BytesIO(buf.getvalue()), "image/png")})
            self.assertEqual(r.status_code, 200, r.text)
            post = queries.get_post(r.json()["id"])
            self.assertEqual((post["media_type"], post["source"], post["page_id"]), ("image", "upload", b))
            self.assertTrue(Path(post["image_path"]).exists())
            self.assertIsNone(post["video_path"])
            r = self.client.post("/api/posts/upload", data={},
                                 files={"file": ("fake.png", io.BytesIO(b"not an image"), "image/png")})
            self.assertEqual(r.status_code, 400)

    def test_attach_media_to_existing_post(self):
        pid = queries.create_draft("t", "c", user_id=self.user["id"])
        buf = io.BytesIO()
        Image.new("RGB", (16, 16)).save(buf, "JPEG")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(webapp, "UPLOAD_IMAGE_DIR", Path(tmp)):
            r = self.client.post(f"/posts/{pid}/media", files={"file": ("me.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")},
                                 follow_redirects=False)
            self.assertIn("msg=", r.headers["location"])
            post = queries.get_post(pid)
            self.assertTrue(post["image_path"].endswith(".jpg"))
            self.assertEqual(post["upload_name"], "me.jpg")
            r = self.client.post(f"/posts/{pid}/media", files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
                                 follow_redirects=False)
            self.assertIn("err=", r.headers["location"])

    # ----- publisher -----

    def test_publisher_uses_the_posts_page_credentials(self):
        self.connect()
        a, b = pages.list_pages(self.user["id"])
        pa = queries.create_draft("to A", "c", user_id=self.user["id"], page_id=a["id"], platforms=["facebook", "instagram"])
        pb = queries.create_draft("to B", "c", user_id=self.user["id"], page_id=b["id"], platforms=["facebook"])
        for pid in (pa, pb):
            queries.set_status(pid, "approved", "", platforms=None)
        with get_connection() as conn:
            conn.execute("UPDATE posts SET image_url = 'https://img.example/a.jpg'")
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url, kw.get("data") or kw.get("params") or {}))
            if url.endswith("/media"):
                return FakeResp({"id": "cont"})
            if "cont" in url and method == "GET":
                return FakeResp({"status_code": "FINISHED"})
            if url.endswith("/media_publish"):
                return FakeResp({"id": "ig1"})
            return FakeResp({"id": "x", "post_id": "fb1"})

        with mock.patch.object(publisher.requests, "request", side_effect=fake), \
             mock.patch.object(publisher.time, "sleep", lambda s: None):
            self.assertEqual(publisher.main([]), 0)
        fb_a = next(c for c in calls if "/1410574492136860/photos" in c[1])
        self.assertEqual(fb_a[2]["access_token"], "EAApageA")
        fb_b = next(c for c in calls if "/1220614701127481/photos" in c[1])
        self.assertEqual(fb_b[2]["access_token"], "EAApageB")
        ig = next(c for c in calls if "/17841425261682291/media" in c[1])
        self.assertEqual(ig[2]["access_token"], "EAApageA")
        self.assertEqual(queries.get_post(pa)["status"], "published")
        self.assertEqual(queries.get_post(pb)["status"], "published")

    def test_instagram_refused_for_page_without_instagram(self):
        self.connect()
        b = pages.list_pages(self.user["id"])[1]
        pid = queries.create_draft("no ig", "c", user_id=self.user["id"], page_id=b["id"], platforms=["instagram"])
        queries.set_status(pid, "approved", "")
        with get_connection() as conn:
            conn.execute("UPDATE posts SET image_url = 'https://img.example/a.jpg'")
        with mock.patch.object(publisher.requests, "request", side_effect=AssertionError("must not call")):
            self.assertEqual(publisher.main([]), 1)
        self.assertIn("no Instagram business account", queries.get_post(pid)["last_error"])

    def test_configured_platforms_from_pages(self):
        self.assertNotIn("facebook", publisher.configured_platforms(self.user["id"]))
        self.connect()
        self.assertEqual(publisher.configured_platforms(self.user["id"])[:2], ["facebook", "instagram"])

    # ----- tenancy -----

    def test_pages_are_private_to_their_user(self):
        self.connect()
        mine = pages.list_pages(self.user["id"])[0]["id"]
        other = auth.create_user("other@example.com", "password123")
        self.assertIsNone(pages.get_page(mine, other["id"]))
        self.assertEqual(pages.list_pages(other["id"]), [])
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(other["id"]))
        self.assertNotIn("Unexplained Greece", self.client.get("/settings").text)
        self.client.post(f"/settings/pages/{mine}/delete", follow_redirects=False)
        self.assertEqual(len(pages.list_pages(self.user["id"])), 2)   # untouched
        pid = queries.create_draft("x", "c", user_id=other["id"])
        self.client.post(f"/posts/{pid}/approve", data={"platforms": "facebook", "page_id": str(mine)}, follow_redirects=False)
        self.assertIsNone(queries.get_post(pid)["page_id"])            # cannot borrow my page


if __name__ == "__main__":
    unittest.main()
