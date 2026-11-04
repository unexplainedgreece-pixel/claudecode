"""Meta error handling: plain-language hints, transient flags, container polling
that survives a glitchy status check, and media_publish retries."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["SCHEDULER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CSRF_DISABLED"] = "1"

from src import publisher   # noqa: E402


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, ""

    def json(self):
        return self._p


def err(code, subcode=None, message="boom", user_msg=None):
    e = {"code": code, "message": message}
    if subcode is not None:
        e["error_subcode"] = subcode
    if user_msg:
        e["error_user_msg"] = user_msg
    return FakeResp({"error": e}, 400)


class MetaErrorTests(unittest.TestCase):
    def test_hint_code_and_transient_flag(self):
        exc = publisher.meta_error({"error": {"code": 190, "message": "Error validating access token"}})
        self.assertEqual((exc.code, exc.subcode, exc.transient), (190, None, False))
        self.assertIn("Reconnect the Page", str(exc))
        self.assertIn("Meta API error 190:", str(exc))

    def test_subcode_hint_beats_code_hint_and_user_message_is_preferred(self):
        exc = publisher.meta_error({"error": {"code": 9007, "error_subcode": 2207052, "message": "x",
                                              "error_user_msg": "Media download failed"}})
        self.assertIn("9007/2207052: Media download failed", str(exc))
        self.assertIn("link must be public", str(exc))
        self.assertFalse(exc.transient)
        self.assertTrue(publisher.meta_error({"error": {"code": 100, "error_subcode": 33, "message": "Authorization Error"}}).transient)
        self.assertTrue(publisher.meta_error({"error": {"code": 4, "message": "limit"}}).transient)
        self.assertIn("Threads API error 1:", str(publisher.meta_error({"error": {"code": 1}}, platform="Threads")))

    def test_meta_request_raises_rich_error(self):
        with mock.patch.object(publisher.requests, "request", return_value=err(2207057, message="daily limit")):
            with self.assertRaises(publisher.PublishError) as cm:
                publisher.meta_request("POST", "https://graph/x")
        self.assertEqual(cm.exception.code, 2207057)
        self.assertIn("resets after 24 hours", str(cm.exception))


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(publisher.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_poll_survives_two_glitched_status_checks(self):
        answers = [err(100, 33, "Authorization Error"), err(100, 33, "Authorization Error"),
                   FakeResp({"status_code": "IN_PROGRESS"}), FakeResp({"status_code": "FINISHED"})]
        with mock.patch.object(publisher.requests, "request", side_effect=answers) as req:
            publisher._poll_container("c1", "tok", tries=10)
        self.assertEqual(req.call_count, 4)

    def test_poll_gives_up_after_three_consecutive_failures(self):
        answers = [err(100, 33)] * 3 + [FakeResp({"status_code": "FINISHED"})]
        with mock.patch.object(publisher.requests, "request", side_effect=answers):
            with self.assertRaises(publisher.PublishError):
                publisher._poll_container("c1", "tok", tries=10)

    def test_poll_does_not_mask_a_real_error(self):
        with mock.patch.object(publisher.requests, "request", return_value=err(190, message="token expired")):
            with self.assertRaises(publisher.PublishError) as cm:
                publisher._poll_container("c1", "tok", tries=10)
        self.assertEqual(cm.exception.code, 190)

    def test_poll_statuses(self):
        for status, transient in (("ERROR", False), ("EXPIRED", True)):
            with mock.patch.object(publisher.requests, "request", return_value=FakeResp({"status_code": status, "status": "bad"})):
                with self.assertRaises(publisher.PublishError) as cm:
                    publisher._poll_container("c1", "tok", tries=3)
            self.assertEqual(cm.exception.transient, transient, status)
        with mock.patch.object(publisher.requests, "request", return_value=FakeResp({"status_code": "IN_PROGRESS"})):
            with self.assertRaises(publisher.PublishError) as cm:
                publisher._poll_container("c1", "tok", tries=2)
        self.assertTrue(cm.exception.transient)
        self.assertIn("timed out", str(cm.exception))
        with mock.patch.object(publisher.requests, "request", return_value=FakeResp({"status_code": "PUBLISHED"})):
            publisher._poll_container("c1", "tok", tries=1)      # already published counts as done

    def test_media_publish_retries_not_ready_then_succeeds(self):
        answers = [err(9007, 2207027, "The media is not ready for publishing"), FakeResp({"id": "ig9"})]
        with mock.patch.object(publisher.requests, "request", side_effect=answers) as req:
            self.assertEqual(publisher._media_publish("ig", "c1", "tok")["id"], "ig9")
        self.assertEqual(req.call_count, 2)

    def test_media_publish_does_not_retry_permanent_errors(self):
        with mock.patch.object(publisher.requests, "request", return_value=err(352, message="bad format")) as req:
            with self.assertRaises(publisher.PublishError) as cm:
                publisher._media_publish("ig", "c1", "tok")
        self.assertEqual(req.call_count, 1)
        self.assertIn("MP4", str(cm.exception))

    def test_media_publish_gives_up_after_retries(self):
        with mock.patch.object(publisher.requests, "request", return_value=err(24, message="temporary")) as req:
            with self.assertRaises(publisher.PublishError):
                publisher._media_publish("ig", "c1", "tok")
        self.assertEqual(req.call_count, publisher.IG_PUBLISH_RETRIES)


if __name__ == "__main__":
    unittest.main()


class CaptionAndReelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from config.db import init_db
        init_db()

    @staticmethod
    def _post(**over):
        base = {"id": 1, "user_id": 1, "caption": "Hello", "hashtags": "#a #b", "platforms": "facebook,instagram,threads",
                "image_url": None, "video_url": None, "video_path": None, "jpeg_path": None, "image_path": None,
                "media_type": "video", "page_id": None, "fb_post_id": None}
        base.update(over)
        return base

    def test_caption_issues_counts_hashtags_mentions_and_length(self):
        tags = " ".join(f"#t{i}" for i in range(31))
        p = self._post(hashtags=tags)
        issues = publisher.caption_issues(p)
        self.assertTrue(any("Instagram: 31 hashtags" in i for i in issues), issues)
        self.assertFalse(any(i.startswith("Facebook") for i in issues))
        p = self._post(caption="x" * 600, hashtags="")
        issues = publisher.caption_issues(p, ["threads", "instagram"])
        self.assertEqual(len(issues), 1)
        self.assertIn("Threads: caption is 600 characters", issues[0])
        p = self._post(caption=" ".join(f"@u{i}" for i in range(21)), hashtags="")
        self.assertTrue(any("21 @mentions" in i for i in publisher.caption_issues(p, ["instagram"])))
        self.assertEqual(publisher.caption_issues(self._post()), [])
        with mock.patch.object(publisher, "hashtags_in_comment", return_value=True):
            self.assertTrue(publisher.caption_issues(self._post(hashtags=tags), ["instagram"]))   # comment has the same cap

    def test_instagram_refuses_bad_caption_before_any_api_call(self):
        p = self._post(image_url="https://i/x.jpg", hashtags=" ".join(f"#t{i}" for i in range(31)))
        with mock.patch.object(publisher.requests, "request") as req, \
             mock.patch.object(publisher, "meta_credentials", return_value={"token": "t", "ig_id": "9", "page_id": "1"}):
            with self.assertRaises(publisher.PublishError) as cm:
                publisher.publish_instagram(p)
        self.assertIn("31 hashtags", str(cm.exception))
        req.assert_not_called()

    def _run_facebook(self, dims, calls):
        def fake(method, url, **kw):
            calls.append((method, url, kw.get("data") if not hasattr(kw.get("data"), "read") else "<bytes>", kw.get("headers") or {}))
            if url.endswith("/video_reels") and kw["data"]["upload_phase"] == "start":
                return FakeResp({"video_id": "v77", "upload_url": "https://rupload/x"})
            if "rupload.facebook.com" in url:
                return FakeResp({"success": True})
            if url.endswith("/video_reels"):
                return FakeResp({"success": True, "post_id": "ignored"})
            if url.endswith("/v77") and method == "GET":
                return FakeResp({"status": {"video_status": "ready", "publishing_phase": {"status": "complete"}}})
            return FakeResp({"id": "vid1"})
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(b"0" * 2048)
            path = fh.name
        try:
            p = self._post(video_path=path)
            with mock.patch.object(publisher.requests, "request", side_effect=fake), \
                 mock.patch.object(publisher.time, "sleep", lambda s: None), \
                 mock.patch("src.library.probe_video", return_value=dims), \
                 mock.patch.object(publisher, "meta_credentials", return_value={"token": "tok", "ig_id": None, "page_id": "P1"}):
                return publisher.publish_facebook(p)
        finally:
            os.unlink(path)

    def test_vertical_video_becomes_a_facebook_reel(self):
        calls = []
        self.assertEqual(self._run_facebook({"width": 1080, "height": 1920}, calls), "v77")
        urls = [c[1] for c in calls]
        self.assertTrue(urls[0].endswith("/P1/video_reels") and calls[0][2]["upload_phase"] == "start")
        self.assertIn("rupload.facebook.com/video-upload/", urls[1])
        self.assertEqual(calls[1][3]["file_size"], "2048")
        self.assertEqual(calls[1][3]["Authorization"], "OAuth tok")
        self.assertEqual(calls[2][2]["video_state"], "PUBLISHED")
        self.assertEqual(calls[2][2]["description"], "Hello\n\n#a #b")
        self.assertEqual(calls[3][0], "GET")
        self.assertFalse(any(u.endswith("/videos") for u in urls))

    def test_horizontal_video_stays_a_feed_video(self):
        calls = []
        self.assertEqual(self._run_facebook({"width": 1920, "height": 1080}, calls), "vid1")
        self.assertTrue(calls[0][1].endswith("/P1/videos"))
        self.assertEqual(len(calls), 1)

    def test_unknown_dimensions_stay_a_feed_video(self):
        calls = []
        self.assertEqual(self._run_facebook({}, calls), "vid1")
        self.assertTrue(calls[0][1].endswith("/P1/videos"))


class AltTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from config.db import init_db
        init_db()

    def test_alt_text_is_title_plus_first_sentence_without_hashtags(self):
        p = {"topic": "Sunset at Mandraki", "caption": "Golden hour over the harbour #rhodes. Come early!\nSecond line"}
        self.assertEqual(publisher.alt_text(p), "Sunset at Mandraki. Golden hour over the harbour.")
        self.assertEqual(publisher.alt_text({"topic": "", "caption": ""}), "")
        self.assertEqual(publisher.alt_text({"topic": "Octopus", "caption": "Octopus grilled today."}), "Octopus grilled today.")
        self.assertEqual(len(publisher.alt_text({"topic": "x" * 900, "caption": ""})), 500)

    def test_alt_text_is_sent_for_images_not_reels(self):
        calls = []

        def fake(method, url, **kw):
            calls.append((url, kw.get("data") or {}))
            if url.endswith("/media"):
                return FakeResp({"id": "c"})
            if method == "GET":
                return FakeResp({"status_code": "FINISHED"})
            return FakeResp({"id": "ig1", "post_id": "fb1"})
        base = {"id": 1, "user_id": 1, "topic": "Sunset", "caption": "Harbour view.", "hashtags": "", "platforms": "instagram",
                "video_url": None, "video_path": None, "jpeg_path": None, "image_path": None, "media_type": "image", "page_id": None}
        creds = {"token": "t", "ig_id": "9", "page_id": "P"}
        with mock.patch.object(publisher.requests, "request", side_effect=fake), \
             mock.patch.object(publisher.time, "sleep", lambda s: None), \
             mock.patch.object(publisher, "meta_credentials", return_value=creds):
            publisher.publish_instagram({**base, "image_url": "https://i/x.jpg"})
            publisher.publish_facebook({**base, "image_url": "https://i/x.jpg"})
            publisher.publish_instagram({**base, "image_url": None, "video_url": "https://v/x.mp4"})
        ig_image = next(c for c in calls if c[0].endswith("/9/media") and "image_url" in c[1])
        self.assertEqual(ig_image[1]["alt_text"], "Sunset. Harbour view.")
        fb_photo = next(c for c in calls if c[0].endswith("/P/photos"))
        self.assertEqual(fb_photo[1]["alt_text_custom"], "Sunset. Harbour view.")
        reel = next(c for c in calls if c[0].endswith("/9/media") and "video_url" in c[1])
        self.assertNotIn("alt_text", reel[1])


class AiLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from config.db import init_db
        init_db()

    def test_ai_images_get_the_instagram_ai_label_and_own_photos_do_not(self):
        calls = []

        def fake(method, url, **kw):
            calls.append((url, kw.get("data") or {}))
            if url.endswith("/media"):
                return FakeResp({"id": "c"})
            if method == "GET":
                return FakeResp({"status_code": "FINISHED"})
            return FakeResp({"id": "ig1"})
        base = {"id": 1, "user_id": 1, "topic": "Sunset", "caption": "Harbour.", "hashtags": "", "platforms": "instagram",
                "video_url": None, "video_path": None, "jpeg_path": None, "image_path": None, "media_type": "image",
                "page_id": None, "image_url": "https://i/x.jpg"}
        with mock.patch.object(publisher.requests, "request", side_effect=fake), \
             mock.patch.object(publisher.time, "sleep", lambda s: None), \
             mock.patch.object(publisher, "meta_credentials", return_value={"token": "t", "ig_id": "9", "page_id": "P"}):
            publisher.publish_instagram({**base, "media_credit": "AI image (gemini)"})
            publisher.publish_instagram({**base, "media_credit": "Photo by Nikos on Pexels (https://p)"})
            publisher.publish_instagram({**base, "media_credit": None})
        containers = [c[1] for c in calls if c[0].endswith("/9/media")]
        self.assertEqual(containers[0].get("is_ai_generated"), "true")
        self.assertNotIn("is_ai_generated", containers[1])
        self.assertNotIn("is_ai_generated", containers[2])
        self.assertTrue(publisher.is_ai_media({"media_credit": "AI image (cloudflare)"}))
        self.assertFalse(publisher.is_ai_media({"topic": "no credit column"}))


class OddPayloadTests(unittest.TestCase):
    def test_non_dict_error_payloads_become_publish_errors(self):
        for payload in ({"error": "invalid_token"}, ["x"], None, {"error": None}):
            exc = publisher.meta_error(payload)
            self.assertIsInstance(exc, publisher.PublishError)
            self.assertFalse(exc.transient)
        self.assertIn("invalid_token", str(publisher.meta_error({"error": "invalid_token"})))
        with mock.patch.object(publisher.requests, "request", return_value=FakeResp(["nope"], 500)):
            with self.assertRaises(publisher.PublishError):
                publisher.meta_request("GET", "https://graph/x")
        with mock.patch.object(publisher.requests, "request", return_value=FakeResp(None, 200)):
            with self.assertRaises(publisher.PublishError):
                publisher.meta_request("GET", "https://graph/x")

    def test_reel_processing_error_with_odd_fields_is_a_clean_error(self):
        answers = [FakeResp({"video_id": "v1", "upload_url": "u"}), FakeResp({"success": True}), FakeResp({"success": True}),
                   FakeResp({"status": {"video_status": "error", "processing_phase": {"status": "error", "error": "bad codec"},
                                        "uploading_phase": None}})]
        post = {"video_url": "https://v/x.mp4", "id": 1}
        with mock.patch.object(publisher.requests, "request", side_effect=answers), \
             mock.patch.object(publisher.time, "sleep", lambda s: None):
            with self.assertRaises(publisher.PublishError) as cm:
                publisher.publish_facebook_reel(post, "P", "tok", "hi")
        self.assertIn("bad codec", str(cm.exception))
        # a string status body: treated as "no more information", reel counts as published
        answers = [FakeResp({"video_id": "v1", "upload_url": "u"}), FakeResp({"success": True}), FakeResp({"success": True}),
                   FakeResp({"status": "weird"})]
        with mock.patch.object(publisher.requests, "request", side_effect=answers), \
             mock.patch.object(publisher.time, "sleep", lambda s: None):
            self.assertEqual(publisher.publish_facebook_reel(post, "P", "tok", "hi"), "v1")


class BackoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from config.db import init_db
        init_db()

    def setUp(self):
        from config.db import get_connection
        from src.web import auth, queries
        with get_connection() as conn:
            for t in ("posts", "users"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"JWT_SECRET": "b", "META_ACCESS_TOKEN": "t", "META_PAGE_ID": "1",
                                                "META_INSTAGRAM_ACCOUNT_ID": "2"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.uid = auth.create_user("b@example.com", "password123", "B")["id"]
        self.pid = queries.create_draft("x", "Hello", "", user_id=self.uid)
        queries.set_status(self.pid, "approved", platforms=["facebook"])
        with get_connection() as conn:
            conn.execute("UPDATE posts SET image_url = 'https://i/x.jpg' WHERE id = ?", (self.pid,))

    def _run(self, resp):
        from src.web import queries
        with mock.patch.object(publisher.requests, "request", return_value=resp), \
             mock.patch.object(publisher.time, "sleep", lambda s: None), \
             mock.patch("src.notify.send", return_value=False):
            publisher._publish_claimed(queries.get_post(self.pid), ["facebook"], dry_run=False)
        return queries.get_post(self.pid)

    def test_failures_back_off_and_the_card_says_when(self):
        from datetime import datetime, timedelta
        post = self._run(err(1, message="temporary"))
        self.assertEqual(post["publish_attempts"], 1)
        self.assertIn("next automatic try after", post["last_error"])
        wait = datetime.strptime(post["retry_after"], "%Y-%m-%d %H:%M:%S") - datetime.now()
        self.assertTrue(timedelta(minutes=9) < wait <= timedelta(minutes=10))
        # while waiting, the worker leaves it alone (no API call, attempts unchanged)
        with mock.patch.object(publisher.requests, "request") as req:
            publisher._publish_claimed(post, ["facebook"], dry_run=False)
        req.assert_not_called()
        self.assertEqual(self._run_after_wait(err(1)), 2)

    def _run_after_wait(self, resp):
        from config.db import get_connection
        with get_connection() as conn:
            conn.execute("UPDATE posts SET retry_after = '2000-01-01 00:00:00' WHERE id = ?", (self.pid,))
        return self._run(resp)["publish_attempts"]

    def test_spam_block_waits_hours_and_explains(self):
        from datetime import datetime, timedelta
        post = self._run(err(368, 1390008, "We limit how often you can post"))
        self.assertIn("spam protection", post["last_error"])
        wait = datetime.strptime(post["retry_after"], "%Y-%m-%d %H:%M:%S") - datetime.now()
        self.assertTrue(timedelta(hours=5, minutes=59) < wait <= timedelta(hours=6))

    def test_approving_or_clearing_resets_the_wait(self):
        from src.web import queries
        self._run(err(1))
        queries.clear_error(self.pid)
        self.assertIsNone(queries.get_post(self.pid)["retry_after"])
        self._run(err(1))
        queries.set_status(self.pid, "approved")
        self.assertIsNone(queries.get_post(self.pid)["retry_after"])
