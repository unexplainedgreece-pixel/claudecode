"""Tests for src/security/crypto.py and src/security/secrets.py."""

import os
import tempfile
import unittest
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

from config.db import get_connection, init_db          # noqa: E402
from src.security import crypto, secrets                # noqa: E402

KEY = crypto.generate_key()


class CryptoTests(unittest.TestCase):
    def test_roundtrip(self):
        k = crypto.load_key(KEY)
        token = crypto.encrypt("EAAB-very-secret-token", k)
        self.assertTrue(token.startswith("v1:"))
        self.assertNotIn("secret", token)
        self.assertEqual(crypto.decrypt(token, k), "EAAB-very-secret-token")

    def test_same_plaintext_encrypts_differently(self):
        k = crypto.load_key(KEY)
        self.assertNotEqual(crypto.encrypt("x", k), crypto.encrypt("x", k))  # random nonce

    def test_wrong_key_fails(self):
        token = crypto.encrypt("hello", crypto.load_key(KEY))
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt(token, crypto.load_key(crypto.generate_key()))

    def test_tampered_data_fails(self):
        token = crypto.encrypt("hello", crypto.load_key(KEY))
        tampered = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt(tampered, crypto.load_key(KEY))

    def test_unrecognised_format(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt("plain-text-value", crypto.load_key(KEY))

    def test_key_validation(self):
        with mock.patch.dict(os.environ, {"APP_SECRET_KEY": ""}):
            with self.assertRaises(crypto.CryptoError):
                crypto.load_key()
        with self.assertRaises(crypto.CryptoError):
            crypto.load_key("not-base64!!")
        with self.assertRaises(crypto.CryptoError):
            crypto.load_key("c2hvcnQ=")  # "short": wrong length

    def test_key_from_env(self):
        with mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY}):
            self.assertEqual(crypto.decrypt(crypto.encrypt("abc")), "abc")

    def test_keygen_cli(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(crypto.main(["keygen"]), 0)
        line = out.getvalue().strip()
        self.assertTrue(line.startswith("APP_SECRET_KEY="))
        self.assertEqual(len(crypto.load_key(line.split("=", 1)[1])), 32)


class SecretsTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM credentials")
        self.env = mock.patch.dict(os.environ, {"APP_SECRET_KEY": KEY,
                                                "META_PAGE_ID": "your_facebook_page_id_here",
                                                "IMGUR_CLIENT_ID": "real-imgur-id"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_env_wins_over_platform_db(self):
        secrets.set_secret("IMGUR_CLIENT_ID", "db-value")            # platform row (user 0)
        self.assertEqual(secrets.get_secret("IMGUR_CLIENT_ID"), "real-imgur-id")

    def test_users_own_value_wins_over_env(self):
        secrets.set_secret("IMGUR_CLIENT_ID", "alice-imgur", user_id=7)
        self.assertEqual(secrets.get_secret("IMGUR_CLIENT_ID", 7), "alice-imgur")
        self.assertEqual(secrets.get_secret("IMGUR_CLIENT_ID", 8), "real-imgur-id")   # other user: env
        self.assertEqual(secrets.get_secret("IMGUR_CLIENT_ID"), "real-imgur-id")

    def test_placeholder_env_falls_back_to_platform_db(self):
        with self.assertRaises(secrets.ConfigError):
            secrets.get_secret("META_PAGE_ID")
        secrets.set_secret("META_PAGE_ID", "123456")
        self.assertEqual(secrets.get_secret("META_PAGE_ID"), "123456")
        self.assertEqual(secrets.get_secret("META_PAGE_ID", 42), "123456")   # platform default for everyone

    def test_user_cannot_read_another_users_value(self):
        secrets.set_secret("META_ACCESS_TOKEN", "alice-token", user_id=1)
        with self.assertRaises(secrets.ConfigError):
            secrets.get_secret("META_ACCESS_TOKEN", 2)
        desc = {d["name"]: d for d in secrets.describe_secrets(2)}
        self.assertIsNone(desc["META_ACCESS_TOKEN"]["source"])
        secrets.delete_secret("META_ACCESS_TOKEN", user_id=2)             # no effect on user 1
        self.assertEqual(secrets.get_secret("META_ACCESS_TOKEN", 1), "alice-token")

    def test_stored_value_is_encrypted_on_disk(self):
        secrets.set_secret("META_ACCESS_TOKEN", "EAAB-plain-token")
        with get_connection() as conn:
            raw = conn.execute("SELECT value_encrypted FROM credentials").fetchone()["value_encrypted"]
        self.assertTrue(raw.startswith("v1:"))
        self.assertNotIn("plain-token", raw)

    def test_refuses_placeholder_value(self):
        with self.assertRaises(ValueError):
            secrets.set_secret("META_ACCESS_TOKEN", "your_meta_token_here")

    def test_missing_key_blocks_storage(self):
        with mock.patch.dict(os.environ, {"APP_SECRET_KEY": ""}):
            with self.assertRaises(crypto.CryptoError):
                secrets.set_secret("META_ACCESS_TOKEN", "abc")

    def test_wrong_key_gives_config_error_on_read(self):
        secrets.set_secret("META_ACCESS_TOKEN", "abc")
        with mock.patch.dict(os.environ, {"APP_SECRET_KEY": crypto.generate_key()}):
            with self.assertRaises(secrets.ConfigError):
                secrets.get_secret("META_ACCESS_TOKEN")

    def test_delete_and_describe(self):
        secrets.set_secret("META_ACCESS_TOKEN", "EAABlongtokenvalue123", user_id=5)
        desc = {d["name"]: d for d in secrets.describe_secrets(5)}
        self.assertEqual(desc["META_ACCESS_TOKEN"]["source"], "yours")
        self.assertEqual(desc["META_ACCESS_TOKEN"]["masked"], "EAA...e123")
        self.assertEqual(desc["IMGUR_CLIENT_ID"]["source"], "platform")
        self.assertIsNone(desc["META_PAGE_ID"]["source"])
        secrets.delete_secret("META_ACCESS_TOKEN", user_id=5)
        with self.assertRaises(secrets.ConfigError):
            secrets.get_secret("META_ACCESS_TOKEN", 5)


if __name__ == "__main__":
    unittest.main()
