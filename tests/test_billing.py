"""Billing: tier limits, usage, Stripe webhooks (signature mocked), checkout, operator CLI."""

import os
import tempfile
import unittest
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name

from fastapi.testclient import TestClient              # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src.billing import limits, stripe_integration as billing, tiers   # noqa: E402
from src.web import app as webapp, auth, queries        # noqa: E402

STRIPE_ENV = {"STRIPE_SECRET_KEY": "sk_test_123", "STRIPE_WEBHOOK_SECRET": "whsec_123",
              "STRIPE_PRICE_PRO": "price_pro", "STRIPE_PRICE_ENTERPRISE": "price_ent"}


def set_tier(user_id, tier):
    with get_connection() as conn:
        conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user_id))


class LimitTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            for t in ("posts", "users", "billing_events"):
                conn.execute(f"DELETE FROM {t}")
        self.user = auth.create_user("lim@example.com", "password123")

    def make_posts(self, n, **kw):
        for _ in range(n):
            queries.create_draft("x", user_id=self.user["id"], **kw)

    def test_tier_table(self):
        self.assertEqual(tiers.tier_info("free")["posts_per_month"], 5)
        self.assertEqual(tiers.tier_info("pro")["posts_per_month"], 50)
        self.assertIsNone(tiers.tier_info("enterprise")["posts_per_month"])
        self.assertEqual(tiers.tier_info(None)["label"], "Free")
        self.assertEqual(tiers.tier_info("bogus")["label"], "Free")

    def test_free_limits(self):
        user = auth.get_user(self.user["id"])
        self.make_posts(4)
        limits.enforce(user, "create_post")
        self.make_posts(1)
        with self.assertRaises(limits.LimitError) as cm:
            limits.enforce(user, "create_post")
        self.assertEqual(cm.exception.code, "posts_limit")
        self.assertIn("5 of 5", str(cm.exception))
        with self.assertRaises(limits.LimitError):
            limits.enforce(user, "upload")
        with self.assertRaises(limits.LimitError):
            limits.enforce(user, "reel")
        self.assertEqual(limits.remaining_posts(user), 0)

    def test_pro_allows_uploads_not_reels(self):
        set_tier(self.user["id"], "pro")
        user = auth.get_user(self.user["id"])
        limits.enforce(user, "upload")
        with self.assertRaises(limits.LimitError) as cm:
            limits.enforce(user, "reel")
        self.assertEqual(cm.exception.code, "no_reels")
        self.make_posts(50)
        with self.assertRaises(limits.LimitError) as cm:
            limits.enforce(user, "upload")
        self.assertEqual(cm.exception.code, "posts_limit")

    def test_enterprise_is_unlimited(self):
        set_tier(self.user["id"], "enterprise")
        user = auth.get_user(self.user["id"])
        self.make_posts(120)
        for action in ("create_post", "upload", "reel"):
            limits.enforce(user, action)
        self.assertIsNone(limits.remaining_posts(user))

    def test_usage_counts_this_month_only_and_by_kind(self):
        self.make_posts(2)
        self.make_posts(1, source="upload", media_type="video")
        self.make_posts(1, media_type="video")
        with get_connection() as conn:   # one old post from last year
            conn.execute("INSERT INTO posts (topic, status, user_id, created_at) VALUES ('old', 'published', ?, '2025-01-01 10:00:00')",
                         (self.user["id"],))
        other = auth.create_user("other@example.com", "password123")
        queries.create_draft("theirs", user_id=other["id"])
        u = limits.usage(self.user["id"])
        self.assertEqual((u["posts"], u["uploads"], u["reels"]), (4, 1, 1))


class StripeTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            for t in ("posts", "users", "billing_events"):
                conn.execute(f"DELETE FROM {t}")
        self.user = auth.create_user("pay@example.com", "password123")
        self.env = mock.patch.dict(os.environ, STRIPE_ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_not_configured(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "your_stripe_key_here"}):
            self.assertFalse(billing.is_configured())
            with self.assertRaisesRegex(billing.BillingError, "not configured"):
                billing.create_checkout_session(self.user, "pro", "http://s", "http://c")

    def test_checkout_session_passes_user_and_price(self):
        session = mock.MagicMock(url="https://checkout.stripe.com/abc")
        with mock.patch("stripe.checkout.Session.create", return_value=session) as create:
            url = billing.create_checkout_session(self.user, "enterprise", "http://ok", "http://cancel")
        self.assertEqual(url, "https://checkout.stripe.com/abc")
        kw = create.call_args.kwargs
        self.assertEqual(kw["mode"], "subscription")
        self.assertEqual(kw["line_items"][0]["price"], "price_ent")
        self.assertEqual(kw["metadata"], {"user_id": str(self.user["id"]), "tier": "enterprise"})
        self.assertEqual(kw["customer_email"], "pay@example.com")
        with self.assertRaises(billing.BillingError):
            billing.create_checkout_session(self.user, "free", "http://ok", "http://cancel")

    def test_portal_needs_customer(self):
        with self.assertRaisesRegex(billing.BillingError, "Subscribe"):
            billing.create_portal_session(self.user, "http://back")
        billing.set_tier(self.user["id"], "pro", customer_id="cus_1")
        with mock.patch("stripe.billing_portal.Session.create", return_value=mock.MagicMock(url="https://portal")):
            self.assertEqual(billing.create_portal_session(auth.get_user(self.user["id"]), "http://back"), "https://portal")

    def test_webhook_checkout_completed_upgrades_user(self):
        event = {"id": "evt_1", "type": "checkout.session.completed",
                 "data": {"object": {"customer": "cus_9", "subscription": "sub_9",
                                     "metadata": {"user_id": str(self.user["id"]), "tier": "pro"}}}}
        with mock.patch("stripe.Webhook.construct_event", return_value=event) as construct:
            result = billing.handle_webhook(b"{}", "sig")
        construct.assert_called_once_with(b"{}", "sig", "whsec_123")
        self.assertTrue(result["handled"])
        user = auth.get_user(self.user["id"])
        self.assertEqual((user["tier"], user["stripe_customer_id"], user["stripe_subscription_id"]), ("pro", "cus_9", "sub_9"))
        # Stripe retries: the same event id is ignored the second time
        billing.set_tier(self.user["id"], "free")
        with mock.patch("stripe.Webhook.construct_event", return_value=event):
            billing.handle_webhook(b"{}", "sig")
        self.assertEqual(auth.get_user(self.user["id"])["tier"], "free")

    def test_webhook_subscription_updated_and_deleted(self):
        billing.set_tier(self.user["id"], "pro", customer_id="cus_5")
        updated = {"id": "evt_2", "type": "customer.subscription.updated",
                   "data": {"object": {"id": "sub_5", "customer": "cus_5", "status": "active",
                                       "items": {"data": [{"price": {"id": "price_ent"}}]}}}}
        self.assertEqual(billing.apply_event(updated)["tier"], "enterprise")
        self.assertEqual(auth.get_user(self.user["id"])["tier"], "enterprise")
        lapsed = dict(updated, id="evt_3")
        lapsed["data"] = {"object": dict(updated["data"]["object"], status="canceled")}
        self.assertEqual(billing.apply_event(lapsed)["tier"], "free")
        billing.set_tier(self.user["id"], "pro")
        deleted = {"id": "evt_4", "type": "customer.subscription.deleted",
                   "data": {"object": {"customer": "cus_5"}}}
        self.assertEqual(billing.apply_event(deleted)["tier"], "free")
        self.assertEqual(auth.get_user(self.user["id"])["tier"], "free")
        unknown = {"id": "evt_5", "type": "invoice.paid", "data": {"object": {}}}
        self.assertFalse(billing.apply_event(unknown)["handled"])

    def test_bad_signature_is_rejected(self):
        import stripe
        err = stripe.error.SignatureVerificationError("bad sig", "sig")
        with mock.patch("stripe.Webhook.construct_event", side_effect=err):
            with self.assertRaisesRegex(billing.BillingError, "Webhook rejected"):
                billing.handle_webhook(b"{}", "sig")

    def test_cli_set_tier_and_list(self):
        import contextlib, io
        self.assertEqual(billing.main(["set-tier", "pay@example.com", "enterprise"]), 0)
        self.assertEqual(auth.get_user(self.user["id"])["tier"], "enterprise")
        self.assertEqual(billing.main(["set-tier", "pay@example.com", "platinum"]), 2)
        self.assertEqual(billing.main(["set-tier", "nobody@example.com", "pro"]), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            billing.main(["list"])
        self.assertIn("pay@example.com", out.getvalue())
        self.assertIn("enterprise", out.getvalue())


class BillingRouteTests(unittest.TestCase):
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
            for t in ("posts", "users", "billing_events"):
                conn.execute(f"DELETE FROM {t}")
        self.user = auth.create_user("route@example.com", "password123")
        self.client.cookies.set(auth.COOKIE_NAME, auth.create_token(self.user["id"]))

    def test_billing_page_shows_plan_and_usage(self):
        for _ in range(3):
            queries.create_draft("x", user_id=self.user["id"])
        page = self.client.get("/billing").text
        self.assertIn("Free", page)
        self.assertIn("3 / 5", page)
        self.assertIn("Online payments are not set up", page)
        usage = self.client.get("/api/billing/usage").json()
        self.assertEqual(usage["usage"]["posts"], 3)
        self.assertEqual(usage["remaining_posts"], 2)

    def test_free_user_cannot_create_sixth_post_or_reel(self):
        for _ in range(5):
            queries.create_draft("x", user_id=self.user["id"])
        r = self.client.post("/posts/new", data={"topic": "six", "caption": "c"},
                             headers={"referer": "http://x/"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])
        self.assertIn("5+of+5", r.headers["location"].replace("%20", "+"))
        set_tier(self.user["id"], "pro")
        r = self.client.post("/posts/new", data={"topic": "reel", "caption": "c", "media_type": "video"},
                             headers={"referer": "http://x/"}, follow_redirects=False)
        self.assertIn("Enterprise", r.headers["location"])
        self.assertEqual(len(queries.list_posts(user_id=self.user["id"])), 5)

    def test_checkout_redirects_to_stripe(self):
        with mock.patch.dict(os.environ, STRIPE_ENV), \
             mock.patch("stripe.checkout.Session.create", return_value=mock.MagicMock(url="https://checkout.stripe.com/x")):
            r = self.client.post("/billing/checkout", data={"tier": "pro"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "https://checkout.stripe.com/x")
        r = self.client.post("/billing/checkout", data={"tier": "pro"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])    # not configured without env

    def test_webhook_route_is_public_and_verifies(self):
        self.client.cookies.clear()
        with mock.patch.dict(os.environ, STRIPE_ENV):
            event = {"id": "evt_r", "type": "checkout.session.completed",
                     "data": {"object": {"customer": "cus_r", "subscription": "sub_r",
                                         "metadata": {"user_id": str(self.user["id"]), "tier": "enterprise"}}}}
            with mock.patch("stripe.Webhook.construct_event", return_value=event):
                r = self.client.post("/api/billing/webhook", content=b"{}", headers={"stripe-signature": "t"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(auth.get_user(self.user["id"])["tier"], "enterprise")
            import stripe
            with mock.patch("stripe.Webhook.construct_event",
                            side_effect=stripe.error.SignatureVerificationError("no", "sig")):
                self.assertEqual(self.client.post("/api/billing/webhook", content=b"{}").status_code, 400)


if __name__ == "__main__":
    unittest.main()
