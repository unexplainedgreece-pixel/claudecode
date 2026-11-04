"""Carousel posts: several library photos in one Instagram / Facebook post."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["SCHEDULER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import carousel, library, publisher, review, uploader  # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, ""

    def json(self):
        return self._p


class CarouselTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(webapp.app)
        cls.client.__enter__()
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        init_db()
        with get_connection() as conn:
            for t in ("posts", "post_media", "users", "pages", "assets", "review_links"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"JWT_SECRET": "car"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.uid = auth.create_user("car@example.com", "password123", "Hotel Blue")["id"]
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))
        from PIL import Image
        self.lib = Path(tempfile.mkdtemp())
        lib_patch = mock.patch.object(library, "LIBRARY_DIR", self.lib)
        lib_patch.start()
        self.addCleanup(lib_patch.stop)
        self.tmp = str(library.user_dir(self.uid))          # the page rescans this folder
        self.assets = []
        for i in range(3):
            path = Path(self.tmp) / f"room{i}.png"
            Image.new("RGB", (40, 30), (i * 50, 100, 150)).save(path)
            with get_connection() as conn:
                aid = conn.execute(
                    "INSERT INTO assets (user_id, path, kind, caption, hashtags, analysis) VALUES (?, ?, 'image', ?, ?, ?)",
                    (self.uid, str(path), f"Room {i} with sea view" if i else "", "#hotel #rhodes" if i else "",
                     json.dumps({"hook": f"Sea view room {i}"}) if i else None)).lastrowid
            self.assets.append(aid)
        with get_connection() as conn:
            self.video_asset = conn.execute("INSERT INTO assets (user_id, path, kind) VALUES (?, ?, 'video')",
                                            (self.uid, str(Path(self.tmp) / "clip.mp4"))).lastrowid

    def test_create_from_assets_uses_first_analysed_caption_and_keeps_order(self):
        pid = carousel.create_from_assets(self.uid, [self.assets[2], self.assets[0], self.assets[1]])
        post = queries.get_post(pid)
        self.assertEqual((post["media_type"], post["source"], post["topic"]), ("carousel", "library", "Sea view room 2"))
        self.assertEqual(post["caption"], "Room 2 with sea view")
        self.assertEqual(post["image_path"], str(Path(self.tmp) / "room2.png"))
        slides = carousel.items(pid)
        self.assertEqual([s["asset_id"] for s in slides], [self.assets[2], self.assets[0], self.assets[1]])
        self.assertEqual([s["position"] for s in slides], [0, 1, 2])
        for bad in ([self.assets[0]], self.assets + [self.video_asset], [self.assets[0], 99999], ["x", "y"]):
            with self.assertRaises(carousel.CarouselError):
                carousel.create_from_assets(self.uid, bad)
        other = auth.create_user("o@example.com", "password123", "O")["id"]
        with self.assertRaises(carousel.CarouselError):
            carousel.create_from_assets(other, self.assets[:2])             # not their photos

    def test_uploader_hosts_every_slide_then_marks_the_post(self):
        pid = carousel.create_from_assets(self.uid, self.assets)
        post = uploader.fetch_posts_needing_hosting(user_id=self.uid)
        self.assertEqual([p["id"] for p in post], [pid])
        calls = []

        def fake_upload(jpeg_path, post):
            calls.append(jpeg_path)
            return f"https://cdn/{Path(jpeg_path).stem}.jpg", f"ref-{len(calls)}"
        self.assertTrue(uploader.process_post(queries.get_post(pid), fake_upload))
        self.assertEqual(len(calls), 3)
        post = queries.get_post(pid)
        self.assertTrue(post["hosted_at"])
        self.assertEqual(post["image_url"], "https://cdn/room0.jpg")
        self.assertEqual(carousel.hosted_urls(pid), ["https://cdn/room0.jpg", "https://cdn/room1.jpg", "https://cdn/room2.jpg"])
        self.assertEqual(uploader.fetch_posts_needing_hosting(user_id=self.uid), [])
        # a failing slide leaves a clear error and does not mark the post hosted
        pid2 = carousel.create_from_assets(self.uid, self.assets[:2])

        def failing(jpeg_path, post):
            raise uploader.UploadError("R2 said no")
        self.assertFalse(uploader.process_post(queries.get_post(pid2), failing))
        self.assertIn("photo 1: R2 said no", queries.get_post(pid2)["last_error"])

    def _publish(self, pid, platforms):
        calls = []

        def fake(method, url, **kw):
            data = dict(kw.get("data") or {})
            if kw.get("files"):
                data["<file>"] = kw["files"]["source"][0]
            calls.append((method, url, data))
            if url.endswith("/photos"):
                return FakeResp({"id": f"ph{len(calls)}"})
            if url.endswith("/feed"):
                return FakeResp({"id": "fbpost1"})
            if url.endswith("/media"):
                return FakeResp({"id": f"c{len(calls)}"})
            if method == "GET":
                return FakeResp({"status_code": "FINISHED"})
            if url.endswith("/media_publish"):
                return FakeResp({"id": "ig77"})
            return FakeResp({"id": "x"})
        post = queries.get_post(pid)
        creds = {"token": "tok", "ig_id": "IG", "page_id": "PG"}
        with mock.patch.object(publisher.requests, "request", side_effect=fake), \
             mock.patch.object(publisher.time, "sleep", lambda s: None), \
             mock.patch.object(publisher, "meta_credentials", return_value=creds):
            results = {p: publisher.PUBLISHERS[p](post) for p in platforms}
        return results, calls

    def test_facebook_multi_photo_post_from_local_files_and_hosted_links(self):
        pid = carousel.create_from_assets(self.uid, self.assets)
        results, calls = self._publish(pid, ["facebook"])
        self.assertEqual(results["facebook"], "fbpost1")
        photos = [c for c in calls if c[1].endswith("/PG/photos")]
        self.assertEqual(len(photos), 3)
        self.assertTrue(all(c[2]["published"] == "false" and "<file>" in c[2] for c in photos))
        feed = next(c for c in calls if c[1].endswith("/PG/feed"))
        self.assertEqual(json.loads(feed[2]["attached_media[0]"]), {"media_fbid": "ph1"})
        self.assertEqual(json.loads(feed[2]["attached_media[2]"]), {"media_fbid": "ph3"})
        self.assertEqual(feed[2]["message"], "Room 1 with sea view\n\n#hotel #rhodes")
        # hosted: links instead of files
        uploader.process_post(queries.get_post(pid), lambda jp, p: (f"https://cdn/{Path(jp).stem}.jpg", "r"))
        results, calls = self._publish(pid, ["facebook"])
        photos = [c for c in calls if c[1].endswith("/PG/photos")]
        self.assertEqual([c[2]["url"] for c in photos], ["https://cdn/room0.jpg", "https://cdn/room1.jpg", "https://cdn/room2.jpg"])

    def test_instagram_carousel_needs_hosting_then_uses_children_containers(self):
        pid = carousel.create_from_assets(self.uid, self.assets)
        with self.assertRaises(publisher.PublishError) as cm:
            self._publish(pid, ["instagram"])
        self.assertIn("public link for every photo", str(cm.exception))
        uploader.process_post(queries.get_post(pid), lambda jp, p: (f"https://cdn/{Path(jp).stem}.jpg", "r"))
        results, calls = self._publish(pid, ["instagram"])
        self.assertEqual(results["instagram"], "ig77")
        containers = [c for c in calls if c[1].endswith("/IG/media")]
        self.assertEqual(len(containers), 4)
        self.assertTrue(all(c[2]["is_carousel_item"] == "true" for c in containers[:3]))
        self.assertEqual([c[2]["image_url"] for c in containers[:3]], ["https://cdn/room0.jpg", "https://cdn/room1.jpg", "https://cdn/room2.jpg"])
        parent = containers[3][2]
        self.assertEqual((parent["media_type"], parent["children"]), ("CAROUSEL", "c1,c2,c3"))
        self.assertIn("#hotel", parent["caption"])
        self.assertNotIn("is_ai_generated", parent)
        publish = [c for c in calls if c[1].endswith("/media_publish")]
        self.assertEqual(len(publish), 1)

    def test_carousels_go_to_facebook_and_instagram_only(self):
        pid = carousel.create_from_assets(self.uid, self.assets[:2])
        post = queries.get_post(pid)
        with mock.patch.object(publisher, "configured_platforms", return_value=["facebook", "instagram", "tiktok", "threads"]):
            self.assertEqual(publisher.post_platforms(post, list(publisher.ALL_PLATFORMS)), ["facebook", "instagram"])
        queries.update_post(pid, platforms=["instagram", "youtube"])
        self.assertEqual(publisher.post_platforms(queries.get_post(pid), list(publisher.ALL_PLATFORMS)), ["instagram"])

    def test_library_form_creates_the_post_and_pages_show_the_slides(self):
        r = self.client.post("/library/carousel", data={"asset_ids": [str(a) for a in self.assets]}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/posts/", r.headers["location"])
        pid = int(r.headers["location"].split("/posts/")[1].split("?")[0])
        self.assertEqual(len(carousel.items(pid)), 3)
        r = self.client.post("/library/carousel", data={"asset_ids": [str(self.assets[0])]}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])
        html = self.client.get("/queue").text
        self.assertIn("carousel · 3 photos", html)
        self.assertIn(f"/files/{pid}/carousel/2", html)
        self.assertEqual(self.client.get(f"/files/{pid}/carousel/2").status_code, 200)
        self.assertEqual(self.client.get(f"/files/{pid}/carousel/3").status_code, 404)
        self.assertIn("Carousel (3 photos)", self.client.get(f"/posts/{pid}").text)
        self.assertIn('name="asset_ids"', self.client.get("/library").text)
        # client review page shows the strip through the token
        link = review.create_link(self.uid, "Hotel")
        self.client.cookies.clear()
        html = self.client.get(f"/review/{link['token']}").text
        self.assertIn(f"/review/{link['token']}/files/{pid}/carousel/1", html)
        self.assertEqual(self.client.get(f"/review/{link['token']}/files/{pid}/carousel/1").status_code, 200)
        self.assertEqual(self.client.get(f"/review/{link['token']}/files/{pid}/carousel/9").status_code, 404)


if __name__ == "__main__":
    unittest.main()
