#!/usr/bin/env python3
"""Run the full redemy lesson pipeline end to end: check -> audio -> video -> upload pack.

One command per lesson, or a whole workspace:

    python3 make_video.py --workspace samples/demo
    python3 make_video.py --workspace samples/demo --lesson 01-your-walk-away
    python3 make_video.py --workspace samples/demo --dry-run   # show commands, run nothing

Each stage calls the matching script in this folder and the run stops at the
first stage that fails, naming it. $0 cost: the default engine is the free
edge-tts neural voices; no API keys anywhere.
"""

import argparse
import shutil
import subprocess
import sys

from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINES = ("edge-tts", "elevenlabs", "say")


def die(msg):
    print(f"make_video: error: {msg}", file=sys.stderr)
    sys.exit(1)


def lesson_dirs(ws):
    return sorted(p for p in (ws / "class").glob("[0-9][0-9]-*") if p.is_dir())


def resolve_lesson(ws, name):
    """Accept '01-name', a path, or a full path; return the lesson dir or die."""
    cand = Path(name)
    if cand.is_dir():
        return cand.resolve()
    cand = ws / "class" / name
    if cand.is_dir():
        return cand.resolve()
    avail = ", ".join(d.name for d in lesson_dirs(ws)) or "(none)"
    die(f"lesson not found: {name} (available in {ws}/class/: {avail})")


def run(cmd, stage, dry_run):
    label = " ".join(str(c) for c in cmd)
    if dry_run:
        print(f"[dry-run] {stage}: {label}")
        return
    print(f"--- [{stage}] $ {Path(cmd[1]).name} ...")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        die(f"stage '{stage}' failed (exit {rc}) -- fix the output above, then re-run")


def main():
    ap = argparse.ArgumentParser(
        prog="make_video.py",
        description=__doc__.splitlines()[0],
        epilog="examples:\n"
               "  python3 make_video.py --workspace samples/demo\n"
               "  python3 make_video.py --workspace samples/demo --lesson 01-your-walk-away\n"
               "  python3 make_video.py --workspace samples/demo --compile   # also the 1920x1080 long-form video\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--workspace", "-w", required=True,
                    help="workspace root containing class/ (and raw/ for the check stage)")
    ap.add_argument("--lesson", "-l",
                    help="one lesson: '01-my-lesson' or a path (default: every lesson in class/)")
    ap.add_argument("--engine", choices=ENGINES, default="edge-tts",
                    help="TTS engine (default: edge-tts -- free, no API key)")
    ap.add_argument("--voice-a", help="voice for host A (overrides REDEMY_VOICE_A)")
    ap.add_argument("--voice-b", help="voice for host B (overrides REDEMY_VOICE_B)")
    ap.add_argument("--assets", help="stock-footage directory for render_video.py")
    ap.add_argument("--compile", action="store_true",
                    help="also render the 1920x1080 compilation of all lessons")
    ap.add_argument("--skip-check", action="store_true",
                    help="skip the check_lessons.py guardrail (useful when iterating on video)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands each stage would run, without running them")
    args = ap.parse_args()

    py = sys.executable
    ws = Path(args.workspace).expanduser().resolve()
    if not ws.is_dir():
        die(f"workspace not found: {ws}")
    if not (ws / "class").is_dir():
        die(f"{ws} has no class/ folder -- nothing to build")

    lessons = [resolve_lesson(ws, args.lesson)] if args.lesson else lesson_dirs(ws)
    if not lessons:
        die(f"no lesson folders like '01-my-lesson' in {ws}/class/")

    for les in lessons:
        if not (les / "script.txt").is_file():
            die(f"{les.name} has no script.txt -- a lesson needs script.txt (and card.md for the check stage)")

    assets = [str(Path(args.assets).expanduser().resolve())] if args.assets else []

    n = 0
    total = len(lessons) * (4 if not args.skip_check else 3)
    for les in lessons:
        if not args.skip_check:
            n += 1
            run([py, str(HERE / "check_lessons.py"), str(ws), str(les)],
                f"check {n}/{total} ({les.name})", args.dry_run)
        n += 1
        audio_cmd = [py, str(HERE / "render_audio.py"), "--engine", args.engine,
                     str(les / "script.txt"), str(les / "lesson.mp3")]
        if args.voice_a:
            audio_cmd += ["--voice-a", args.voice_a]
        if args.voice_b:
            audio_cmd += ["--voice-b", args.voice_b]
        run(audio_cmd, f"audio {n}/{total} ({les.name})", args.dry_run)
        n += 1
        run([py, str(HERE / "render_video.py"), "--lesson", str(les)] + assets,
            f"video {n}/{total} ({les.name})", args.dry_run)

    n += 1
    run([py, str(HERE / "package_class.py"), str(ws)],
        f"upload-pack {n}/{total}", args.dry_run)

    if args.compile:
        n += 1
        run([py, str(HERE / "render_video.py"), "--workspace", str(ws), "--compile"] + assets,
            f"compilation {n}/{total}", args.dry_run)

    if args.dry_run:
        print(f"[dry-run] {total} stage(s) planned, nothing executed")
    else:
        print(f"done: {len(lessons)} lesson(s) -> {ws}/upload-pack/ (thumbnails + meta per lesson)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("interrupted")
    except BrokenPipeError:
        sys.exit(1)
    except Exception as e:  # never a raw traceback for the user
        die(f"unexpected failure: {e}")
