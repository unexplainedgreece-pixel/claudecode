"""Real photos for AI posts: Wikimedia Commons (licence filtered), Pexels, Pixabay, credit lines."""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ["SCHEDULER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CSRF_DISABLED"] = "1"

from config.db import get_connection, init_db          # noqa: E402
from src import ai_plan, image_generator, library, publisher, stock   # noqa: E402
from src.ai import providers                            # noqa: E402
from src.web import auth, queries                       # noqa: E402


class FakeResp:
    def __init__(self, payload=None, status=200, content=b""):
        self._p, self.status_code, self.content, self.text = payload, status, content, ""

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise stock.requests.HTTPError(str(self.status_code))


def wiki_page(idx, title, license_name, width=2000, mime="image/jpeg", artist="<a href='x'>Nikos</a>"):
    return {"index": idx, "title": title, "imageinfo": [{"thumburl": f"https://upload/{idx}.jpg", "url": f"https://upload/{idx}-orig.jpg",
                                                         "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{idx}",
                                                         "width": width, "mime": mime,
                                                         "extmetadata": {"LicenseShortName": {"value": license_name},
                                                                         "Artist": {"value": artist}}}]}


class WikimediaTests(unittest.TestCase):
    def test_picks_the_first_free_photo_and_skips_bad_licences_small_files_and_non_images(self):
        pages = {"1": wiki_page(1, "a", "CC BY-NC 2.0"), "2": wiki_page(2, "b", "CC BY-SA 4.0", width=500),
                 "3": wiki_page(3, "c", "CC BY-SA 4.0", mime="image/svg+xml"), "4": wiki_page(4, "d", "CC BY-SA 4.0"),
                 "5": wiki_page(5, "e", "CC0", artist="")}
        with mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": pages}})) as get:
            found = stock.search_wikimedia("Baalbek trilithon")
        self.assertEqual(found["url"], "https://upload/4.jpg")
        self.assertEqual(found["credit"], "Photo: Nikos (CC BY-SA 4.0) via Wikimedia Commons")
        self.assertTrue(found["attribution_required"])
        self.assertIn("User-Agent", get.call_args.kwargs["headers"])
        with mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": {"5": pages["5"]}}})):
            found = stock.search_wikimedia("x")
        self.assertFalse(found["attribution_required"])
        self.assertEqual(found["credit"], "Photo: unknown (CC0) via Wikimedia Commons")
        with mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": {"1": pages["1"]}}})):
            with self.assertRaises(stock.StockError):
                stock.search_wikimedia("x")

    def test_find_photo_falls_through_the_sources(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(url)
            if "wikimedia" in url:
                return FakeResp({"query": {"pages": {}}})
            if "pexels" in url:
                return FakeResp({"photos": []})
            return FakeResp({"hits": [{"largeImageURL": "https://pixabay/x.jpg", "user": "Maria", "pageURL": "https://pixabay.com/p/1", "id": 1}]})
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "p", "PIXABAY_API_KEY": "q"}), \
             mock.patch.object(stock.requests, "get", side_effect=fake_get):
            found = stock.find_photo("pyramids of giza")
        self.assertEqual((found["source"], found["url"], found["attribution_required"]), ("pixabay", "https://pixabay/x.jpg", False))
        self.assertEqual(len(calls), 3)
        # no keys at all: wikimedia only, and a clear error when it has nothing
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "", "PIXABAY_API_KEY": ""}), \
             mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": {}}})):
            with self.assertRaises(stock.StockError) as cm:
                stock.find_photo("nothing")
        self.assertIn("wikimedia", str(cm.exception))


class PhotoPostBase(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            for t in ("posts", "users"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": "gem", "JWT_SECRET": "x"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.uid = auth.create_user("ph@example.com", "password123", "UG")["id"]
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, format="JPEG")
        self.jpeg = buf.getvalue()

    def _post(self, source="photo", query="Baalbek trilithon stones"):
        pid = queries.create_draft("Baalbek", "Οι πέτρες.\n\nThe stones.", "#mystery", "huge ancient stones", user_id=self.uid, source="ai")
        with get_connection() as conn:
            conn.execute("UPDATE posts SET image_query = ?, image_source = ?, title_overlay = 'Baalbek' WHERE id = ?", (query, source, pid))
        return pid

class PhotoPostTests(PhotoPostBase):
    def test_photo_post_uses_a_real_photo_and_the_caption_gets_the_credit(self):
        pid = self._post()
        found = {"url": "https://upload/4.jpg", "credit": "Photo: Nikos (CC BY-SA 4.0) via Wikimedia Commons",
                 "link": "https://commons.wikimedia.org/wiki/File:4", "attribution_required": True, "source": "wikimedia"}
        with mock.patch.object(stock, "find_photo", return_value=found), \
             mock.patch.object(providers.requests, "get", return_value=FakeResp(content=self.jpeg)), \
             mock.patch.object(image_generator, "generate_image") as paint, \
             mock.patch("src.overlay.render_title"):
            self.assertTrue(image_generator.process_post(queries.get_post(pid)))
        paint.assert_not_called()
        post = queries.get_post(pid)
        self.assertTrue(post["media_credit"].startswith("CREDIT REQUIRED | Photo: Nikos (CC BY-SA 4.0) via Wikimedia Commons"))
        caption = publisher.full_caption(post)
        self.assertIn("📷 Photo: Nikos (CC BY-SA 4.0) via Wikimedia Commons", caption)
        self.assertNotIn("http", caption.split("#mystery")[0].split("📷")[1])   # no raw link in the caption
        self.assertTrue(caption.endswith("#mystery"))
        self.assertFalse(publisher.is_ai_media(post))

    def test_pexels_photo_needs_no_credit_line_and_ai_fallback_when_nothing_fits(self):
        pid = self._post()
        found = {"url": "https://pexels/x.jpg", "credit": "Photo by Ana on Pexels", "link": "https://pexels.com/p/1",
                 "attribution_required": False, "source": "pexels"}
        with mock.patch.object(stock, "find_photo", return_value=found), \
             mock.patch.object(providers.requests, "get", return_value=FakeResp(content=self.jpeg)), \
             mock.patch("src.overlay.render_title"):
            image_generator.process_post(queries.get_post(pid))
        post = queries.get_post(pid)
        self.assertNotIn("📷", publisher.full_caption(post))
        self.assertEqual(post["media_credit"], "Photo by Ana on Pexels (https://pexels.com/p/1)")
        pid2 = self._post()
        with mock.patch.object(stock, "find_photo", side_effect=stock.StockError("nothing")), \
             mock.patch.object(image_generator, "generate_image", return_value=(self.jpeg, "AI image (gemini)")) as paint, \
             mock.patch("src.overlay.render_title"):
            self.assertTrue(image_generator.process_post(queries.get_post(pid2)))
        paint.assert_called_once()
        self.assertTrue(publisher.is_ai_media(queries.get_post(pid2)))
        # "AI only" posts never look for a photo
        pid3 = self._post(source="ai")
        with mock.patch.object(stock, "find_photo") as find, \
             mock.patch.object(image_generator, "generate_image", return_value=(self.jpeg, "AI image (gemini)")), \
             mock.patch("src.overlay.render_title"):
            image_generator.process_post(queries.get_post(pid3))
        find.assert_not_called()

    def test_planner_stores_the_search_phrase_and_the_users_source_choice(self):
        library.save_brand(self.uid, ai_posts_per_day=1, ai_picture_source="photo", brand_brief="Mysteries")

        def fake_text(prompt, user_id=None, json_mode=False):
            if "Suggest" in prompt:
                return json.dumps({"ideas": [{"topic": "Baalbek", "why": "", "best_day": "", "format": "image"}]})
            return json.dumps({"title": "Baalbek", "caption": "c", "hashtags": ["#x"], "image_prompt": "stones",
                               "image_query": "Baalbek trilithon stones"})
        with mock.patch.object(providers, "text", side_effect=fake_text):
            pid = ai_plan.plan(self.uid, count=1)[0]
        post = queries.get_post(pid)
        self.assertEqual((post["image_query"], post["image_source"]), ("Baalbek trilithon stones", "photo"))
        library.save_brand(self.uid, ai_picture_source="ai")
        with get_connection() as conn:
            conn.execute("UPDATE users SET last_ai_plan_date = NULL WHERE id = ?", (self.uid,))
        with mock.patch.object(providers, "text", side_effect=fake_text):
            pid = ai_plan.plan(self.uid, count=1)[0]
        self.assertEqual(queries.get_post(pid)["image_source"], "ai")


if __name__ == "__main__":
    unittest.main()


class SimplerQueryTests(unittest.TestCase):
    def test_photo_search_retries_with_the_first_two_words(self):
        calls = []

        def find(query, user_id=None):
            calls.append(query)
            if query == "Baalbek trilithon":
                return {"url": "https://u/x.jpg", "credit": "Photo by A on Pexels", "link": "", "attribution_required": False, "source": "pexels"}
            raise stock.StockError("nothing")
        with mock.patch.object(stock, "find_photo", side_effect=find), \
             mock.patch.object(providers.requests, "get", return_value=FakeResp(content=b"jpeg")):
            data, credit = image_generator.fetch_photo("Baalbek trilithon stones megaliths", None)
        self.assertEqual(calls, ["Baalbek trilithon stones megaliths", "Baalbek trilithon"])
        self.assertEqual(data, b"jpeg")


class MoreSourcesTests(unittest.TestCase):
    def test_commons_file_and_article_picture_are_licence_checked(self):
        page = wiki_page(1, "File:Baalbek.jpg", "CC BY-SA 3.0")
        with mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": {"1": page}}})):
            found = stock.wikipedia_lead_image("https://upload.wikimedia.org/wikipedia/commons/a/ab/Baalbek%20stones.jpg")
        self.assertEqual((found["source"], found["attribution_required"]), ("wikimedia", True))
        bad = wiki_page(1, "x", "Fair use")
        with mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": {"1": bad}}})):
            with self.assertRaises(stock.StockError):
                stock.commons_file("x.jpg")
        with self.assertRaises(stock.StockError):
            stock.wikipedia_lead_image("https://upload.wikimedia.org/wikipedia/en/x/xx/Local.jpg")   # not on Commons

    def test_nasa_unsplash_flickr(self):
        def nasa(url, params=None, headers=None, timeout=None):
            if "search" in url:
                return FakeResp({"collection": {"items": [{"data": [{"nasa_id": "PIA1", "center": "JPL", "title": "Mars"}],
                                                           "links": [{"href": "http://images/PIA1~thumb.jpg"}]}]}})
            return FakeResp({"collection": {"items": [{"href": "http://images/PIA1~orig.jpg"}, {"href": "http://images/PIA1~large.jpg"}]}})
        with mock.patch.object(stock.requests, "get", side_effect=nasa):
            found = stock.search_nasa("mars")
        self.assertEqual((found["url"], found["source"], found["attribution_required"]), ("https://images/PIA1~large.jpg", "nasa", False))

        with mock.patch.dict(os.environ, {"UNSPLASH_ACCESS_KEY": "u"}), \
             mock.patch.object(stock.requests, "get", return_value=FakeResp({"results": [
                 {"id": "a1", "urls": {"regular": "https://unsplash/a.jpg"}, "user": {"name": "Maria"},
                  "links": {"html": "https://unsplash.com/photos/a1", "download_location": "https://api.unsplash.com/photos/a1/download"}}]})) as get:
            found = stock.search_unsplash("stones")
        self.assertEqual(found["credit"], "Photo: Maria on Unsplash")
        self.assertTrue(found["attribution_required"])
        self.assertEqual(get.call_count, 2)                                # search + the download trigger the API terms require

        with mock.patch.dict(os.environ, {"FLICKR_API_KEY": "f"}), \
             mock.patch.object(stock.requests, "get", return_value=FakeResp({"stat": "ok", "photos": {"photo": [
                 {"id": "1", "owner": "o", "ownername": "Nikos", "license": "0", "url_l": "https://f/1.jpg"},
                 {"id": "2", "owner": "o", "ownername": "Nikos", "license": "9", "url_l": "https://f/2.jpg"}]}})) as get:
            found = stock.search_flickr("Baalbek")
        self.assertEqual((found["url"], found["attribution_required"]), ("https://f/2.jpg", False))
        self.assertIn("CC0", found["credit"])
        self.assertIn("4,5,7,9,10", get.call_args.kwargs["params"]["license"])

    def test_find_photo_skips_sources_without_keys(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(url)
            if "wikimedia" in url:
                return FakeResp({"query": {"pages": {}}})
            return FakeResp({"collection": {"items": []}})
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "", "PIXABAY_API_KEY": "", "UNSPLASH_ACCESS_KEY": "", "FLICKR_API_KEY": ""}), \
             mock.patch.object(stock.requests, "get", side_effect=fake_get):
            with self.assertRaises(stock.StockError) as cm:
                stock.find_photo("x")
        self.assertTrue(any("wikimedia" in u for u in calls) and any("nasa" in u for u in calls))
        self.assertNotIn("unsplash", str(cm.exception))
        self.assertNotIn("flickr", str(cm.exception))


class ArticlePictureFirstTests(PhotoPostBase):
    def test_the_articles_own_picture_is_used_before_searching(self):
        pid = self._post()
        with get_connection() as conn:
            conn.execute("UPDATE posts SET host_ref = 'wiki-image:https://upload.wikimedia.org/wikipedia/commons/a/ab/Baalbek.jpg' WHERE id = ?", (pid,))
        found = {"url": "https://upload/lead.jpg", "credit": "Photo: Nikos (CC BY 4.0) via Wikimedia Commons", "link": "https://commons/x",
                 "license": "CC BY 4.0", "attribution_required": True, "source": "wikimedia"}
        with mock.patch.object(stock, "wikipedia_lead_image", return_value=found) as lead, \
             mock.patch.object(stock, "find_photo") as search, \
             mock.patch.object(providers.requests, "get", return_value=FakeResp(content=self.jpeg)), \
             mock.patch("src.overlay.render_title"):
            self.assertTrue(image_generator.process_post(queries.get_post(pid)))
        lead.assert_called_once()
        search.assert_not_called()
        self.assertIn("CC BY 4.0", queries.get_post(pid)["media_credit"])


class PlaceMatchingTests(unittest.TestCase):
    def test_commons_search_can_demand_that_the_file_names_the_place(self):
        pages = {"1": wiki_page(1, "File:Great Pyramid of Giza.jpg", "CC BY-SA 4.0"),
                 "2": wiki_page(2, "File:Piramide del Sol, Teotihuacan.jpg", "CC BY-SA 4.0")}
        pages["1"]["imageinfo"][0]["extmetadata"]["ImageDescription"] = {"value": "The pyramid at Giza, Egypt"}
        pages["2"]["imageinfo"][0]["extmetadata"]["ImageDescription"] = {"value": "Pyramid of the Sun seen from the Avenue of the Dead"}
        with mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": pages}})):
            self.assertEqual(stock.search_wikimedia("pyramid")["url"], "https://upload/1.jpg")          # no check: first hit
            found = stock.search_wikimedia("Teotihuacan Pyramid of the Sun", must_mention="Teotihuacan Pyramid of the Sun")
        self.assertEqual(found["url"], "https://upload/2.jpg")
        with mock.patch.object(stock.requests, "get", return_value=FakeResp({"query": {"pages": {"1": pages["1"]}}})):
            with self.assertRaises(stock.StockError):
                stock.search_wikimedia("Teotihuacan Pyramid of the Sun", must_mention="Teotihuacan Pyramid of the Sun")
        self.assertTrue(stock._mentions("Dragon Houses of Euboea", "Drakospito Ochi, Euboea"))
        self.assertFalse(stock._mentions("Pyramid of the Sun", "Great pyramid, Giza"))         # generic words do not count

    def test_page_planner_never_asks_the_stock_libraries(self):
        from src import ai_plan, image_generator
        with mock.patch.object(image_generator, "fetch_lead_image", side_effect=image_generator.ImageGenError("no")), \
             mock.patch.object(stock, "search_wikimedia", side_effect=stock.StockError("none")) as commons, \
             mock.patch.object(stock, "find_photo") as libraries:
            self.assertIsNone(ai_plan._secure_photo("Teotihuacan Pyramid of the Sun", "https://upload.wikimedia.org/wikipedia/commons/x.jpg", 1))
        libraries.assert_not_called()
        self.assertEqual(commons.call_args.kwargs["must_mention"], "Teotihuacan Pyramid of the Sun")
