---
name: edit-video
description: Edit a video from written instructions — remove sections, speed up slow passages, label them with floating text, add chapters and a contents card, and fix quiet audio. Use when asked to cut, trim, shorten, speed up, caption, chapter, or clean up the sound of a recording (lecture, lab, screencast).
argument-hint: <video-file> [instructions]
allowed-tools: Bash, Read, Write
---

You are editing a video using `vedit`, which takes a **JSON edit spec** and applies it.
Never call `auto-editor` or `ffmpeg` directly — `vedit` exists because their raw flags are
easy to get subtly wrong, and a wrong flag silently produces a mangled video.

## The one rule that matters

**Every time you write refers to the ORIGINAL video.** Cuts never shift later timestamps.
If you cut `["0:00","1:30"]` and also cut `["10:00","10:30"]`, the second range still means
10:00 in the *original* file — do not subtract the first cut. The same is true of captions
and chapters: write them in original time, and `vedit` works out where they land.

## Step 1 — look at the video

```bash
vedit probe lecture.mp4
```

Gives duration, resolution, frame rate and whether it has audio. You need the duration
before you can write ranges.

## Step 2 — transcribe, if you need to know what is in it

Only needed when you are asked for chapters, or to find sections by content.

```bash
vedit transcribe lecture.mp4 -o lecture-transcript.txt
```

Roughly 30x faster than real time, so a 20-minute lecture takes well under a minute. The
output is `[m:ss] text` blocks **in original time**, which is the same clock the spec uses —
so a timestamp you read there can go straight into `chapters[]` with no conversion.

Read it, propose chapters, and **show them to the user before rendering**. Windows are two
minutes wide, so round the boundaries sensibly rather than quoting `[14:00]` verbatim.

## Step 3 — write the spec

Write a `.json` file. Every key is optional; include only what was asked for.

```json
{
  "cuts":     [["0:00", "1:30"], ["14:05", "end"]],
  "speed":    [{"range": ["5:00", "8:00"], "factor": 5, "label": "5x - waiting for download"}],
  "text":     [{"range": ["9:00", "9:20"], "text": "the menu moved in v4"}],
  "chapters": [{"at": "0:00", "title": "Introduction"},
               {"at": "8:00", "title": "Installing the toolchain"}],
  "toc_card": true,
  "audio":    {"normalize": "ebu", "target": -16}
}
```

| Key | Meaning |
|---|---|
| `cuts` | `[start, stop]` ranges to **remove**. |
| `speed` | `{"range": [start, stop], "factor": N}`. `N > 1` speeds up. Pitch is preserved. |
| `speed[].label` | Floating text over that sped-up stretch. **Prefer this to a slide** for marking speed-ups — it needs no timestamp of its own and does not lengthen the video. |
| `text` | Floating captions: `{"range": [start, stop], "text": "..."}`, optional `position` (`top`, `middle`, `bottom`; default `bottom`). |
| `slides` | Full-screen cards **inserted** at a point: `{"at": time, "text": "..."}` or `{"at": time, "image": "/path.png"}`. These add to the running time. |
| `chapters` | `{"at": time, "title": "..."}`. Produces real chapter markers plus a pasteable `0:00 Title` list. |
| `toc_card` | `true` for a contents card at the start. Needs `chapters`. |
| `audio` | `{"normalize": "ebu"}` for loudness, or `"peak"`. See below. |

**Times**: `"1:30"`, `"1:02:03"`, `90` (seconds), `"90s"`, `"start"`, `"end"`. Ranges are
half-open, `[start, stop)`.

**Styling** (all optional): captions and slides take `color`, `background`, `font_size`;
slides also take `seconds` (default 3). Text may contain colons, quotes and `\n` — no
escaping needed.

`vedit example` prints a starter spec.

## Quiet audio

Screen recordings are very often far too quiet — quiet enough that the viewer has to turn
everything up. Check for it and say so:

```bash
ffmpeg -hide_banner -i lecture.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json \
  -f null - 2>&1 | grep input_i
```

Broadcast speech sits near **−16 LUFS**. If `input_i` is well below that (−25, −38), add
`"audio": {"normalize": "ebu", "target": -16}` and mention it to the user. `vedit` measures
and corrects in one step; you do not need to run a second pass yourself.

## Step 4 — dry run, always

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run
```

Resolves every time, prints what will happen and estimates the final duration **without
rendering**. Read it back and check it against what was asked. Show this plan to the user
before rendering anything long.

## Step 5 — render

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4
```

Prints the output path, the actual duration, and the pasteable chapter list. **Compare the
duration to the dry-run estimate** — if they disagree, something is wrong; do not report
success. The source file is never modified; always write to a new path.

## Common mistakes

| Mistake | What happens |
|---|---|
| Compensating for earlier cuts | Later ranges land in the wrong place. Always use original timestamps. |
| A slide for every speed-up | Works, but pads the video and interrupts it. Use `speed[].label` instead. |
| Captioning footage you also cut | Rejected — the caption could never be seen. |
| Two chapters inside one cut range | Rejected — they would collapse onto the same moment. |
| `toc_card` with no `chapters` | Rejected. The card lists the chapters. |
| Using `speed` to remove something | Use `cuts`. A factor of 99999 is not a cut. |
| Overlapping `speed` ranges | Rejected. Merge them into one entry. |
| A `cut` and a `speed` over the same footage | The cut wins — that footage is removed, not sped up. |
| Guessing the duration | Run `vedit probe` first. |

If `vedit` prints an error, it names the exact key and what it expected — read it and fix
the spec rather than guessing at different syntax.

## Worked example

> "Drop the first 90 seconds of dead air, speed up the two download waits, mark them so
> people know, chapter it, and the sound is way too quiet."

```bash
vedit probe lecture.mp4                       # -> 22:20, 1920x1080 @ 30fps
vedit transcribe lecture.mp4 -o t.txt         # -> read it, propose chapters
```

```json
{
  "cuts":  [["0:00", "1:30"]],
  "speed": [{"range": ["4:20", "6:10"], "factor": 5, "label": "5x - waiting for download"},
            {"range": ["12:00", "13:30"], "factor": 5, "label": "5x - installing"}],
  "chapters": [{"at": "1:30", "title": "Downloading R"},
               {"at": "6:10", "title": "Installing RStudio"},
               {"at": "13:30", "title": "First Quarto document"}],
  "toc_card": true,
  "audio": {"normalize": "ebu", "target": -16}
}
```

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run   # check
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4             # render
```
