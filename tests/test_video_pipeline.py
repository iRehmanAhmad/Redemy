"""TDD tests for the T-002 course-to-video pipeline (Asma, Backend).

Covers: edge-tts word-timestamp JSON schema + CLI contract (render_audio),
caption grouping, shot planning, ASS caption generation, lesson meta schema,
upload-pack output (package_class, Saima's <NN-name>/ layout), and frame
distinctness helpers (render_video).

Run from the repo root:  python3 -m unittest discover -s tests
Unit tests are stdlib-only and never touch the network or ffmpeg; the dry-run
(scripted separately) exercises the real render path end to end.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "redemy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import package_class  # noqa: E402
import render_audio  # noqa: E402
import render_video  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402 (new visual tests need PIL)


def W(text, start, end, speaker="A"):
    return {"text": text, "start": start, "end": end, "speaker": speaker}


WORDS = [
    W("Okay", 0.0, 0.3), W("real", 0.3, 0.5), W("talk.", 0.5, 0.8),
    W("For", 0.8, 1.0), W("fifty", 1.0, 1.3), W("years,", 1.3, 1.6),
    W("AI", 1.6, 1.8), W("was", 1.8, 2.0), W("a", 2.0, 2.1), W("party", 2.1, 2.4),
    W("trick.", 2.4, 2.8),
    W("It", 3.0, 3.2, "B"), W("was", 3.2, 3.4, "B"), W("three", 3.4, 3.6, "B"),
    W("boring", 3.6, 3.9, "B"), W("things.", 3.9, 4.2, "B"),
]


class WordTimestampSchema(unittest.TestCase):
    """render_audio --engine edge-tts must emit the JSON the caption pipeline reads."""

    def test_write_word_timestamps_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "words.json"
            events = [("Okay", 0, 3_000_000, "A"), ("real", 5_000_000, 8_000_000, "B")]
            turns = [{"speaker": "A", "start": 0.0, "end": 0.3}]
            render_audio.write_word_timestamps(path, events, turns, engine="edge-tts")
            doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["engine"], "edge-tts")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["words"][0], {"text": "Okay", "start": 0.0, "end": 0.3, "speaker": "A"})
        # edge-tts reports 100ns ticks; the writer converts to seconds.
        self.assertEqual(doc["words"][1]["start"], 0.5)
        self.assertEqual(doc["words"][1]["end"], 0.8)
        self.assertEqual(doc["turns"], turns)

    def test_ticks_to_seconds(self):
        self.assertAlmostEqual(render_audio.ticks_to_seconds(12_345_678), 1.2345678)

    def test_boundary_events_carry_speaker(self):
        """Per-turn synthesis must tag each word with its turn's speaker."""
        chunks = [
            {"type": "audio", "data": b"\x00"},
            {"type": "WordBoundary", "offset": 0, "duration": 3_000_000, "text": "Hi"},
        ]
        got = render_audio.collect_boundaries(chunks, speaker="B", offset_ticks=20_000_000)
        self.assertEqual(got, [("Hi", 20_000_000, 23_000_000, "B")])


class EngineSelection(unittest.TestCase):
    """Saima's CLI contract: argparse, edge-tts default, positional script kept."""

    def test_default_engine_is_edge_tts(self):
        args = render_audio.parse_args(["script.txt", "out.mp3"])
        self.assertEqual(args.engine, "edge-tts")

    def test_elevenlabs_still_selectable(self):
        args = render_audio.parse_args(["script.txt", "--engine", "elevenlabs"])
        self.assertEqual(args.engine, "elevenlabs")

    def test_out_defaults_to_script_dir(self):
        args = render_audio.parse_args(["some/dir/script.txt"])
        script, out, words = render_audio.resolve_paths(args)
        self.assertEqual(out, Path("some/dir/lesson.mp3"))
        self.assertEqual(words, Path("some/dir/words.json"))

    def test_words_flag_overrides(self):
        args = render_audio.parse_args(["s.txt", "--words", "w.json"])
        _, _, words = render_audio.resolve_paths(args)
        self.assertEqual(words, Path("w.json"))


class CaptionGrouping(unittest.TestCase):
    """Design spec §3: 3-4 words per group, never across a speaker change,
    prefer clause-punctuation ends, hard cap 5 words."""

    def test_three_to_four_words_per_group(self):
        groups = render_video.group_captions(WORDS[:11])
        for g in groups:
            self.assertLessEqual(len(g["words"]), 4, g)

    def test_never_splits_across_speaker_change(self):
        groups = render_video.group_captions(WORDS)
        for g in groups:
            speakers = {w["speaker"] for w in g["words"]}
            self.assertEqual(len(speakers), 1, g)

    def test_prefers_clause_punctuation_end(self):
        groups = render_video.group_captions(WORDS[:11])
        # "talk." ends a clause -> group boundary lands right after it.
        self.assertEqual(groups[0]["words"][-1]["text"], "talk.")

    def test_timing_spans_first_to_last_word(self):
        groups = render_video.group_captions(WORDS[:11])
        self.assertEqual((groups[0]["start"], groups[0]["end"]), (0.0, 0.8))

    def test_hard_cap_five_words(self):
        many = [W(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(12)]
        for g in render_video.group_captions(many):
            self.assertLessEqual(len(g["words"]), 5)


class ShotPlanning(unittest.TestCase):
    """Design spec §5: 4-6 shots, hook first, hard cuts, one-rule beat is its own shot."""

    def _turns(self):
        return [
            {"speaker": "A", "text": "t0", "start": 0.0, "end": 20.0},
            {"speaker": "B", "text": "t1", "start": 20.0, "end": 45.0},
            {"speaker": "A", "text": "t2", "start": 45.0, "end": 60.0},
            {"speaker": "B", "text": "t3 rule", "start": 60.0, "end": 80.0},
            {"speaker": "A", "text": "t4", "start": 80.0, "end": 110.0},
            {"speaker": "B", "text": "t5", "start": 110.0, "end": 130.0},
        ]

    def test_four_to_six_shots(self):
        shots = render_video.plan_shots(self._turns(), duration=130.0, rule_turn=3)
        self.assertGreaterEqual(len(shots), 4)
        self.assertLessEqual(len(shots), 6)

    def test_hook_shot_starts_at_zero(self):
        shots = render_video.plan_shots(self._turns(), duration=130.0, rule_turn=3)
        self.assertEqual(shots[0]["start"], 0.0)

    def test_one_rule_beat_is_its_own_shot(self):
        shots = render_video.plan_shots(self._turns(), duration=130.0, rule_turn=3)
        rule_shots = [s for s in shots if s["one_rule"]]
        self.assertEqual(len(rule_shots), 1)
        self.assertEqual((rule_shots[0]["start"], rule_shots[0]["end"]), (60.0, 80.0))

    def test_shots_tile_the_lesson(self):
        shots = render_video.plan_shots(self._turns(), duration=130.0, rule_turn=3)
        self.assertEqual(shots[0]["start"], 0.0)
        self.assertEqual(shots[-1]["end"], 130.0)
        for a, b in zip(shots, shots[1:]):
            self.assertAlmostEqual(a["end"], b["start"])


class AssCaptions(unittest.TestCase):
    """Kinetic captions: karaoke word timing, amber active word, 6px stroke,
    fade in/out, two-line cap, captions pause on the one-rule beat."""

    def test_per_word_events_match_word_timing(self):
        groups = render_video.group_captions(WORDS[:11])
        ass = render_video.build_ass(groups, duration=5.0, rule_beat=None)
        # "Okay" (0.0-0.3) gets its own event, active word painted amber.
        self.assertIn("Dialogue: 0,0:00:00.00,", ass)
        self.assertIn("{\\c&H000AD6FF&}OKAY{\\c&HFFFFFF&}", ass)
        self.assertIn("TALK.", ass)  # rest of the group stays visible

    def test_amber_highlight_and_stroke_in_style(self):
        groups = render_video.group_captions(WORDS[:3])
        ass = render_video.build_ass(groups, duration=5.0, rule_beat=None)
        self.assertIn("0AD6FF", ass)  # --amber #FFD60A as ASS BBGGRR on the active word
        self.assertIn(",1,6,0,5,", ass)  # BorderStyle=1, Outline=6, Shadow=0, Alignment=5

    def test_two_line_cap(self):
        groups = render_video.group_captions(WORDS[:11])
        ass = render_video.build_ass(groups, duration=5.0, rule_beat=None)
        for line in ass.splitlines():
            if line.startswith("Dialogue:"):
                self.assertLessEqual(line.count("\\N"), 1, line)

    def test_captions_pause_on_one_rule_beat(self):
        groups = render_video.group_captions(WORDS)
        ass = render_video.build_ass(groups, duration=5.0, rule_beat=(2.5, 4.5))
        # No dialogue may overlap the one-rule beat.
        for line in ass.splitlines():
            if line.startswith("Dialogue:"):
                start = render_video.ass_time_to_seconds(line.split(",")[1])
                end = render_video.ass_time_to_seconds(line.split(",")[2])
                self.assertFalse(start < 4.5 and end > 2.5, line)

    def test_fade_in_out(self):
        groups = render_video.group_captions(WORDS[:3])
        ass = render_video.build_ass(groups, duration=5.0, rule_beat=None)
        self.assertIn("\\fad(100,0)", ass)  # first word of a group fades in
        self.assertIn("\\fad(0,100)", ass)  # last word of a group fades out


class LessonMeta(unittest.TestCase):
    """meta.json is the render contract: hosts, one-rule beat, thumbnail copy."""

    GOOD = {
        "lesson": 1, "course": "AI Basics for Everyone",
        "title": "Why AI suddenly got smart",
        "headline": ["WHY AI GOT", "SMART"],
        "hosts": {"A": "Nova", "B": "Atlas"},
        "one_rule": {"turn": 6, "headline": "Scale plus attention beats clever hand-coded rules",
                     "sub": "every single time"},
        "description": "Why AI suddenly got smart: data, GPUs, and the transformer.",
        "tags": ["ai", "machine learning", "explainer"],
        "hashtags": ["#AI", "#MachineLearning"],
    }

    def test_valid_meta_passes(self):
        self.assertEqual(render_video.check_meta(self.GOOD), [])

    def test_missing_hosts_fails(self):
        bad = dict(self.GOOD)
        del bad["hosts"]
        self.assertTrue(render_video.check_meta(bad))

    def test_headline_two_line_cap(self):
        bad = dict(self.GOOD, headline=["ONE", "TWO", "THREE"])
        problems = render_video.check_meta(bad)
        self.assertTrue(any("headline" in p for p in problems))

    def test_speaker_names_default(self):
        self.assertEqual(render_video.speaker_name({}, "A"), "HOST A")
        self.assertEqual(render_video.speaker_name({"hosts": {"A": "Nova"}}, "A"), "Nova")


class UploadPack(unittest.TestCase):
    """package_class emits upload-pack/<NN-name>/: thumbnail.png (1280x720) +
    <NN-name>.md with title/description/tags/hashtags (Saima's layout)."""

    def _lesson(self, ws):
        d = ws / "class" / "01-why-ai-got-smart"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(LessonMeta.GOOD), encoding="utf-8")
        (d / "lesson.mp4").write_bytes(b"FAKE")  # presence marker; real file in dry-run
        return d

    def test_thumbnail_png_size(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            d = self._lesson(ws)
            thumb = package_class.render_thumbnail(d, LessonMeta.GOOD, bg=None)
            self.assertEqual(thumb.size, (1280, 720))

    def test_upload_pack_writes_png_and_md(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._lesson(ws)
            out = ws / "upload-pack"
            made = package_class.write_upload_pack(ws, out)
            self.assertEqual(len(made), 1)
            png = out / "01-why-ai-got-smart" / "thumbnail.png"
            md = out / "01-why-ai-got-smart" / "01-why-ai-got-smart.md"
            self.assertTrue(png.exists())
            self.assertTrue(md.exists())
            text = md.read_text(encoding="utf-8")
            for needle in ("Why AI suddenly got smart", "machine learning", "#AI"):
                self.assertIn(needle, text)

    def test_upload_pack_renders_without_video(self):
        # Saima's contract: no lesson.mp4 is not fatal -- thumbnail from card text.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            d = ws / "class" / "01-x"
            d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps(LessonMeta.GOOD), encoding="utf-8")
            out = ws / "upload-pack"
            made = package_class.write_upload_pack(ws, out)
            self.assertEqual(len(made), 1)
            self.assertTrue((out / "01-x" / "thumbnail.png").exists())

    def test_upload_pack_skips_lessons_without_meta(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            d = ws / "class" / "01-x"
            d.mkdir(parents=True)
            out = ws / "upload-pack"
            self.assertEqual(package_class.write_upload_pack(ws, out), [])


class FrameSampling(unittest.TestCase):
    """Dry-run guardrail: frames sampled across the video must differ —
    a text-card-only render fails this."""

    def test_distinct_frames_pass(self):
        frames = [bytes([i]) * 64 for i in (1, 2, 3, 1)]
        self.assertTrue(render_video.frames_are_distinct(
            [render_video.frame_hash(f) for f in frames], need=3))

    def test_identical_frames_fail(self):
        hs = [render_video.frame_hash(b"\x00" * 64)] * 4
        self.assertFalse(render_video.frames_are_distinct(hs, need=3))


class ProgressBarPreRender(unittest.TestCase):
    """Barka audit 2026-10-18: drawbox w='1080*t/dur' never evaluated per-frame
    t on this ffmpeg build, so the bar is pre-rendered as a PNG sequence."""

    def _fill_width(self, img):
        # scan row y=4: gradient fill pixels are opaque (alpha 255),
        # track pixels are (42,42,53) at 70% (alpha 178)
        px = img.load()
        w = 0
        for x in range(img.width):
            if px[x, 4][3] == 255:
                w = x + 1
            else:
                break
        return w

    def _render(self, td, duration=3.7):
        n = render_video.render_progress_bar(Path(td), duration)
        frames = [Image.open(Path(td) / f"bar-{i:04d}.png") for i in range(n)]
        return n, frames

    def test_frame_count_matches_duration(self):
        with tempfile.TemporaryDirectory() as td:
            n, _ = self._render(td, duration=3.7)
        self.assertEqual(n, round(3.7 * render_video.BAR_FPS))

    def test_fill_starts_empty_ends_full(self):
        with tempfile.TemporaryDirectory() as td:
            n, frames = self._render(td)
            self.assertEqual(self._fill_width(frames[0]), 0)
            self.assertEqual(self._fill_width(frames[-1]), render_video.W)

    def test_fill_grows_monotonically(self):
        with tempfile.TemporaryDirectory() as td:
            n, frames = self._render(td)
            widths = [self._fill_width(frames[i]) for i in range(0, n, 5)]
            self.assertTrue(all(b >= a for a, b in zip(widths, widths[1:])),
                            f"bar fill must never shrink: {widths}")
            self.assertGreater(widths[-1], widths[0])

    def test_track_and_separator(self):
        with tempfile.TemporaryDirectory() as td:
            n, frames = self._render(td)
            f0 = frames[0]
            self.assertEqual(f0.size, (render_video.W, 9))  # 8px bar + 1px sep
            self.assertEqual(f0.getpixel((render_video.W - 1, 4))[:3], (42, 42, 53))
            self.assertEqual(f0.getpixel((render_video.W - 1, 4))[3], int(255 * 0.7))
            self.assertEqual(f0.getpixel((540, 8)), (0, 0, 0, 128))  # black@0.5 sep


class ThumbnailEyebrow(unittest.TestCase):
    """Barka audit 2026-10-18: eyebrow must keep 'BASICS' ('LESSON 1 · AI BASICS')."""

    def _fit(self):
        d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font = package_class._font("NotoSans-Bold.ttf", 64)
        return package_class._fit_eyebrow(d, "01", "AI BASICS FOR EVERYONE", font)

    def test_eyebrow_keeps_basics(self):
        self.assertEqual(self._fit(), "LESSON 1 · AI BASICS")

    def test_eyebrow_fits_badge_gap(self):
        d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font = package_class._font("NotoSans-Bold.ttf", 64)
        brow = self._fit()
        rendered = (d.textlength(brow, font=font)
                    + package_class.THUMB_EYEBROW_TRACKING * len(brow))
        # badge at x=1120, eyebrow starts x=80 -> 1040px truly available
        self.assertLessEqual(rendered, 1040)


class OneRuleCardBorder(unittest.TestCase):
    """Barka audit 2026-10-18: gradient border reads faint -> wider ring."""

    def test_border_ring_width(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "card.png"
            render_video.render_rule_card("Scale plus attention beats clever rules", "", p)
            img = Image.open(p).convert("RGBA")
        px = img.load()
        ring = 0
        # y=800: straight left edge (clear of the 36px corner arcs), no text there
        for x in range(90, 140):
            r, g, b, a = px[x, 800]
            if max(r, g, b) - min(r, g, b) > 60:
                ring += 1
            else:
                break
        self.assertEqual(ring, render_video.ONE_RULE_BORDER_W)
        self.assertEqual(render_video.ONE_RULE_BORDER_W, 8)


class OneRuleCardGhostRegression(unittest.TestCase):
    """Barka re-audit 2026-10-18: the card ghosted at ~25% opacity for its
    whole beat. Root cause: the cd-long card stream EOF'd at graph t=cd while
    overlay's enable window sat at rs..rs+cd on the main timeline, so
    eof_action=repeat froze the last card frame (inside the fade-out ramp).
    These tests lock the setpts+absolute-timing construction that fixes it."""

    def _chain(self, rs=68.5, re_=76.2):
        args, fc, out = render_video.card_overlay_filters(9, "[vlt]", Path("card.png"), rs, re_)
        return args, ";".join(fc), out

    def test_stream_shifted_onto_enable_window(self):
        _, chain, _ = self._chain()
        # setpts aligns the cd-long stream with the enable window on the
        # main timeline; without it overlay EOF-repeats the faded last frame
        self.assertIn("setpts=PTS+68.500/TB", chain)

    def test_fade_timings_are_absolute(self):
        _, chain, _ = self._chain()
        cd = 76.2 - 68.5 + 0.4  # 8.1
        self.assertIn(f"fade=t=in:st=68.500:d=0.2:alpha=1", chain)
        self.assertIn(f"fade=t=out:st={68.5 + cd - 0.15:.3f}:d=0.15:alpha=1", chain)

    def test_scale_pop_clamped_non_negative_on_absolute_time(self):
        _, chain, _ = self._chain()
        # the old min(t/0.2,1) goes negative on the absolute timeline and
        # would break scale with a negative width
        self.assertIn("min(max(0,(t-68.500))/0.2,1)", chain)

    def test_input_duration_covers_enable_window(self):
        args, chain, _ = self._chain()
        cd = 76.2 - 68.5 + 0.4  # 8.1
        self.assertEqual(args[:4], ["-loop", "1", "-t", f"{cd:.3f}"])
        self.assertIn(f"enable='between(t,68.500,{76.2 + 0.4:.3f})'", chain)

    def test_output_label_chains(self):
        _, _, out = self._chain()
        self.assertEqual(out, "[vcard]")


class CardAssemblyWiring(unittest.TestCase):
    """2026-10-18: the setpts refactor initially left the old block's
    trailing lines in render_lesson (duplicate overlay + double inp += 1),
    breaking the ffmpeg command ('Invalid input file index'). This test
    stubs the slow prerender steps, captures the assembly command, and
    checks the wiring: every -i index valid, exactly one card overlay."""

    def _capture_assembly(self):
        from unittest import mock
        words = [W("one", 0.0, 0.5), W("two", 0.5, 1.0),
                 W("three", 5.0, 5.5), W("four", 5.5, 6.0)]
        doc = {"version": 1, "engine": "edge-tts", "words": words,
               "turns": [{"speaker": "A", "start": 0.0, "end": 1.0},
                         {"speaker": "B", "start": 5.0, "end": 6.0}]}
        meta = {"hosts": {"A": "Nova", "B": "Atlas"},
                "one_rule": {"turn": 1, "headline": "The headline",
                             "sub": "The sub"}}
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "words.json").write_text(json.dumps(doc))
            (td / "audio.mp3").write_bytes(b"\x00" * 64)
            captured = {}

            def fake_run(cmd, what="ffmpeg"):
                if what == "lesson assembly":
                    captured["cmd"] = cmd
                    raise RuntimeError("STOP-AT-ASSEMBLY")

            blank = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
            with mock.patch.object(render_video, "run", fake_run), \
                 mock.patch.object(render_video, "render_shot_segment",
                                   lambda s, a, seed, tmp: "seg.mp4"), \
                 mock.patch.object(render_video, "render_scrim",
                                   lambda p: blank.save(p)), \
                 mock.patch.object(render_video, "render_progress_bar",
                                   lambda tmp, dur: None), \
                 mock.patch.object(render_video, "render_lower_third",
                                   lambda n, t, c, p: blank.save(p)), \
                 mock.patch.object(render_video, "render_rule_card",
                                   lambda h, s, p: blank.save(p)), \
                 mock.patch.object(render_video, "probe_seconds",
                                   lambda p: 10.0):
                try:
                    render_video.render_lesson(td, td / "audio.mp3",
                                               td / "words.json", meta,
                                               td / "assets", td / "out.mp4",
                                               "seed")
                except RuntimeError as e:
                    self.assertEqual(str(e), "STOP-AT-ASSEMBLY")
        return captured["cmd"]

    def test_audio_map_index_valid(self):
        cmd = self._capture_assembly()
        n_inputs = sum(1 for t in cmd if t == "-i")
        mi = cmd.index("-map")
        audio_idx = int(cmd[mi + 3].split(":")[0])
        self.assertEqual(n_inputs, 6)   # base+scrim+bar, 1 pill, card, audio
        self.assertEqual(audio_idx, 5)  # audio is the last input
        self.assertLess(audio_idx, n_inputs)

    def test_single_card_overlay_with_setpts(self):
        cmd = self._capture_assembly()
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(fc.count("overlay=x='(W-w)/2'"), 1)
        self.assertIn("setpts=PTS+", fc)


if __name__ == "__main__":
    unittest.main()
