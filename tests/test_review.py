"""Client review links: a client approves or asks for changes from a private link."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ["SCHEDULER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["CSRF_DISABLED"] = "1"

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src import review                                  # noqa: E402
from src.security import crypto                         # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

KEY = crypto.generate_key() if hasattr(crypto, "generate_key") else "x" * 44


class ReviewTests(unittest.TestCase):
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
            for t in ("posts", "users", "pages", "review_links"):
                conn.execute(f"DELETE FROM {t}")
        self.env = mock.patch.dict(os.environ, {"JWT_SECRET": "rev"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.owner = auth.create_user("agency@example.com", "password123", "Apex Digital")
        self.other = auth.create_user("other@example.com", "password123", "Other")
        self.uid = self.owner["id"]
        with get_connection() as conn:
            self.page_a = conn.execute("INSERT INTO pages (user_id, name, fb_page_id, token_encrypted) VALUES (?, 'Taverna Nikos', '1', 'x')",
                                       (self.uid,)).lastrowid
            self.page_b = conn.execute("INSERT INTO pages (user_id, name, fb_page_id, token_encrypted) VALUES (?, 'Hotel Blue', '2', 'x')",
                                       (self.uid,)).lastrowid
        self.p_a = queries.create_draft("Grilled octopus", "Fresh today", "#rhodes", user_id=self.uid)
        self.p_b = queries.create_draft("Pool view", "Sunset", "", user_id=self.uid)
        self.p_none = queries.create_draft("General", "Hi", "", user_id=self.uid)
        self.p_other = queries.create_draft("Not yours", "x", "", user_id=self.other["id"])
        with get_connection() as conn:
            conn.execute("UPDATE posts SET page_id = ? WHERE id = ?", (self.page_a, self.p_a))
            conn.execute("UPDATE posts SET page_id = ? WHERE id = ?", (self.page_b, self.p_b))
        self.client.cookies.clear()

    def login(self):
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.uid))

    def test_page_scoped_link_shows_only_that_pages_posts_and_unassigned_ones(self):
        link = review.create_link(self.uid, "Nikos", self.page_a)
        ids = [p["id"] for p in review.pending_posts(link)]
        self.assertEqual(sorted(ids), sorted([self.p_a, self.p_none]))
        all_link = review.create_link(self.uid, "All")
        self.assertEqual(sorted(p["id"] for p in review.pending_posts(all_link)), sorted([self.p_a, self.p_b, self.p_none]))
        with self.assertRaises(review.ReviewError):
            review.create_link(self.other["id"], "steal", self.page_a)          # not their Page

    def test_public_page_and_decisions_without_login(self):
        link = review.create_link(self.uid, "Nikos", self.page_a)
        r = self.client.get(f"/review/{link['token']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Grilled octopus", r.text)
        self.assertIn("Fresh today", r.text)
        self.assertNotIn("Pool view", r.text)
        self.assertNotIn("Not yours", r.text)
        self.assertIn("noindex", r.text)
        with mock.patch("src.notify.send") as sent:
            r = self.client.post(f"/review/{link['token']}/{self.p_a}", data={"decision": "approve"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("msg=", r.headers["location"])
        post = queries.get_post(self.p_a)
        self.assertEqual((post["status"], post["client_decision"]), ("approved", "approved"))
        self.assertTrue(post["approved_at"])
        sent.assert_called_once()
        self.assertIn("Nikos approved", sent.call_args.args[1])
        # ask for changes keeps it waiting, with the note
        r = self.client.post(f"/review/{link['token']}/{self.p_none}", data={"decision": "changes", "note": ""}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])                           # note required
        with mock.patch("src.notify.send"):
            r = self.client.post(f"/review/{link['token']}/{self.p_none}", data={"decision": "changes", "note": "Use the drone shot"},
                                 follow_redirects=False)
        post = queries.get_post(self.p_none)
        self.assertEqual((post["status"], post["client_decision"], post["client_note"]), ("draft", "changes", "Use the drone shot"))
        r = self.client.get(f"/review/{link['token']}")
        self.assertIn("Your earlier note: Use the drone shot", r.text)
        self.assertIn("Your recent decisions", r.text)
        # already handled -> error, and the other user's post is never reachable
        r = self.client.post(f"/review/{link['token']}/{self.p_a}", data={"decision": "approve"}, follow_redirects=False)
        self.assertIn("already", r.headers["location"])
        r = self.client.post(f"/review/{link['token']}/{self.p_other}", data={"decision": "approve"}, follow_redirects=False)
        self.assertIn("not%20available", r.headers["location"])
        self.assertEqual(queries.get_post(self.p_other)["status"], "draft")
        r = self.client.post(f"/review/{link['token']}/{self.p_b}", data={"decision": "approve"}, follow_redirects=False)
        self.assertEqual(queries.get_post(self.p_b)["status"], "draft")   # other Page, not on this link

    def test_owner_card_shows_the_client_note_and_decision(self):
        link = review.create_link(self.uid, "Nikos")
        with mock.patch("src.notify.send"):
            review.decide(link, self.p_a, "changes", "Less text please")
            review.decide(link, self.p_b, "approve")
        self.login()
        html = self.client.get("/queue").text
        self.assertIn("Client asked for changes", html)
        self.assertIn("Less text please", html)
        self.assertIn("Approved by the client", html)           # approved posts are on the same page

    def test_revoked_expired_and_unknown_links_are_404(self):
        link = review.create_link(self.uid, "Nikos")
        self.assertEqual(self.client.get(f"/review/{link['token']}").status_code, 200)
        review.revoke(link["id"], self.uid)
        self.assertEqual(self.client.get(f"/review/{link['token']}").status_code, 404)
        self.assertEqual(self.client.post(f"/review/{link['token']}/{self.p_a}", data={"decision": "approve"}).status_code, 404)
        expired = review.create_link(self.uid, "Old", days=3)
        with get_connection() as conn:
            conn.execute("UPDATE review_links SET expires_at = ? WHERE id = ?",
                         ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), expired["id"]))
        self.assertEqual(self.client.get(f"/review/{expired['token']}").status_code, 404)
        self.assertEqual(self.client.get("/review/nope").status_code, 404)
        self.assertFalse(review.revoke(link["id"], self.other["id"]))          # not theirs

    def test_media_is_served_only_for_covered_posts(self):
        link = review.create_link(self.uid, "Nikos", self.page_a)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            fh.write(b"\xff\xd8\xff\xd9")
            path = fh.name
        self.addCleanup(os.unlink, path)
        with get_connection() as conn:
            conn.execute("UPDATE posts SET jpeg_path = ? WHERE id IN (?, ?)", (path, self.p_a, self.p_b))
        r = self.client.get(f"/review/{link['token']}/files/{self.p_a}/image")
        self.assertEqual((r.status_code, r.content), (200, b"\xff\xd8\xff\xd9"))
        self.assertEqual(self.client.get(f"/review/{link['token']}/files/{self.p_b}/image").status_code, 404)
        self.assertEqual(self.client.get(f"/review/{link['token']}/files/{self.p_a}/audio").status_code, 404)
        html = self.client.get(f"/review/{link['token']}").text
        self.assertIn(f"/review/{link['token']}/files/{self.p_a}/image", html)

    def test_settings_page_lists_links_and_owner_can_create_and_revoke(self):
        self.login()
        r = self.client.post("/settings/review-links", data={"label": "Nikos", "page_id": str(self.page_a), "days": "30"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("msg=", r.headers["location"])
        links = review.list_links(self.uid)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["page_name"], "Taverna Nikos")
        self.assertTrue(links[0]["expires_at"])
        html = self.client.get("/settings").text
        self.assertIn(f"/review/{links[0]['token']}", html)
        self.assertIn("Client review links", html)
        r = self.client.post(f"/settings/review-links/{links[0]['id']}/revoke", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(review.list_links(self.uid)[0]["revoked"], 1)
        # someone else's link id -> 404
        other_link = review.create_link(self.other["id"], "x")
        self.assertEqual(self.client.post(f"/settings/review-links/{other_link['id']}/revoke", follow_redirects=False).status_code, 404)

    def test_review_posts_work_with_csrf_enabled_and_a_session_cookie(self):
        """The owner opening their own link in a logged-in browser must not be blocked by CSRF."""
        link = review.create_link(self.uid, "Nikos")
        self.login()
        with mock.patch.dict(os.environ, {"CSRF_DISABLED": "0"}), mock.patch("src.notify.send"):
            r = self.client.post(f"/review/{link['token']}/{self.p_a}", data={"decision": "approve"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(queries.get_post(self.p_a)["status"], "approved")


if __name__ == "__main__":
    unittest.main()
