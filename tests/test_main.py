"""Tests for the orchestrator in src/main.py (steps are mocked)."""

import os
import tempfile
import unittest
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

from config.db import get_connection, init_db          # noqa: E402
from src import main as orch                            # noqa: E402
from src.security.secrets import ConfigError            # noqa: E402


def runs():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pipeline_runs ORDER BY id")]


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM pipeline_runs")
            conn.execute("DELETE FROM posts")
        self.calls = []

        def fake(name, code=0):
            def _m(argv):
                self.calls.append((name, list(argv)))
                return code
            return _m

        self.patches = [
            mock.patch.object(orch.image_generator, "main", fake("images")),
            mock.patch.object(orch.audio_generator, "main", fake("audio")),
            mock.patch.object(orch.video_compiler, "main", fake("video")),
            mock.patch.object(orch.uploader, "main", fake("upload")),
            mock.patch.object(orch.publisher, "main", fake("publish")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_steps_run_in_order_and_are_recorded(self):
        result = orch.run_pipeline(steps=["publish", "images", "upload"])
        self.assertTrue(result["ok"])
        self.assertEqual([c[0] for c in self.calls], ["images", "upload", "publish"])
        self.assertEqual([r["name"] for r in result["steps"]], ["images", "upload", "publish"])
        rec = runs()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["steps"], "images,upload,publish")
        self.assertEqual(rec[0]["ok"], 1)
        self.assertIn("Pipeline finished: OK", rec[0]["log"])

    def test_dry_run_flag_passed_through(self):
        orch.run_pipeline(steps=["images"], dry_run=True)
        self.assertEqual(self.calls, [("images", ["--dry-run"])])

    def test_failed_step_does_not_stop_later_steps(self):
        with mock.patch.object(orch.uploader, "main", lambda argv: 1):
            result = orch.run_pipeline(steps=["images", "upload", "publish"])
        self.assertFalse(result["ok"])
        self.assertEqual([c[0] for c in self.calls], ["images", "publish"])
        self.assertEqual(result["summary"], "images=ok, upload=exit 1, publish=ok")
        self.assertEqual(runs()[0]["ok"], 0)

    def test_crashing_step_is_caught(self):
        def boom(argv):
            raise RuntimeError("kaboom")
        with mock.patch.object(orch.publisher, "main", boom):
            result = orch.run_pipeline(steps=["publish"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"][0]["exit_code"], 3)
        self.assertIn("kaboom", runs()[0]["log"])

    def test_generate_step_with_topics(self):
        saved = []
        with mock.patch.object(orch.generator, "generate_post",
                               lambda t, platform, business, user_id=None: {"topic": t, "caption": "c",
                                                                            "hashtags": "", "image_prompt": "p"}), \
             mock.patch.object(orch.generator, "save_post",
                               lambda post, media_type="image", user_id=None: saved.append(post) or 7):
            result = orch.run_pipeline(steps=["generate", "images"], topics=["A", "B"])
        self.assertTrue(result["ok"])
        self.assertEqual([p["topic"] for p in saved], ["A", "B"])
        self.assertEqual([c[0] for c in self.calls], ["images"])

    def test_generate_skipped_without_topics(self):
        orch.run_pipeline(steps=["generate", "images"])
        self.assertEqual([c[0] for c in self.calls], ["images"])
        self.assertEqual(runs()[0]["steps"], "images")

    def test_generate_missing_key_is_config_exit(self):
        def no_key(t, platform, business, user_id=None):
            raise ConfigError("OPENAI_API_KEY is not set.")
        with mock.patch.object(orch.generator, "generate_post", no_key):
            result = orch.run_pipeline(steps=["generate"], topics=["A"])
        self.assertEqual(result["steps"][0]["exit_code"], 2)
        self.assertIn("OPENAI_API_KEY", runs()[0]["log"])

    def test_loop_runs_max_times(self):
        from src import ops
        self.assertEqual(orch.run_loop(interval_minutes=0.0001, max_runs=3), 0)
        self.assertEqual(len(runs()), 0)        # nothing happened in any cycle: quiet, not listed
        self.assertTrue(ops.worker_status()["idle_at"])
        self.assertEqual(len(self.calls), 15)  # 3 runs x 5 steps

    def test_full_order_includes_audio_and_video(self):
        orch.run_pipeline(steps=list(orch.STEP_ORDER))
        self.assertEqual([c[0] for c in self.calls], ["images", "audio", "video", "upload", "publish"])

    def test_generate_video_draft(self):
        saved = []
        with mock.patch.object(orch.generator, "generate_post",
                               lambda t, platform, business, user_id=None: {"topic": t, "caption": "c",
                                                                            "hashtags": "", "image_prompt": "p"}), \
             mock.patch.object(orch.generator, "save_post",
                               lambda post, media_type="image", user_id=None: saved.append(media_type) or 1):
            self.assertEqual(orch.main(["generate", "Reel topic", "--media-type", "video"]), 0)
        self.assertEqual(saved, ["video"])

    def test_concurrent_run_is_skipped(self):
        orch._run_lock.acquire()
        try:
            result = orch.run_pipeline(steps=["images"])
        finally:
            orch._run_lock.release()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(self.calls, [])

    def test_cli_run(self):
        self.assertEqual(orch.main(["run", "--steps", "images,upload"]), 0)
        self.assertEqual([c[0] for c in self.calls], ["images", "upload"])
        self.assertEqual(orch.main(["run", "--steps", "nonsense"]), 2)


if __name__ == "__main__":
    unittest.main()


class IdleRunTests(unittest.TestCase):
    """A background cycle that found nothing to do leaves no run record, only a 'checked at' note."""

    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM pipeline_runs")
            conn.execute("DELETE FROM posts")
            conn.execute("DELETE FROM app_state")

    def _count(self):
        with get_connection() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM pipeline_runs").fetchone()["n"]

    def test_quiet_cycle_is_not_recorded_but_manual_runs_and_busy_cycles_are(self):
        from src import ops
        steps = ("recycle", "upload", "publish", "metrics")
        with mock.patch("src.tiktok.check_pending", return_value={}):
            r = orch.run_pipeline(steps=steps, user_id=None, quiet_if_idle=True)
        self.assertTrue(r.get("idle"))
        self.assertEqual(self._count(), 0)
        self.assertTrue(ops.worker_status()["idle_at"] or ops.get_state("worker_idle")[1])
        # the same cycle started by hand from the dashboard is always listed
        with mock.patch("src.tiktok.check_pending", return_value={}):
            r = orch.run_pipeline(steps=steps, user_id=None)
        self.assertFalse(r.get("idle"))
        self.assertEqual(self._count(), 1)
        # a warning makes the cycle worth listing
        def noisy(user_id=None):
            orch.log.warning("post #1 instagram: Meta API error 10")
            return 0
        with mock.patch.object(orch.analytics, "run_step", side_effect=noisy), \
             mock.patch("src.tiktok.check_pending", return_value={}):
            r = orch.run_pipeline(steps=steps, user_id=None, quiet_if_idle=True)
        self.assertFalse(r.get("idle"))
        self.assertEqual(self._count(), 2)
