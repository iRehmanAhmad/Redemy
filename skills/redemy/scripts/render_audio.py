"""Render a two-host lesson script to one MP3, plus word timestamps for captions.

Script format, one turn per line:
    A: So here's the thing about pricing...
    B: Right, and that's where most people...

Engines (--engine, default edge-tts):
- edge-tts: free neural voices, no API key, Windows/Linux/Mac. Emits
  words.json (word-level timestamps) next to the MP3 — render_video.py
  captions depend on this. Voices default to en-US-AriaNeural (A) and
  en-US-GuyNeural (B); override with --voice-a/--voice-b or
  REDEMY_VOICE_A / REDEMY_VOICE_B.
- elevenlabs: ElevenLabs Text-to-Dialogue when ELEVENLABS_API_KEY is set
  (paid). One call per batch, not isolated lines glued with silence.
- say: macOS `say` (free, offline, plainer).

Only edge-tts emits word timestamps. The elevenlabs/say paths print a note
explaining that captions need an edge-tts render.

Both paths finish with the same warm EQ and loudness pass in ffmpeg.

Usage: python3 render_audio.py [--engine {edge-tts,elevenlabs,say}]
           [--voice-a V] [--voice-b V] [--words WORDS] [--out OUT] script
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

POLISH_AF = (
    "highpass=f=90,equalizer=f=200:t=q:w=1:g=-2,equalizer=f=3000:t=q:w=1:g=2.5,"
    "deesser,lowpass=f=13500,acompressor=ratio=2.5,aecho=0.8:0.5:40:0.08,loudnorm=I=-16"
)
BATCH_CHARS = 3000
EDGE_DEFAULTS = {"A": "en-US-AriaNeural", "B": "en-US-GuyNeural"}
WORD_TS_VERSION = 1


def die(msg, hint=None):
    print(f"render_audio: error: {msg}", file=sys.stderr)
    if hint:
        print(f"render_audio: hint: {hint}", file=sys.stderr)
    sys.exit(1)


def parse_turns(text):
    turns = []
    for line in text.splitlines():
        speaker, sep, said = line.partition(":")
        if sep and speaker.strip() in ("A", "B") and said.strip():
            turns.append((speaker.strip(), said.strip()))
    if not turns:
        raise ValueError("no 'A:' or 'B:' lines found in the script")
    return turns


def strip_tags(said):
    """Audio tags like [laughs] are stage directions; no engine should read them."""
    return re.sub(r"\[[^\]]+\]", "", said).strip()


def ticks_to_seconds(ticks):
    """edge-tts word boundaries arrive in 100ns ticks."""
    return ticks / 10_000_000


def collect_boundaries(chunks, speaker, offset_ticks=0):
    """Pull (text, start_ticks, end_ticks, speaker) word events out of an
    edge-tts stream. Times stay in 100ns ticks here; write_word_timestamps
    converts to seconds."""
    events = []
    for ch in chunks:
        if isinstance(ch, dict) and ch.get("type") == "WordBoundary":
            events.append((ch["text"], offset_ticks + ch["offset"],
                           offset_ticks + ch["offset"] + ch["duration"], speaker))
    return events


def write_word_timestamps(path, events, turns, engine="edge-tts"):
    """Write the caption pipeline's contract: words.json.

    events: [(text, start_ticks, end_ticks, speaker), ...] (100ns ticks)
    turns:  per-turn audio ranges as (speaker, start_s, end_s) tuples or
            {"speaker", "start", "end"} dicts
    """
    def _turn(t):
        if isinstance(t, dict):
            return t["speaker"], t["start"], t["end"]
        return t[0], t[1], t[2]

    doc = {
        "version": WORD_TS_VERSION,
        "engine": engine,
        "words": [
            {"text": t, "start": round(ticks_to_seconds(s), 3),
             "end": round(ticks_to_seconds(e), 3), "speaker": sp}
            for t, s, e, sp in events
        ],
        "turns": [
            {"speaker": sp, "start": round(s, 3), "end": round(e, 3)}
            for sp, s, e in (_turn(t) for t in turns)
        ],
    }
    Path(path).write_text(json.dumps(doc, indent=1), encoding="utf-8")


async def _edge_turn(voice, text):
    import edge_tts  # local import: only needed for this engine

    audio = bytearray()
    events = []
    # boundary="WordBoundary" is what makes the server emit per-word timings;
    # the default SentenceBoundary would leave the caption pipeline with nothing.
    communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            events.append(chunk)
    return bytes(audio), events


async def _edge_all(turns, voices):
    out = []
    for who, said in turns:
        text = strip_tags(said)
        if not text:
            continue
        audio, events = await _edge_turn(voices[who], text)
        out.append((who, audio, events))
    return out


_PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
               "all_proxy", "ALL_PROXY")


def _without_proxy_env():
    """Context manager: drop proxy env vars.

    The sandbox egress proxy hangs on the wss:// upgrade to
    speech.platform.bing.com (plain HTTPS through it is fine); a direct TLS
    connection works, so edge-tts must bypass the proxy.
    """
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        saved = {k: os.environ.pop(k) for k in _PROXY_VARS if k in os.environ}
        try:
            yield
        finally:
            os.environ.update(saved)

    return _cm()


def edge_tts_parts(turns, tmp, voices):
    """Synthesize each turn with edge-tts; return (parts, events, turn_ranges).

    events: [(text, start_s, end_s, speaker)] with absolute lesson times.
    turn_ranges: [(speaker, start_s, end_s)] per synthesized turn.
    """
    with _without_proxy_env():
        results = asyncio.run(_edge_all(turns, voices))
    parts, events, turn_ranges = [], [], []
    offset_ticks = 0
    for i, (who, audio, chunks) in enumerate(results):
        part = tmp / f"turn{i:03d}.mp3"
        part.write_bytes(audio)
        parts.append(part)
        events.extend(collect_boundaries(chunks, who, offset_ticks))
        dur = probe_seconds(part)
        turn_ranges.append((who, offset_ticks / 10_000_000,
                            offset_ticks / 10_000_000 + dur))
        offset_ticks += int(dur * 10_000_000)
    return parts, events, turn_ranges


def probe_seconds(path):
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip())


def elevenlabs_parts(turns, tmp, key):
    voices = {"A": os.environ.get("REDEMY_VOICE_A", "TX3LPaxmHKxFdv7VOQHJ"),
              "B": os.environ.get("REDEMY_VOICE_B", "cgSgspJ2msm6clMCkdW9")}
    stability = float(os.environ.get("REDEMY_STABILITY", "0.3"))
    parts = []
    for i, batch in enumerate(batches(turns)):
        body = {"inputs": [{"text": strip_tags(said), "voice_id": voices[who]} for who, said in batch],
                "model_id": "eleven_v3", "settings": {"stability": stability}, "seed": 42}
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-dialogue", data=json.dumps(body).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
        part = tmp / f"part{i}.mp3"
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                part.write_bytes(r.read())
        except urllib.error.HTTPError as e:
            # "HTTP Error 401" alone hides the cause (bad key, quota, unknown voice id);
            # the API says which in the response body. The key is never printed.
            detail = e.read().decode("utf-8", "replace").strip()[:300]
            sys.exit(f"ElevenLabs refused batch {i + 1}: HTTP {e.code} {e.reason}. {detail}")
        parts.append(part)
    return parts


def batches(turns):
    batch, size = [], 0
    for t in turns:
        if batch and size + len(t[1]) > BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t[1])
    yield batch


def say_parts(turns, tmp):
    voices = {"A": os.environ.get("REDEMY_SAY_A", "Samantha"),
              "B": os.environ.get("REDEMY_SAY_B", "Daniel")}
    parts = []
    for i, (who, said) in enumerate(turns):
        part = tmp / f"turn{i:03d}.aiff"
        subprocess.run(["say", "-v", voices[who], "-o", str(part), strip_tags(said)], check=True)
        parts.append(part)
    return parts


def stitch(parts, tmp, out_path):
    listing = tmp / "list.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-af", POLISH_AF, "-ar", "44100", "-b:a", "128k", str(out_path)], check=True)


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="render_audio.py",
        description=__doc__.splitlines()[0],
        epilog="examples:\n"
               "  python3 render_audio.py --engine edge-tts class/01-x/script.txt\n"
               "  python3 render_audio.py --engine edge-tts --voice-a en-US-JennyNeural \\\n"
               "      class/01-x/script.txt class/01-x/lesson.mp3\n"
               "  python3 render_audio.py --engine elevenlabs class/01-x/script.txt\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", help="lesson dialogue script ('A: ...' / 'B: ...' lines)")
    ap.add_argument("out", nargs="?", default=None,
                    help="output MP3 (default: <script-dir>/lesson.mp3)")
    ap.add_argument("--engine", choices=("edge-tts", "elevenlabs", "say"), default="edge-tts",
                    help="TTS engine (default: edge-tts -- free, no API key)")
    ap.add_argument("--voice-a", help="voice for host A (overrides REDEMY_VOICE_A)")
    ap.add_argument("--voice-b", help="voice for host B (overrides REDEMY_VOICE_B)")
    ap.add_argument("--words", default=None,
                    help="word-timestamps JSON path (default: <out-dir>/words.json; edge-tts only)")
    return ap.parse_args(argv)


def resolve_paths(args):
    script = Path(args.script).expanduser()
    out = Path(args.out).expanduser() if args.out else script.parent / "lesson.mp3"
    words = Path(args.words).expanduser() if args.words else out.parent / "words.json"
    return script, out, words


def edge_voices(args):
    return {
        "A": args.voice_a or os.environ.get("REDEMY_VOICE_A", EDGE_DEFAULTS["A"]),
        "B": args.voice_b or os.environ.get("REDEMY_VOICE_B", EDGE_DEFAULTS["B"]),
    }


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if not shutil.which("ffmpeg"):
        die("ffmpeg not found", "install it: apt install ffmpeg  (or: brew install ffmpeg)")
    script_path, out_path, words_path = resolve_paths(args)
    if not script_path.is_file():
        die(f"script not found: {script_path}",
            f"point it at a lesson script, e.g. python3 render_audio.py --engine edge-tts {script_path}")
    try:
        turns = parse_turns(script_path.read_text(encoding="utf-8"))
    except ValueError as e:
        die(f"{e} in {script_path} (expected lines like 'A: ...')")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    engine = args.engine
    if engine == "edge-tts":
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            die("the edge-tts package is not installed (pip install edge-tts) -- it is the free neural engine")
    elif engine == "elevenlabs":
        if not os.environ.get("ELEVENLABS_API_KEY"):
            die("ELEVENLABS_API_KEY is not set (paid) -- use --engine edge-tts for the free default")
    elif engine == "say" and not shutil.which("say"):
        die("'say' not found (macOS only) -- use --engine edge-tts")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if engine == "edge-tts":
            parts, events, turn_ranges = edge_tts_parts(turns, tmp, edge_voices(args))
            write_word_timestamps(words_path, events, turn_ranges, engine="edge-tts")
            words_note = f", words.json ({len(events)} words)"
        else:
            parts = (elevenlabs_parts(turns, tmp, os.environ["ELEVENLABS_API_KEY"])
                     if engine == "elevenlabs" else say_parts(turns, tmp))
            words_note = ""
            print(f"render_audio: note: --engine {engine} provides no word timestamps; "
                  f"render_video.py captions need words.json -- re-render with --engine edge-tts",
                  file=sys.stderr)
        stitch(parts, tmp, out_path)
    length = stamp_card(script_path.with_name("card.md"), out_path)
    print(f"wrote {out_path} ({length}) from {len(turns)} turns via {engine}{words_note}")


def stamp_card(card, mp3):
    """Write the real running time onto the card's Listen line. A hand-typed
    "(2 min)" sat on a 2:32 lesson in the first full test run."""
    secs = probe_seconds(mp3)
    length = f"{int(secs // 60)}:{int(secs % 60):02d}"
    if card.exists():
        text = card.read_text(encoding="utf-8")
        card.write_text(re.sub(r"^(\*\*Listen:\*\*.*?)\s*\([^)]*\)\s*$", rf"\1 ({length})", text, count=1, flags=re.M),
                        encoding="utf-8")
    return length


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
