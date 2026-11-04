"""
Tests for src/video/video_compiler.py.

Pure logic (chunking, timing, audio mixing) runs for real. The MoviePy render
is mocked in the flow tests, and exercised once for real at a tiny size in
RealRenderTest, which is skipped when ffmpeg is not installed.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SCHEDULER_DB_PATH"] = _tmp_db.name
os.environ["CSRF_DISABLED"] = "1"

from PIL import Image                                   # noqa: E402
from pydub import AudioSegment                          # noqa: E402
from pydub.generators import Sine                       # noqa: E402
from config.db import get_connection, init_db          # noqa: E402
from src.video import video_compiler as vc              # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_tone(path: Path, seconds: float = 2.0, freq: int = 440):
    Sine(freq).to_audio_segment(duration=int(seconds * 1000)).apply_gain(-6).export(path, format="mp3")
    return path


def insert_post(**overrides):
    row = {"topic": "t", "caption": "Golden hour on the harbour. Cold drinks, no rush. #rhodes",
           "status": "draft", "media_type": "video"}
    row.update(overrides)
    cols, params = ",".join(row), ",".join(f":{k}" for k in row)
    with get_connection() as conn:
        return conn.execute(f"INSERT INTO posts ({cols}) VALUES ({params})", row).lastrowid


def get_post(pid):
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


class ChunkingTests(unittest.TestCase):
    def test_groups_of_three_without_orphan(self):
        words = "one two three four five six seven".split()
        self.assertEqual(vc.chunk_words(words, 3), [["one", "two", "three"], ["four", "five", "six", "seven"]])
        self.assertEqual(vc.chunk_words(words[:6], 3), [["one", "two", "three"], ["four", "five", "six"]])
        self.assertEqual(vc.chunk_words(["solo"], 3), [["solo"]])
        self.assertEqual(vc.chunk_words([], 3), [])
        self.assertEqual(vc.chunk_words(words[:3], 0), [["one"], ["two"], ["three"]])

    def test_estimate_timeline_covers_audio_exactly(self):
        text = "Your next customer reads reviews before they read your menu. Book a table today!"
        chunks = vc.estimate_timeline(text, 10.0, 3)
        self.assertEqual(chunks[0].start, 0.0)
        self.assertEqual(chunks[-1].end, 10.0)
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a.end, b.start)           # no gaps, no overlaps
            self.assertGreater(a.duration, 0)
        self.assertEqual(" ".join(c.text for c in chunks), text)

    def test_longer_chunks_get_more_time_and_sentence_ends_pause(self):
        chunks = vc.estimate_timeline("a b c. extraordinarily complicated vocabulary", 10.0, 3)
        self.assertLess(chunks[0].duration, chunks[1].duration)
        plain = vc.estimate_timeline("a b c extraordinarily complicated vocabulary", 10.0, 3)
        self.assertGreater(chunks[0].duration, plain[0].duration)   # the '.' adds a pause

    def test_empty_text_or_duration(self):
        self.assertEqual(vc.estimate_timeline("", 5.0), [])
        self.assertEqual(vc.estimate_timeline("hi there", 0), [])

    def test_whisper_timeline_groups_real_timestamps(self):
        words = [SimpleNamespace(word=w, start=i * 0.5, end=i * 0.5 + 0.4)
                 for i, w in enumerate("one two three four five".split())]
        client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(words=words))))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
             mock.patch("openai.OpenAI", return_value=client), \
             tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            chunks = vc.whisper_timeline(Path(f.name), 3)
        self.assertEqual([c.text for c in chunks], ["one two three", "four five"])
        self.assertEqual(chunks[0].start, 0.0)
        self.assertEqual(chunks[0].end, chunks[1].start)      # gap closed
        self.assertAlmostEqual(chunks[1].end, 2.4)

    def test_build_timeline_falls_back_to_estimate(self):
        with mock.patch.object(vc, "CAPTION_SYNC", "whisper"), \
             mock.patch.object(vc, "whisper_timeline", side_effect=vc.VideoError("no words")):
            chunks = vc.build_timeline("one two three four five", Path("/nope.mp3"), 4.0)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[-1].end, 4.0)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duration(self):
        self.assertAlmostEqual(vc.audio_duration(make_tone(self.dir / "a.mp3", 2.0)), 2.0, delta=0.15)
        with self.assertRaises(vc.VideoError):
            vc.audio_duration(self.dir / "missing.mp3")

    def test_mix_without_music_copies_speech(self):
        speech = make_tone(self.dir / "s.mp3", 1.5)
        out = vc.mix_audio(speech, self.dir / "mix.mp3", None)
        self.assertAlmostEqual(len(AudioSegment.from_file(out)) / 1000, 1.5, delta=0.15)

    def test_mix_loops_short_music_under_speech(self):
        speech = make_tone(self.dir / "s.mp3", 3.0, 440)
        music = make_tone(self.dir / "m.mp3", 0.7, 110)       # shorter: must loop
        out = vc.mix_audio(speech, self.dir / "mix.mp3", music)
        mixed = AudioSegment.from_file(out)
        self.assertAlmostEqual(len(mixed) / 1000, 3.0, delta=0.15)
        self.assertLess(abs(mixed.dBFS - AudioSegment.from_file(speech).dBFS), 3)  # music is quiet

    def test_pick_music(self):
        with mock.patch.object(vc, "BG_MUSIC_DIR", self.dir / "none"):
            self.assertIsNone(vc.pick_music())
        with mock.patch.object(vc, "BG_MUSIC_DIR", self.dir):
            self.assertIsNone(vc.pick_music())
            make_tone(self.dir / "track.mp3", 0.3)
            (self.dir / "notes.txt").write_text("ignored")
            self.assertEqual(vc.pick_music().name, "track.mp3")


class FlowTests(unittest.TestCase):
    """compile_for_post with the heavy render replaced by a fake."""

    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM posts")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.audio = self.dir / "speech.mp3"
        self.audio.write_bytes(b"fake")
        self.image = self.dir / "post.png"
        Image.new("RGB", (64, 64), (10, 20, 30)).save(self.image)
        self.patches = [
            mock.patch.object(vc, "VIDEO_DIR", self.dir / "videos"),
            mock.patch.object(vc, "STOCK_DIR", self.dir / "stock"),
            mock.patch.object(vc, "BG_MUSIC_DIR", self.dir / "music"),
            mock.patch.object(vc, "audio_duration", lambda p: 4.0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fake_render(self, calls):
        def render(visual, audio, out, text, music_path=None, chunks=None, **kw):
            calls.append({"visual": visual, "audio": audio, "out": out, "text": text,
                          "music": music_path, "chunks": chunks})
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00" * 2048)
            return out
        return render

    def test_renders_posts_with_audio_and_image(self):
        pid = insert_post(audio_path=str(self.audio), image_path=str(self.image))
        image_post = insert_post(media_type="image", audio_path=str(self.audio), image_path=str(self.image))
        no_audio = insert_post(image_path=str(self.image))
        calls = []
        with mock.patch.object(vc, "render", self.fake_render(calls)):
            self.assertEqual(vc.main([]), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["visual"], self.image)
        self.assertEqual(calls[0]["text"], "Golden hour on the harbour. Cold drinks, no rush.")
        self.assertEqual(len(calls[0]["chunks"]), 3)
        post = get_post(pid)
        self.assertTrue(post["video_path"].endswith(f"post_{pid}.mp4"))
        self.assertIsNone(get_post(image_post)["video_path"])
        self.assertIsNone(get_post(no_audio)["video_path"])

    def test_prefers_jpeg_then_png_then_stock(self):
        jpeg = self.dir / "post.jpg"
        Image.new("RGB", (64, 64)).save(jpeg)
        self.assertEqual(vc.pick_visual({"jpeg_path": str(jpeg), "image_path": str(self.image)}), jpeg)
        self.assertEqual(vc.pick_visual({"jpeg_path": "/gone.jpg", "image_path": str(self.image)}), self.image)
        self.assertIsNone(vc.pick_visual({"jpeg_path": None, "image_path": None}))
        (self.dir / "stock").mkdir()
        (self.dir / "stock" / "beach.mp4").write_bytes(b"x")
        self.assertEqual(vc.pick_visual({"jpeg_path": None, "image_path": None}).name, "beach.mp4")

    def test_missing_audio_file_and_missing_visual_are_recorded(self):
        gone = insert_post(audio_path="/gone/audio.mp3", image_path=str(self.image))
        no_visual = insert_post(audio_path=str(self.audio))
        with mock.patch.object(vc, "render", self.fake_render([])):
            self.assertEqual(vc.main([]), 1)
        self.assertIn("no audio_path", get_post(gone)["last_error"])
        self.assertIn("no image and no stock clip", get_post(no_visual)["last_error"])

    def test_render_errors_are_recorded_and_contained(self):
        a = insert_post(audio_path=str(self.audio), image_path=str(self.image))
        b = insert_post(audio_path=str(self.audio), image_path=str(self.image))
        def render(visual, audio, out, text, music_path=None, chunks=None, **kw):
            if out.name == f"post_{a}.mp4":
                raise vc.VideoError("font missing")
            raise RuntimeError("moviepy exploded")
        with mock.patch.object(vc, "render", render):
            self.assertEqual(vc.main([]), 1)
        self.assertIn("font missing", get_post(a)["last_error"])
        self.assertIn("render crashed: moviepy exploded", get_post(b)["last_error"])

    def test_music_is_passed_when_available(self):
        pid = insert_post(audio_path=str(self.audio), image_path=str(self.image))
        (self.dir / "music").mkdir()
        (self.dir / "music" / "chill.mp3").write_bytes(b"x")
        calls = []
        with mock.patch.object(vc, "render", self.fake_render(calls)):
            vc.main(["--id", str(pid)])
        self.assertEqual(calls[0]["music"].name, "chill.mp3")

    def test_dry_run(self):
        pid = insert_post(audio_path=str(self.audio), image_path=str(self.image))
        with mock.patch.object(vc, "render", side_effect=AssertionError("must not render")):
            self.assertEqual(vc.main(["--dry-run"]), 0)
        self.assertIsNone(get_post(pid)["video_path"])

    def test_find_font(self):
        with mock.patch.object(vc, "FONT_CANDIDATES", ["/nope.ttf"]):
            with self.assertRaises(vc.VideoError):
                vc.find_font()


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class RealRenderTest(unittest.TestCase):
    """One genuine MoviePy render at a small size, so API misuse is caught."""

    def test_image_and_stock_clip_render_to_mp4(self):
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            speech = make_tone(d / "speech.mp3", 1.6)
            music = make_tone(d / "music.mp3", 0.5, 110)
            Image.new("RGB", (400, 300), (30, 90, 160)).save(d / "bg.png")   # landscape: must be cropped
            text = "Golden hour on the harbour tonight"
            out = vc.render(d / "bg.png", speech, d / "out.mp4", text, music_path=music,
                            size=(180, 320), fps=12)
            self.assertTrue(out.exists() and out.stat().st_size > 1000)
            info = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(out)]))
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
            self.assertEqual((video["width"], video["height"], video["codec_name"]), (180, 320, "h264"))
            self.assertEqual(audio["codec_name"], "aac")
            self.assertAlmostEqual(float(info["format"]["duration"]), 1.6, delta=0.3)

            # a 0.5 s stock clip must be looped to the speech length
            from moviepy import ImageClip
            ImageClip(str(d / "bg.png")).with_duration(0.5).write_videofile(
                str(d / "stock.mp4"), fps=12, codec="libx264", audio=False, logger=None)
            out2 = vc.render(d / "stock.mp4", speech, d / "out2.mp4", text, size=(180, 320), fps=12)
            info2 = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(out2)]))
            self.assertAlmostEqual(float(info2["format"]["duration"]), 1.6, delta=0.3)

    def test_too_short_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            Image.new("RGB", (64, 64)).save(d / "bg.png")
            with self.assertRaisesRegex(vc.VideoError, "too short"):
                vc.render(d / "bg.png", make_tone(d / "s.mp3", 0.2), d / "o.mp4", "hi", size=(64, 112))


if __name__ == "__main__":
    unittest.main()
