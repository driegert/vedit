---
name: edit-video
description: Edit a video from written instructions — remove sections, speed up slow passages, and insert title-card or image slides. Use when asked to cut, trim, shorten, speed up, or add section titles to a recording (lecture, lab, screencast).
argument-hint: <video-file> [instructions]
allowed-tools: Bash, Read, Write
---

You are editing a video using `vedit`, which takes a **JSON edit spec** and applies it.
Never call `auto-editor` or `ffmpeg` directly — `vedit` exists because their raw flags are
easy to get subtly wrong, and a wrong flag silently produces a mangled video.

## The one rule that matters

**Every time you write refers to the ORIGINAL video.** Cuts never shift later timestamps.
If you cut `["0:00","1:30"]` and also cut `["10:00","10:30"]`, the second range still means
10:00 in the *original* file — do not subtract the first cut. This is the mistake to avoid.

## Step 1 — look at the video

```bash
vedit probe lecture.mp4
```

Gives duration, resolution, frame rate and whether it has audio. You need the duration
before you can write ranges. If the user's instructions reference a transcript, read that
too so your timestamps are grounded in something real.

## Step 2 — write the spec

Write a `.json` file. All three keys are optional; include only what was asked for.

```json
{
  "cuts":   [["0:00", "1:30"], ["14:05", "end"]],
  "speed":  [{"range": ["5:00", "8:00"], "factor": 2.0}],
  "slides": [{"at": "8:00", "text": "Part 2: Multitaper", "seconds": 3}]
}
```

| Key | Meaning |
|---|---|
| `cuts` | List of `[start, stop]` ranges to **remove**. |
| `speed` | List of `{"range": [start, stop], "factor": N}`. `N > 1` speeds up, `N < 1` slows down. Audio pitch is preserved. |
| `slides` | List of `{"at": time, ...}` cards **inserted** at that point. |

**Times** may be written as `"1:30"`, `"1:02:03"`, `90` (seconds), `"90s"`, `"start"` or
`"end"`. Ranges are half-open: `[start, stop)`.

**Slides** take exactly one of `text` or `image`:

```json
{"at": "8:00", "text": "Part 2: Multitaper\nThomson (1982)", "seconds": 3}
{"at": "8:00", "image": "/path/to/slide.png", "seconds": 4}
```

Optional on any slide: `seconds` (default 3), `background` (default `black`),
`color` (default `white`), `font_size` (defaults to a size proportional to the video).
Text may contain colons, quotes and `\n` — no escaping needed. Images of any size or
aspect ratio are letterboxed to fit, and transparency is flattened onto `background`.

`vedit example` prints a starter spec if you want one to edit.

## Step 3 — dry run, always

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run
```

This resolves every time, prints what will happen and estimates the final duration —
**without rendering anything**. Read the output back and check it against what was asked:

- Does each cut cover the range the user described?
- Is the estimated final duration plausible?
- Did a range you meant as `mm:ss` get read as seconds?

Show this plan to the user before rendering anything long.

## Step 4 — render

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4
```

Rendering re-encodes the video and takes roughly real time or better, so a 50-minute
lecture is minutes of work, not seconds. On success it prints the output path and the
actual duration. **Compare that duration to the dry-run estimate** — if they disagree,
something is wrong; do not report success.

The source file is never modified. Always write to a new path.

## Common mistakes

| Mistake | What happens |
|---|---|
| Compensating for earlier cuts | Later ranges land in the wrong place. Always use original timestamps. |
| `[stop, start]` (backwards range) | Rejected with an error — ranges must go forwards. |
| Using `speed` to remove something | Use `cuts`. A speed factor of 99999 is not a cut. |
| Giving a slide both `text` and `image` | Rejected. Pick one. |
| Overlapping `speed` ranges | Rejected. Merge them into one entry. |
| A `cut` and a `speed` over the same footage | The cut wins — the footage is removed, not sped up. |
| Guessing the duration | Run `vedit probe` first. A `stop` past the end is clamped, but a wrong `start` is not caught. |

If `vedit` prints an error, it names the exact key and what it expected — read it and fix
the spec rather than guessing at different syntax.

## Worked example

> "Drop the first 90 seconds of dead air, double-speed the long silent worked example
> from 5 to 8 minutes, put a title card before it, and cut everything after 14:05."

```bash
vedit probe lecture.mp4          # -> 15:20 duration, 1920x1080 @ 30fps
```

```json
{
  "cuts":   [["0:00", "1:30"], ["14:05", "end"]],
  "speed":  [{"range": ["5:00", "8:00"], "factor": 2.0}],
  "slides": [{"at": "5:00", "text": "Worked example", "seconds": 3}]
}
```

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run   # check the plan
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4             # render
```

Note the slide sits at `5:00` — the same original timestamp as the start of the speed
range — so it appears immediately before the sped-up passage.
