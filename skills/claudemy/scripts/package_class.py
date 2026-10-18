"""Bundle a finished class for delivery: audio zips under a size cap, plus all cards in one file.

Chat and file-sharing tools cap uploads (one we hit was 30 MB), so the audio is
split into numbered zips that each stay under --max-mb. Cards are combined in
lesson order into cards.md, and into cards.docx when pandoc is installed.

Also writes upload-pack/ (default on): per lesson, a 1280x720 thumbnail.png
(PIL, design-spec §10) and a <NN-name>.md with title/description/tags/hashtags
for the YouTube upload. Pass --no-upload-pack for the old behavior.

Usage: python3 package_class.py [--max-mb 25] [--no-upload-pack] <workspace>
Writes to <workspace>/deliver/ and <workspace>/upload-pack/.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = "/usr/share/fonts/truetype/noto"
THUMB_W, THUMB_H = 1280, 720


def die(msg, hint=None):
    print(f"package_class: error: {msg}", file=sys.stderr)
    if hint:
        print(f"package_class: hint: {hint}", file=sys.stderr)
    sys.exit(1)


def lesson_dirs(ws):
    return sorted(p for p in (ws / "class").glob("[0-9][0-9]-*") if p.is_dir())


def zip_audio(ws, out, max_bytes):
    mp3s, names = [], {}
    for d in lesson_dirs(ws):
        found = sorted(d.glob("*.mp3"))
        if len(found) > 1:
            # Two renders in one folder used to share one member name; whichever
            # extracted last won, silently. Now each keeps its own name, and the run says so.
            print(f"{d.name}: {len(found)} mp3 files, each packed under its own name: "
                  + ", ".join(f.name for f in found), file=sys.stderr)
        for m in found:
            names[m] = f"{d.name}.mp3" if len(found) == 1 else f"{d.name}-{m.stem}.mp3"
        mp3s += found
    groups, current, size = [], [], 0
    for m in mp3s:
        if current and size + m.stat().st_size > max_bytes:
            groups.append(current)
            current, size = [], 0
        current.append(m)
        size += m.stat().st_size
    if current:
        groups.append(current)
    made = []
    for group in groups:
        first, last = group[0].parent.name[:2], group[-1].parent.name[:2]
        path = out / f"audio-lessons-{first}-to-{last}.zip"
        with zipfile.ZipFile(path, "w") as z:
            for m in group:
                z.write(m, names[m])
        made.append(path)
    return made


def combine_cards(ws, out):
    cards = [d / "card.md" for d in lesson_dirs(ws) if (d / "card.md").exists()]
    md = out / "cards.md"
    md.write_text("\n\n---\n\n".join(c.read_text() for c in cards))
    if shutil.which("pandoc"):
        subprocess.run(["pandoc", str(md), "-f", "markdown", "-t", "docx", "-o", str(out / "cards.docx")], check=True)
    return len(cards)


def main(ws, max_mb, upload_pack=True):
    out = ws / "deliver"
    out.mkdir(exist_ok=True)
    zips = zip_audio(ws, out, max_mb * 1024 * 1024)
    n = combine_cards(ws, out)
    for z in zips:
        # Same unit as the cap (MiB). Printing decimal MB showed 26.0 against a 25 cap for a zip under it.
        print(f"{z}  {z.stat().st_size / (1024 * 1024):.1f} MB (cap {max_mb} MB)")
    print(f"{n} cards combined in {out / 'cards.md'}" + (" and cards.docx" if shutil.which("pandoc") else ""))
    if upload_pack:
        made = write_upload_pack(ws, ws / "upload-pack")
        print(f"upload-pack: {len(made)} lesson(s) -> {ws / 'upload-pack'}")


# --------------------------------------------------------------------------
# upload-pack/: thumbnail.png + <NN-name>.md per lesson (design-spec §10)
# --------------------------------------------------------------------------

def _font(name, size):
    return ImageFont.truetype(f"{FONT_DIR}/{name}", size)


def _tracked(draw, xy, text, font, fill, tracking):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lesson_video(lesson_dir):
    v = lesson_dir / "lesson.mp4"
    return v if v.is_file() else None


def extract_bg_frame(video, tmp_path):
    """Darkest-B-roll-frame approximation: frame at t=1s (the hook shot)."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1",
                    "-i", str(video), "-frames:v", "1", str(tmp_path)], check=True)
    return Image.open(tmp_path).convert("RGB")


def render_thumbnail(lesson_dir, meta, bg=None):
    """1280x720 YouTube thumbnail per design-spec §10."""
    lesson_no = re.match(r"(\d+)", lesson_dir.name)
    num = lesson_no.group(1) if lesson_no else "1"
    course = str(meta.get("course", "")).upper()
    headline = meta.get("headline") or [str(meta.get("title", "")).upper()]
    hosts = meta.get("hosts") or {}

    if bg is None:
        bg = Image.new("RGB", (THUMB_W, THUMB_H), (10, 10, 15))
    img = bg.resize((THUMB_W, THUMB_H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(8))
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, THUMB_W, THUMB_H], fill=(10, 10, 15, 140))  # black overlay 55%
    # diagonal gradient ink -> transparent, bottom-left
    grad = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(THUMB_H):
        a = int(150 * y / THUMB_H)
        gd.line([(0, y), (THUMB_W, y)], fill=(10, 10, 15, a))
    img = Image.alpha_composite(img.convert("RGBA"), grad)
    d = ImageDraw.Draw(img)

    amber = _hex("#FFD60A")
    # eyebrow: LESSON 1 · COURSE (trimmed to clear the badge at x=1120)
    brow_f = _font("NotoSans-Bold.ttf", 64)
    brow_words = f"LESSON {int(num)} · {course}".split()
    brow = ""
    for wd in brow_words:
        trial = (brow + " " + wd).strip()
        if d.textlength(trial, font=brow_f) + 18 * len(trial) > 1000:
            break
        brow = trial
    _tracked(d, (80, 90), brow, brow_f, amber, 18)
    # headline: <=3 words/line, <=2 lines
    words, lines, cur = str(headline[0]).split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if len(trial.split()) <= 3:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    lines = (lines + (headline[1:2] or []))[:2]
    hl = _font("NotoSans-Black.ttf", 150)
    y = 190
    last_w = 0
    for ln in lines:
        d.text((80, y), ln, font=hl, fill="white")
        last_w = d.textlength(ln, font=hl)
        y += 165
    d.rectangle([80, y - 18, 80 + last_w, y - 6], fill=amber)  # amber underline bar
    # lesson badge: 120x120 gradient square, top-right
    bx, by, bs = 1120, 80, 120
    c0, c1 = _hex("#00E5FF"), _hex("#FF3D81")
    for yy in range(bs):
        t = yy / bs
        col = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
        d.line([(bx, by + yy), (bx + bs, by + yy)], fill=col)
    badge = _font("NotoSans-Black.ttf", 64)
    d.text((bx + bs / 2, by + bs / 2), str(int(num)), font=badge, fill="white", anchor="mm")
    # host strip: two small pills
    hx = 80
    for sp, key in (("A", "#00E5FF"), ("B", "#FF3D81")):
        name = hosts.get(sp)
        if not name:
            continue
        f = _font("NotoSans-Bold.ttf", 36)
        tw = d.textlength(name, font=f)
        pw = int(tw + 56)
        d.rounded_rectangle([hx, 600, hx + pw, 648], radius=24, fill=(12, 12, 18, 205))
        d.rounded_rectangle([hx + 10, 610, hx + 16, 638], radius=3, fill=_hex(key))
        d.text((hx + 32, 624), name, font=f, fill=_hex(key), anchor="lm")
        hx += pw + 20
    return img.convert("RGB")


def write_meta_md(lesson_dir, meta):
    title = meta.get("title", lesson_dir.name)
    desc = meta.get("description", "")
    tags = meta.get("tags", [])
    hashtags = meta.get("hashtags", [])
    lines = [f"# {title}", "", desc, "", "## Tags"]
    lines += [f"- {t}" for t in tags] or ["- (none)"]
    lines += ["", "## Hashtags", " ".join(hashtags) or "(none)", ""]
    return "\n".join(lines)


def write_upload_pack(ws, out):
    """Write upload-pack/<NN-name>/{thumbnail.png,<NN-name>.md} per lesson
    that has a lesson.mp4 + meta.json. Returns the lesson dirs packed."""
    made = []
    for d in lesson_dirs(ws):
        meta_path = d / "meta.json"
        if not meta_path.is_file():
            print(f"package_class: note: {d.name} has no meta.json -- skipped in upload-pack",
                  file=sys.stderr)
            continue
        video = lesson_video(d)
        if video is None:
            print(f"package_class: note: {d.name} has no lesson.mp4 -- "
                  "thumbnail rendered from card text only", file=sys.stderr)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dest = out / d.name
        dest.mkdir(parents=True, exist_ok=True)
        bg = None
        if video is not None:
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tf:
                    bg = extract_bg_frame(video, tf.name)
            except Exception:
                bg = None  # thumbnail falls back to a dark gradient
        render_thumbnail(d, meta, bg).save(dest / "thumbnail.png")
        (dest / f"{d.name}.md").write_text(write_meta_md(d, meta), encoding="utf-8")
        made.append(d)
    return made


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="package_class.py",
        description=__doc__.splitlines()[0],
        epilog="examples:\n"
               "  python3 package_class.py samples/demo\n"
               "  python3 package_class.py --max-mb 25 --no-upload-pack samples/demo\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspace", help="workspace root containing class/")
    ap.add_argument("--max-mb", type=int, default=25, help="audio zip size cap (default: 25)")
    ap.add_argument("--no-upload-pack", action="store_true",
                    help="skip upload-pack/ (thumbnails + per-video meta)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    try:
        args = parse_args(sys.argv[1:])
        ws = Path(args.workspace).expanduser().resolve()
        if not any(lesson_dirs(ws)):
            die(f"no lesson folders like '01-name' in {ws}/class/")
        main(ws, args.max_mb, upload_pack=not args.no_upload_pack)
    except KeyboardInterrupt:
        die("interrupted")
    except BrokenPipeError:
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:  # never a raw traceback for the user
        die(f"unexpected failure: {e}")
