# assets/ — stock footage for render_video.py

This is the **default `--assets` directory** (it lives beside `scripts/`;
override per run with `--assets <dir>`).

## How it works

`manifest.json` maps 1-based shot numbers to files:

```json
{
  "version": 1,
  "shots": [
    {"shot": 1, "file": "hook-gpu.mp4",
     "source": "Pexels video 3121459 by Jane Doe — https://www.pexels.com/video/...",
     "license": "Pexels License (free for commercial use, no attribution required)"}
  ]
}
```

- `shot` = the shot number from the lesson's shot plan (1 = hook shot).
- `file` = a video (`.mp4`/`.mov`/`.mkv`/`.webm`) or still (`.jpg`/`.png`) in this folder.
- Videos play as-is (scaled to cover 1080x1920); stills get a Ken Burns push/pull.

**Missing footage is not an error.** A shot with no manifest entry renders a
procedural dark-abstract still with Ken Burns motion — the video is never a
blank frame and never text-only.

## Where to get free footage ($0)

- Pexels Videos — pexels.com/videos (Pexels License)
- Pixabay — pixabay.com/videos (Pixabay Content License, no API key needed for manual download)
- Coverr — coverr.co (free, no attribution)
- Mixkit — mixkit.co (free license)

Download the files yourself (browser), drop them here, add the manifest entry
with the source URL and license. Keep the attribution the license asks for —
that is what the `source`/`license` fields are for.
