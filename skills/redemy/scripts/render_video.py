"""Render a lesson (dialogue + MP3 + word timestamps) into a 1080x1920 Short.

Visual system: dark, punchy, high-contrast (see design-spec.md in the task
folder). Shot-list driven (4-6 shots per lesson, hook first), word-by-word
kinetic captions burned in from edge-tts timestamps, speaker-aware
lower-thirds, the "one rule" card ONLY on its flagged beat, progress bar.
Text-card-only output is a FAIL: every shot carries real motion — stock
footage from assets/ or a Ken Burns still, never a blank frame.

$0: system fonts + PIL + ffmpeg only. No stock subscriptions, no APIs.

Lesson contract (<lesson>/):
    script.txt   A:/B: dialogue lines (render_audio.py input)
    lesson.mp3   rendered audio (--audio override)
    words.json   word timestamps (--words override; edge-tts only)
    meta.json    {lesson, course, title, headline[<=2], hosts{A,B},
                  one_rule{turn, headline, sub}, description, tags[], hashtags[]}

Assets contract (--assets, default skills/redemy/assets/):
    manifest.json  {"shots": [{"shot": 1, "file": "hook.mp4",
                               "source": "Pexels video 123 by Jane, pexels.com/...",
                               "license": "Pexels License"}]}
    Missing footage for a shot = Ken Burns still fallback (never blank).

Usage: render_video.py (--lesson LESSON | --workspace WS --compile)
           [--audio A] [--words W] [--out O] [--assets DIR] [--shot-seed S]
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1080, 1920
FPS = 30
HERE = Path(__file__).resolve().parent
DEFAULT_ASSETS = HERE.parent / "assets"

FONT_DIR = "/usr/share/fonts/truetype/noto"
FONTS = {
    "caption": f"{FONT_DIR}/NotoSans-ExtraBold.ttf",
    "bold": f"{FONT_DIR}/NotoSans-Bold.ttf",
    "black": f"{FONT_DIR}/NotoSans-Black.ttf",
    "regular": f"{FONT_DIR}/NotoSans-Regular.ttf",
}
ASS_CAPTION_FONT = "Noto Sans ExtraBold"  # fontconfig family name (fc-list)

PAL = {
    "ink": "#0A0A0F",
    "white": "#FFFFFF",
    "host_a": "#00E5FF",   # Nova / Host A
    "host_b": "#FF3D81",   # Atlas / Host B
    "amber": "#FFD60A",
    "muted": "#9AA0B4",
    "track": "#2A2A35",
    "success": "#00E676",
}


def die(msg, hint=None):
    print(f"render_video: error: {msg}", file=sys.stderr)
    if hint:
        print(f"render_video: hint: {hint}", file=sys.stderr)
    sys.exit(1)


def note(msg):
    print(f"render_video: note: {msg}", file=sys.stderr)


def run(cmd, what="ffmpeg"):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-8:]
        die(f"{what} failed: " + " | ".join(tail))


def probe_seconds(path):
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip())


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# Pure logic (unit-tested)
# --------------------------------------------------------------------------

CLAUSE_END = re.compile(r"[,.:;?!]$")


def group_captions(words, max_words=4):
    """3-4 words per caption group; never across a speaker change; prefer to
    end at clause punctuation; hard cap 5 words."""
    groups = []
    cur = []
    for w in words:
        if cur and w["speaker"] != cur[0]["speaker"]:
            groups.append(_mk_group(cur))
            cur = []
        cur.append(w)
        if len(cur) >= 5 or len(cur) >= max_words or (len(cur) >= 2 and CLAUSE_END.search(w["text"])):
            groups.append(_mk_group(cur))
            cur = []
    if cur:
        groups.append(_mk_group(cur))
    return groups


def _mk_group(words):
    return {"words": words, "start": words[0]["start"], "end": words[-1]["end"],
            "speaker": words[0]["speaker"]}


def plan_shots(turns, duration, rule_turn=None, target=5):
    """Split turns into 4-6 shots. The one-rule turn is always its own shot;
    other cuts prefer speaker changes. Shot 0 starts at 0 (the hook)."""
    n = len(turns)
    if n == 0:
        die("no turns to plan shots from")
    target = max(4, min(6, target))
    if rule_turn is None:
        chunks = _split_turns(list(range(n)), turns, target)
        rule_idx = None
    else:
        if not (0 <= rule_turn < n):
            die(f"one_rule.turn={rule_turn} out of range (0..{n - 1})")
        pre = list(range(0, rule_turn))
        post = list(range(rule_turn + 1, n))
        rest = target - 1
        pre_n = _share(rest, len(pre), len(pre) + len(post))
        post_n = rest - pre_n
        chunks = _split_turns(pre, turns, pre_n) + [[rule_turn]] + _split_turns(post, turns, post_n)
        rule_idx = len(_split_turns(pre, turns, pre_n))
    shots = []
    for i, idxs in enumerate(chunks):
        start = 0.0 if i == 0 else turns[idxs[0]]["start"]
        end = duration if i == len(chunks) - 1 else turns[idxs[-1]]["end"]
        shots.append({
            "index": i, "start": round(start, 3), "end": round(end, 3),
            "speakers": sorted({turns[j]["speaker"] for j in idxs}),
            "one_rule": (i == rule_idx),
        })
    return shots


def _share(total, a_count, ab_count):
    if ab_count == 0:
        return 0
    if a_count == 0:
        return 0
    return max(1, round(total * a_count / ab_count)) if total else 0


def _split_turns(idxs, turns, chunks):
    """Split contiguous turn indices into `chunks` chunks, cutting as close as
    possible to even time splits, preferring speaker changes."""
    if not idxs or chunks <= 0:
        return []
    chunks = min(chunks, len(idxs))
    if chunks == 1:
        return [idxs]
    t0 = turns[idxs[0]]["start"]
    total = turns[idxs[-1]]["end"] - t0
    cuts, lo = [], 0
    for c in range(1, chunks):
        goal = t0 + total * c / chunks
        best = None
        for i in range(lo, len(idxs) - 1):
            cut = turns[idxs[i]]["end"]
            if cuts and cut <= cuts[-1]:
                continue
            spk_change = turns[idxs[i]]["speaker"] != turns[idxs[i + 1]]["speaker"]
            score = (abs(cut - goal), 0 if spk_change else 1)
            if best is None or score < best[0]:
                best = (score, i)
        _, i = best
        cuts.append(turns[idxs[i]]["end"])
        lo = i + 1
    out, start = [], 0
    for ci, cut in enumerate(cuts):
        end = next(i for i in range(start, len(idxs)) if turns[idxs[i]]["end"] >= cut - 1e-6)
        out.append(idxs[start:end + 1])
        start = end + 1
    out.append(idxs[start:])
    return [c for c in out if c]


LATIN = re.compile(r"^[\x00-\x7F]*$")


def _upper(word):
    # Uppercase at render time; non-Latin scripts (Urdu/Hindi Phase 1) keep casing.
    return word.upper() if LATIN.match(word) else word


def _ass_escape(s):
    return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_time_to_seconds(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def build_ass(groups, duration, rule_beat=None):
    """Word-by-word kinetic captions as ASS.

    Each word gets its own Dialogue event spanning [word.start, next.start):
    the full group text renders white with ONLY the active word amber. This
    is the TikTok/Shorts highlight behavior; karaoke \\kf sweeps the wrong
    way (it leaves sung words highlighted), so events are used instead.
    (Design spec §12 suggests per-group PIL PNGs; one ASS track renders the
    same word-by-word semantics in a single filter — see findings-asma.md.)"""
    style = ("Style: Caption,{font},66,&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,"
             "0,0,0,0,100,100,0,0,1,6,0,5,0,0,0,0,0,0".format(font=ASS_CAPTION_FONT))
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n" + style +
            "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, "
            "MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for g in groups:
        if rule_beat and g["start"] < rule_beat[1] and g["end"] > rule_beat[0]:
            continue  # captions pause on the one-rule beat; the card IS the text
        disp = [_ass_escape(_upper(w["text"])) for w in g["words"]]
        l1, l2 = _split_lines(disp)
        for i, w in enumerate(g["words"]):
            start = w["start"]
            end = g["words"][i + 1]["start"] if i + 1 < len(g["words"]) else g["end"]
            end = max(end, w["end"])
            fade = (100, 0) if i == 0 else ((0, 100) if i == len(g["words"]) - 1 else (0, 0))
            body = _word_active_line(l1, l2, i)
            text = ("{\\fad(%d,%d)\\fsp6\\an5\\pos(540,940)}" % fade) + body
            lines.append("Dialogue: 0,%s,%s,Caption,,0,0,0,,%s"
                         % (ass_time(start), ass_time(end), text))
    return head + "\n".join(lines) + "\n"


def _split_lines(words):
    """One line if <=3 short words, else two balanced lines (spec §3)."""
    if len(words) <= 3 and sum(len(w) for w in words) <= 24:
        return words, []
    mid = (len(words) + 1) // 2
    return words[:mid], words[mid:]


def _word_active_line(l1, l2, active):
    """Group text with the active word amber; \\N between the two lines."""
    def paint(words, offset):
        parts = []
        for j, wd in enumerate(words):
            if offset + j == active:
                parts.append("{\\c&H000AD6FF&}%s{\\c&HFFFFFF&}" % wd)
            else:
                parts.append(wd)
        return " ".join(parts)
    line1 = paint(l1, 0)
    if not l2:
        return line1
    return line1 + "\\N" + paint(l2, len(l1))


def check_meta(meta):
    """Validate the lesson meta.json render contract."""
    problems = []
    for key in ("lesson", "course", "title", "headline", "hosts", "one_rule"):
        if key not in meta:
            problems.append(f"meta.json missing {key!r}")
    hl = meta.get("headline")
    if isinstance(hl, list) and len(hl) > 2:
        problems.append("meta.json headline: max 2 lines")
    hosts = meta.get("hosts", {})
    if isinstance(hosts, dict):
        for sp in ("A", "B"):
            if sp not in hosts:
                problems.append(f"meta.json hosts missing {sp!r}")
    rule = meta.get("one_rule", {})
    if isinstance(rule, dict):
        if not isinstance(rule.get("turn"), int):
            problems.append("meta.json one_rule.turn must be an int (0-based turn index)")
        if not rule.get("headline"):
            problems.append("meta.json one_rule.headline is empty")
    return problems


def speaker_name(meta, sp):
    return (meta.get("hosts") or {}).get(sp, f"HOST {sp}")


def frame_hash(frame_bytes):
    return hashlib.md5(frame_bytes).hexdigest()


def frames_are_distinct(hashes, need=3):
    """Anti-text-card guardrail: at least `need` different frames sampled
    across the video."""
    return len(set(hashes)) >= need


# --------------------------------------------------------------------------
# PIL overlay renderers (design-spec §§3,4,6)
# --------------------------------------------------------------------------

def _font(path, size):
    if not os.path.exists(path):
        die(f"font not found: {path}")
    return ImageFont.truetype(path, size)


def _tracked(draw, xy, text, font, fill, tracking, anchor="la", stroke_width=0, stroke_fill=None):
    """Draw text with letter-spacing (PIL has no tracking)."""
    x, y = xy
    kwargs = dict(stroke_width=stroke_width, stroke_fill=stroke_fill) if stroke_width else {}
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor=anchor, **kwargs)
        x += draw.textlength(ch, font=font) + tracking
    return x


def render_scrim(path):
    """Caption-band legibility scrim: black 55% at y=600 -> 0% at y=500/1250."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = img.load()
    for y in range(H):
        if y < 500 or y > 1250:
            a = 0
        elif y < 600:
            a = int(140 * (y - 500) / 100)
        elif y <= 1150:
            a = 140
        else:
            a = int(140 * (1250 - y) / 100)
        if a:
            for x in range(W):
                px[x, y] = (0, 0, 0, a)
    img.save(path)


BAR_FPS = 10       # pre-rendered bar animation rate (overlay repeats frames)
BAR_H = 8          # spec §7: 8px tall, top edge y=0..8
BAR_SEP_H = 1      # 1px black@0.5 separator under the bar (y=8)


def render_progress_bar(tmp_dir, duration):
    """Pre-render the top progress bar as a PNG sequence (BAR_FPS x duration).

    The ffmpeg build on this VM does NOT evaluate per-frame `t` in
    drawbox's `w` (verified 2026-10-18: w='1080*t/dur' rendered full-width
    at every sampled time), so the growing fill is baked frame-by-frame:
    track #2A2A35@70% + growing horizontal cyan->pink gradient fill +
    1px black@0.5 separator. Fed back as a `-framerate BAR_FPS` image2
    input, overlay=0:0. Returns the frame count.
    """
    n = max(2, int(round(duration * BAR_FPS)))
    strip = Image.new("RGBA", (W, BAR_H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(strip)
    c0, c1 = hex_to_rgb(PAL["host_a"]), hex_to_rgb(PAL["host_b"])
    for x in range(W):
        t = x / (W - 1)
        col = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)) + (255,)
        gd.line([(x, 0), (x, BAR_H - 1)], fill=col)
    track = hex_to_rgb(PAL["track"]) + (int(255 * 0.7),)
    for i in range(n):
        fw = int(round(W * i / (n - 1)))  # frame 0: empty track, last: full
        img = Image.new("RGBA", (W, BAR_H + BAR_SEP_H), track)
        if fw > 0:
            img.paste(strip.crop((0, 0, fw, BAR_H)), (0, 0))
        ImageDraw.Draw(img).line([(0, BAR_H), (W - 1, BAR_H)], fill=(0, 0, 0, 128))
        img.save(tmp_dir / f"bar-{i:04d}.png")
    return n


def render_lower_third(name, tag, color_hex, path):
    """Speaker pill: bottom-left x=90, y-top=1420, h=84, radius 28, accent bar."""
    name_f = _font(FONTS["bold"], 44)
    tag_f = _font(FONTS["bold"], 28)
    color = hex_to_rgb(color_hex)
    muted = hex_to_rgb(PAL["muted"])
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    name_w = tmp.textlength(name, font=name_f)
    tag_w = tmp.textlength(tag, font=tag_f)
    pw = int(36 + 20 + name_w + 20 + tag_w + 36)
    ph = 84
    img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=28, fill=(12, 12, 18, 205))
    d.rounded_rectangle([14, 12, 20, ph - 12], radius=3, fill=color)  # accent bar
    d.text((36 + 20, ph / 2), name, font=name_f, fill=color, anchor="lm")
    d.text((36 + 20 + name_w + 20, ph / 2), tag, font=tag_f, fill=muted, anchor="lm")
    img.save(path)


def _wrap(draw, text, font, max_w, max_lines):
    words = text.split()
    lines, cur = [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines[:max_lines]


# One-rule card gradient border width (visible ring). Barka's audit recipe said
# 3px -> 4px, but the rendered ring was actually 6px (inner panel inset 6);
# bumped to 8px for the same proportional visibility increase.
ONE_RULE_BORDER_W = 8


def render_rule_card(headline, sub, path):
    """The one-rule card: centered glass panel x=90..990, y=700..1160."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, x1, y0, y1 = 90, 990, 700, 1160
    bw = ONE_RULE_BORDER_W
    # gradient border: cyan -> pink outer rect, ink inner rect inset bw
    grad = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    c0, c1 = hex_to_rgb(PAL["host_a"]), hex_to_rgb(PAL["host_b"])
    for x in range(x1 - x0):
        t = x / max(1, (x1 - x0) - 1)
        col = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)) + (255,)
        gd.line([(x, 0), (x, y1 - y0)], fill=col)
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], radius=36, fill=255)
    img.paste(grad, (x0, y0), mask)
    d.rounded_rectangle([x0 + bw, y0 + bw, x1 - bw, y1 - bw], radius=36 - bw,
                        fill=(10, 10, 20, 225))
    # success check left of eyebrow
    ey = _font(FONTS["bold"], 52)
    amber = hex_to_rgb(PAL["amber"])
    cx, cy, cr = x0 + 96, 760 + 26, 32
    d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], outline=hex_to_rgb(PAL["success"]), width=6)
    d.line([cx - 14, cy, cx - 4, cy + 10, cx + 15, cy - 12], fill=hex_to_rgb(PAL["success"]), width=7, joint="curve")
    _tracked(d, (cx + cr + 24, 760), "THE ONE RULE", ey, amber, 18)
    max_w = (x1 - x0) - 96
    size = 84
    while size > 60:
        hl = _font(FONTS["black"], size)
        lines = _wrap(d, headline, hl, max_w, 3)
        # _wrap truncates; detect a dropped tail by re-wrapping without the cap
        full = _wrap(d, headline, hl, max_w, 99)
        if len(full) <= 3:
            lines = full
            break
        size -= 4
    lh = size + 10
    block_h = len(lines) * lh + (48 if sub else 0)
    yy = y0 + (y1 - y0 - block_h) / 2 + 30  # optical center below the eyebrow
    for ln in lines:
        d.text(((x0 + x1) / 2, yy), ln, font=hl, fill="white", anchor="ma")
        yy += lh
    if sub:
        d.text(((x0 + x1) / 2, yy + 6), sub, font=_font(FONTS["regular"], 40),
               fill=hex_to_rgb(PAL["muted"]), anchor="ma")
    img.save(path)


def render_shot_still(shot_index, seed, path):
    """Ken Burns fallback still: dark procedural abstract, distinct per shot."""
    rng = random.Random(f"{seed}-{shot_index}")
    base = [(8, 10, 22), (14, 8, 20), (10, 14, 24), (16, 10, 18), (8, 16, 20), (12, 12, 26)][shot_index % 6]
    top = tuple(min(255, c + rng.randint(18, 42)) for c in base)
    img = Image.new("RGB", (W, H), base)
    d = ImageDraw.Draw(img, "RGBA")
    for y in range(H):
        t = y / H
        col = tuple(int(base[i] + (top[i] - base[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=col)
    glow = [hex_to_rgb(PAL["host_a"]), hex_to_rgb(PAL["host_b"]), hex_to_rgb(PAL["amber"])]
    for _ in range(16):
        r = rng.randint(60, 260)
        x, y = rng.randint(0, W), rng.randint(200, 1700)
        c = rng.choice(glow)
        for rr in range(r, 0, -20):
            a = max(0, 26 - (r - rr) // 8)
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=c + (a,))
    for _ in range(7):  # faint diagonal streaks
        x = rng.randint(-200, W)
        d.line([(x, 0), (x + 300, H)], fill=(255, 255, 255, 8), width=rng.randint(2, 9))
    img.save(path)


# --------------------------------------------------------------------------
# ffmpeg assembly
# --------------------------------------------------------------------------

def load_assets_manifest(assets_dir):
    man = Path(assets_dir) / "manifest.json"
    if not man.is_file():
        return {}
    try:
        doc = json.loads(man.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for entry in doc.get("shots", []) if isinstance(doc, dict) else []:
        f = Path(assets_dir) / entry.get("file", "")
        if f.is_file():
            out[int(entry.get("shot", 0))] = f
    return out


def render_shot_segment(shot, assets, seed, tmp):
    """One shot -> silent mp4 of exactly (end-start) seconds, 1080x1920@30."""
    dur = max(0.5, shot["end"] - shot["start"])
    frames = max(15, int(round(dur * FPS)))
    out = tmp / f"shot{shot['index']:02d}.mp4"
    src = assets.get(shot["index"] + 1)  # manifest uses 1-based shot numbers
    if src and src.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"):
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},setsar=1,trim=start=0:end={dur},setpts=PTS-STARTPTS,"
                    f"fps={FPS}",
             "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)], "shot footage")
    else:
        still = tmp / f"still{shot['index']:02d}.png"
        if src:  # stock still image
            im = Image.open(src).convert("RGB")
            im = _cover(im, W * 2, H * 2)
            im.save(still)
        else:
            render_shot_still(shot["index"], seed, still)
        zin = shot["index"] % 2 == 0  # alternate push-in / pull-out
        z = (f"min(1+0.12*on/{frames},1.12)" if zin
             else f"max(1.12-0.12*on/{frames},1.0)")
        run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS),
             "-i", str(still),
             "-vf", f"scale={W * 2}:{H * 2},"
                    f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={W}x{H}:fps={FPS},setsar=1",
             "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)], "ken burns")
    return out


def _cover(im, w, h):
    s = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * s) + 1, int(im.height * s) + 1), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def card_overlay_filters(inp, vlabel, card_png, rs, re_):
    """Filter lines + input args for the one-rule card overlay.

    Barka ghost-fix (2026-10-18): the card stream is only cd=re_-rs+0.4s
    long, but the overlay enable window sits at rs..rs+cd on the MAIN
    timeline. Without setpts the stream EOFs at graph t=cd and overlay's
    default eof_action=repeat freezes the LAST card frame — which sits
    inside the fade-out ramp — so the whole card ghosted at ~25% opacity.
    setpts shifts the stream onto the enable window so overlay's frame-sync
    finds live frames; every card-local timing (fade in/out st, pop-in
    scale) is absolute graph time, with the scale ramp clamped to >=0.
    Returns (input_args, filter_lines, new_vlabel)."""
    cd = re_ - rs + 0.4
    args = ["-loop", "1", "-t", f"{cd:.3f}", "-i", str(card_png)]
    fc = [f"[{inp}:v]format=rgba,setpts=PTS+{rs:.3f}/TB,"
          f"fade=t=in:st={rs:.3f}:d=0.2:alpha=1,"
          f"fade=t=out:st={rs + cd - 0.15:.3f}:d=0.15:alpha=1,"
          f"scale=w='iw*(0.94+0.06*min(max(0,(t-{rs:.3f}))/0.2,1))':h=-2:eval=frame[card]",
          f"{vlabel}[card]overlay=x='(W-w)/2':y='(H-h)/2':"
          f"enable='between(t,{rs:.3f},{re_ + 0.4:.3f})'[vcard]"]
    return args, fc, "[vcard]"


def render_lesson(lesson_dir, audio, words_path, meta, assets_dir, out_path, shot_seed):
    doc = json.loads(Path(words_path).read_text(encoding="utf-8"))
    words = doc["words"]
    turns = doc.get("turns") or _turns_from_speakers(words)
    if not words:
        die(f"no words in {words_path}")
    duration = probe_seconds(audio)
    rule_turn = (meta.get("one_rule") or {}).get("turn")
    rule_beat = None
    if isinstance(rule_turn, int):
        if not (0 <= rule_turn < len(turns)):
            die(f"meta one_rule.turn={rule_turn} out of range (lesson has {len(turns)} turns)")
        rule_beat = (turns[rule_turn]["start"], turns[rule_turn]["end"])
    shots = plan_shots(turns, duration, rule_turn)
    if not shutil.which("ffmpeg"):
        die("ffmpeg not found")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        assets = load_assets_manifest(assets_dir)
        if not assets:
            note(f"no stock footage in {assets_dir} -- using Ken Burns fallback on stills")
        (tmp / "shots.json").write_text(json.dumps(shots, indent=1))
        segs = [render_shot_segment(s, assets, shot_seed, tmp) for s in shots]
        (tmp / "list.txt").write_text("".join(f"file '{p}'\n" for p in segs))
        base = tmp / "base.mp4"
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(tmp / "list.txt"), "-c", "copy", str(base)], "concat shots")

        groups = group_captions(words)
        ass = tmp / "captions.ass"
        ass.write_text(build_ass(groups, duration, rule_beat), encoding="utf-8")
        scrim = tmp / "scrim.png"
        render_scrim(scrim)
        # progress bar: pre-rendered PNG sequence (drawbox can't do per-frame
        # width on this ffmpeg build) -> image2 input index 2, overlay=0:0
        render_progress_bar(tmp, duration)
        bar_seq = tmp / "bar-%04d.png"

        # lower-thirds: one PNG per turn (skip the one-rule beat: the card owns it)
        lt_inputs, lt_filters, lt_idx = [], [], 0
        vlabel = "0:v"
        fc = [f"[{vlabel}][1:v]overlay=0:0:eof_action=pass[b0]"]
        vlabel = "[b0]"
        inp = 3
        for i, t in enumerate(turns):
            if rule_beat and t["start"] < rule_beat[1] and t["end"] > rule_beat[0]:
                continue
            sp = t["speaker"]
            color = PAL["host_a"] if sp == "A" else PAL["host_b"]
            png = tmp / f"lt{i:02d}.png"
            render_lower_third(speaker_name(meta, sp), f"HOST {sp}", color, png)
            lt_inputs += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(png)]
            s, e = t["start"], min(t["start"] + 2.5, duration)
            fc.append(f"[{inp}:v]format=rgba,fade=t=out:st={e - 0.2:.3f}:d=0.2:alpha=1[lt{lt_idx}]")
            fc.append(f"{vlabel}[lt{lt_idx}]overlay=90:1420:enable='between(t,{s:.3f},{e:.3f})'[b{lt_idx + 1}]")
            vlabel = f"[b{lt_idx + 1}]"
            lt_idx += 1
            inp += 1
        # one-rule card
        if rule_beat:
            rule = meta["one_rule"]
            card_png = tmp / "card.png"
            render_rule_card(rule["headline"], rule.get("sub", ""), card_png)
            rs, re_ = rule_beat
            # overlay construction lives in card_overlay_filters (Barka
            # ghost-fix 2026-10-18: setpts aligns the cd-long stream with the
            # absolute-time enable window; see that function's docstring)
            args, cfc, vlabel = card_overlay_filters(inp, vlabel, card_png, rs, re_)
            lt_inputs += args
            fc.extend(cfc)
            inp += 1
        fc.append(f"{vlabel}ass='{ass}'[vass]")
        fc.append(f"[vass][2:v]overlay=0:0:eof_action=pass,format=yuv420p[vout]")
        cmd = (["ffmpeg", "-y", "-loglevel", "error",
                "-i", str(base),
                "-loop", "1", "-t", f"{duration:.3f}", "-i", str(scrim),
                "-framerate", str(BAR_FPS), "-loop", "1", "-t", f"{duration:.3f}",
                "-i", str(bar_seq)]
               + lt_inputs + ["-i", str(audio),
                              "-filter_complex", ";".join(fc),
                              "-map", "[vout]", "-map", f"{inp}:a",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                              "-r", str(FPS), "-c:a", "aac", "-b:a", "128k",
                              "-shortest", "-movflags", "+faststart", str(out_path)])
        run(cmd, "lesson assembly")
    secs = probe_seconds(out_path)
    print(f"wrote {out_path} ({W}x{H}, {int(secs // 60)}:{int(secs % 60):02d}, "
          f"{len(shots)} shots, captions on)")


def _turns_from_speakers(words):
    # Fallback when words.json has no turn ranges: maximal speaker runs.
    turns, cur = [], None
    for w in words:
        if cur is None or w["speaker"] != cur["speaker"]:
            if cur:
                turns.append(cur)
            cur = {"speaker": w["speaker"], "text": "", "start": w["start"], "end": w["end"]}
        cur["end"] = w["end"]
    if cur:
        turns.append(cur)
    return turns


def render_compilation(ws, assets_dir, out_path):
    lessons = sorted(p for p in (ws / "class").glob("[0-9][0-9]-*") if p.is_dir())
    vids = [d / "lesson.mp4" for d in lessons if (d / "lesson.mp4").is_file()]
    if not vids:
        die(f"no lesson.mp4 files under {ws}/class/ -- render lessons first",
            f"python3 scripts/render_video.py --lesson {ws}/class/01-your-lesson")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        inputs, fc = [], []
        for i, v in enumerate(vids):
            inputs += ["-i", str(v)]
            fc.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=increase,"
                      f"crop=1920:1080,boxblur=20:1,eq=brightness=-0.45,setsar=1[bg{i}]")
            fc.append(f"[{i}:v]scale=608:1080,setsar=1[fg{i}]")
            fc.append(f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2[v{i}]")
            fc.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo[a{i}]")
        n = len(vids)
        seg_labels = []
        for i in range(n):
            seg_labels += [f"[v{i}]", f"[a{i}]"]
            if i < n - 1:
                sil_v = tmp / f"sil{i}.mp4"
                run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "color=c=black:s=1920x1080:r=30:d=0.5",
                     "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=0.5",
                     "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", str(sil_v)], "silence gap")
                si = len(inputs) // 2
                inputs += ["-i", str(sil_v)]
                seg_labels += [f"[{si}:v]", f"[{si}:a]"]
        fc.append("".join(seg_labels) + f"concat=n={2 * n - 1}:v=1:a=1[vout][aout]")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-y", "-loglevel", "error"] + inputs +
            ["-filter_complex", ";".join(fc),
             "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-r", str(FPS), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(out_path)], "compilation")
    secs = probe_seconds(out_path)
    print(f"wrote {out_path} (1920x1080, {int(secs // 60)}:{int(secs % 60):02d}, {n} lessons)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="render_video.py",
        description=__doc__.splitlines()[0],
        epilog="examples:\n"
               "  python3 render_video.py --lesson samples/demo/class/01-my-lesson\n"
               "  python3 render_video.py --lesson samples/demo/class/01-my-lesson --assets ~/stock\n"
               "  python3 render_video.py --workspace samples/demo --compile\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--lesson", help="lesson dir -> 1080x1920 Short")
    g.add_argument("--workspace", help="workspace root (use with --compile)")
    ap.add_argument("--compile", action="store_true",
                    help="with --workspace: render the 1920x1080 long-form compilation")
    ap.add_argument("--audio", help="lesson audio (default: <lesson>/lesson.mp3, else first *.mp3)")
    ap.add_argument("--words", help="word-timestamps JSON (default: <lesson>/words.json)")
    ap.add_argument("--out", help="output MP4 (default: <lesson>/lesson.mp4, compilation: <ws>/compilation.mp4)")
    ap.add_argument("--assets", help="stock-footage dir (default: skills/redemy/assets/)")
    ap.add_argument("--shot-seed", help="seed for procedural stills (default: stable hash of lesson name)")
    return ap.parse_args(argv)


def resolve_lesson_inputs(lesson, args):
    lesson = Path(lesson).expanduser().resolve()
    if not lesson.is_dir():
        die(f"lesson not found: {lesson}")
    audio = Path(args.audio).expanduser() if args.audio else lesson / "lesson.mp3"
    if not audio.is_file():
        cands = sorted(lesson.glob("*.mp3"))
        audio = cands[0] if cands else None
    if audio is None or not Path(audio).is_file():
        die(f"no audio in {lesson}",
            f"run: python3 scripts/render_audio.py --engine edge-tts {lesson}/script.txt {lesson}/lesson.mp3")
    words = Path(args.words).expanduser() if args.words else lesson / "words.json"
    if not words.is_file():
        die(f"words.json not found in {lesson} -- captions need word timestamps; "
            "only --engine edge-tts emits them",
            f"run: python3 scripts/render_audio.py --engine edge-tts {lesson}/script.txt {audio}")
    out = Path(args.out).expanduser() if args.out else lesson / "lesson.mp4"
    meta_path = lesson / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    if meta:
        problems = check_meta(meta)
        for p in problems:
            note(f"meta.json: {p}")
    else:
        note(f"no meta.json in {lesson} -- host names default to HOST A/B, no one-rule card")
    return lesson, Path(audio), words, out, meta


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    assets = Path(args.assets).expanduser().resolve() if args.assets else DEFAULT_ASSETS
    if args.lesson:
        lesson, audio, words, out, meta = resolve_lesson_inputs(args.lesson, args)
        seed = args.shot_seed or hashlib.sha256(lesson.name.encode()).hexdigest()[:16]
        out.parent.mkdir(parents=True, exist_ok=True)
        render_lesson(lesson, audio, words, meta, assets, out, seed)
    else:
        if not args.compile:
            die("--workspace needs --compile (per-lesson renders use --lesson)")
        ws = Path(args.workspace).expanduser().resolve()
        out = Path(args.out).expanduser() if args.out else ws / "compilation.mp4"
        render_compilation(ws, assets, out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("interrupted")
    except BrokenPipeError:
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:  # never a raw traceback for the user
        die(f"unexpected failure: {e}")
