---
name: edit-video
description: Edit a video — remove sections, speed up slow passages, label them with floating text, add chapters and a contents card, fix quiet audio, and write a companion guide (with screenshots) or summary from the transcript. Given a video with no or vague instructions, it surveys the recording and offers a menu of edits. Use when asked to cut, trim, shorten, speed up, caption, chapter, or clean up the sound of a recording (lecture, lab, screencast).
argument-hint: <video-file> [instructions]
allowed-tools: Bash, Read, Write
---

You are editing a video using `vedit`, which takes a **JSON edit spec** and applies it.
Never call `auto-editor` or `ffmpeg` to *produce* a video — `vedit` exists because their
raw flags are easy to get subtly wrong, and a wrong flag silently produces a mangled video.
Read-only `ffmpeg` analysis (the survey below) is fine and expected.

## The one rule that matters

**Every time you write refers to the ORIGINAL video.** Cuts never shift later timestamps.
If you cut `["0:00","1:30"]` and also cut `["10:00","10:30"]`, the second range still means
10:00 in the *original* file — do not subtract the first cut. The same is true of captions
and chapters: write them in original time, and `vedit` works out where they land.

## Step 1 — survey the video

Three cheap read-only checks, always, before proposing anything — in **one** command,
and start the transcript in the background at the same time, since chapters and a written
companion both need it and it is the slow one (`-vn` keeps the two ffmpeg passes to
audio only — about 2 s and 20 s for a 20-minute recording):

```bash
nohup vedit transcribe lecture.mp4 -o lecture-transcript.txt >/dev/null 2>&1 &
vedit probe lecture.mp4                                    # duration, fps, resolution, audio
ffmpeg -hide_banner -vn -i lecture.mp4 -af silencedetect=noise=-30dB:d=3 -f null - 2>&1 \
  | grep -E 'silence_(start|end)'                          # dead air (waits, downloads)
ffmpeg -hide_banner -vn -i lecture.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json \
  -f null - 2>&1 | grep input_i                            # loudness, see "Quiet audio"
```

You need the duration before you can write ranges. Silences under ~10 s are ordinary
speech pauses; report the long ones. To learn what a long silence *is* (a download bar? a
window being dragged?), grab a frame from inside it and look at it with `read`:

```bash
ffmpeg -hide_banner -v error -ss 288 -i lecture.mp4 -frames:v 1 -q:v 5 frame_288.jpg
```

That is what turns a `5x` label from "sped up" into "5x - waiting for Positron to download".

## Step 2 — offer the menu

If the instructions already say exactly what to do, skip this and do it. Otherwise present
the options below **with the survey's findings filled in** — the silences found and what
is on screen during them, the measured loudness, the duration — and ask which ones the
user wants. If you have a structured question tool (`ask_user` in pi, `AskUserQuestion`
in Claude Code), use it as a **multi-select**, one option per item with the finding in
its description; otherwise a numbered list in prose. Either way: one question, then
wait; do not render anything yet.

1. **Speed up the dead air** — each long silence at 5x (waits, downloads, installs) or 3x
   (shorter pauses), listing them with original times. With a floating label over each
   stretch saying what is being skipped, or without.
2. **Trim the start and end** — dead air before the talking starts or after it stops.
3. **Chapters and a contents card** — needs a transcript; chapter markers plus a pasteable
   `0:00 Title` list, and optionally a contents card at the start.
4. **Section slides** — a full-screen title card inserted at the start of each section. Say
   that these add a few seconds each and interrupt the footage; labels and chapters usually
   do the job without them.
5. **Sound level** — report the measured LUFS. If it is well below −16, recommend
   normalising and say how far off it is; if it is fine, say so and leave it out.
6. **Floating captions** — a note over a moment of footage ("the menu moved in v4").
7. **A written companion** (`.qmd`) — from the transcript: a **step-by-step guide with a
   screenshot at each step** if it is an instruction video, a **detailed summary** if it is
   a lecture (see "Written companion").
8. **Other one-offs** — an audio-only export (`.m4a`) for listening, a still frame for a
   thumbnail, a smaller re-encoded copy for sharing. These are plain `ffmpeg` on the
   *finished* file and never touch the source.

Only list what applies: a video with no long silences has no item 1, a video that is
already at −16 has no item 5.

## Step 3 — read the transcript, if you need to know what is in it

Needed for chapters, a written companion, or to find sections by content. The survey
started it in the background; by the time the user has answered the menu it is done
(`ls -la lecture-transcript.txt` — if it is missing, the STT server was unreachable; run
`vedit transcribe lecture.mp4 -o lecture-transcript.txt` in the foreground to see why).

Roughly 20x faster than real time, so a 20-minute lecture takes about a minute. The output
is one `[m:ss] text` line per spoken sentence, **in original time** — the same clock the
spec uses, so a timestamp you read there can go straight into `chapters[]` with no
conversion, and it is precise enough to find the moment a step happens on screen.

Read it, propose chapters, and **show them to the user before rendering**. Round chapter
boundaries to the start of a sentence, not the middle of one.

## Step 4 — write the spec

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
everything up. The survey's `loudnorm` line measured it (`input_i`); say what it found.
Broadcast speech sits near **−16 LUFS**. If `input_i` is well below that (−25, −38), add
`"audio": {"normalize": "ebu", "target": -16}` and mention it to the user. `vedit` measures
and corrects in one step; you do not need to run a second pass yourself.

Screen recordings often carry mouse-click transients far louder than the voice. If the
render log says `capping the gain`, that is why: `vedit` stopped short of the target to
keep those clicks under the true-peak ceiling. Report the level it actually reached and
that a limiter pass would be needed to go further — do not call it done at −16.

## Written companion

Decide from the transcript, not the filename. An **instruction video** (installing
something, a walkthrough) gets a step-by-step guide; a **lecture** gets a detailed summary.
Either way it is a **Quarto document**, `<name>-guide.qmd` or `<name>-summary.qmd` beside
the video, with a YAML header and `embed-resources: true` so the rendered HTML is one
portable file:

```yaml
---
title: "Installing R, RStudio and Quarto on macOS"
subtitle: "Companion guide to the video"
format:
  html:
    toc: true
    embed-resources: true
---
```

**Summary** (lecture): sections following the chapters, each definition and formula as
stated, the worked examples, the claims made and the questions left open. Quote the video's
own wording for anything technical; do not paste the transcript.

**Guide** (instruction video): numbered steps, the exact command or menu path used, what
the screen should show when it worked, a short troubleshooting table for anything that went
wrong on camera — and **a screenshot at each step**, so the reader can compare their own
screen to it. Screenshots come from the **source** video (the transcript's clock; the
edited file has been cut and sped up) and go in a folder beside the document,
`<name>-guide-img/step-02-download-rstudio.jpg`, one per step that changes what is on
screen. A terminal step gets the terminal *after* the output appeared.

### Finding the right frame

The transcript tells you when a step was **said**; the screen changes when it **happened**,
usually 1–10 s later. So do not grab the frame at the transcript timestamp — that shows the
moment before the click. Get the list of screen changes once (about 15 s for a 20-minute
recording; on a screencast a score of 0.04–0.1 is a page or dialog, 0.02 is typing and
cursor noise, so `0.04` is the floor):

```bash
ffmpeg -hide_banner -i lecture.mp4 -an -vf "scale=480:-1,select='gt(scene,0.04)',metadata=print:key=lavfi.scene_score" \
  -f null - 2>&1 | grep -E 'pts_time|scene_score' | paste - - | sed -E 's/.*pts_time:([0-9.]+).*scene_score=([0-9.]+)/\1 \2/'
```

For each step, the search interval is from the timestamp of the transcript line that
describes the action to the timestamp of the next line, **plus 10 s**. Take the first screen
change inside it, grab the frame **one second after it** (dialogs finish drawing), and
`read` it before you embed it — it must show the state the reader is meant to reach, not
the mouse on its way there. When there are several changes close together, or none, make
one contact sheet of the interval and pick from it; that is one `read` for six candidates
instead of six:

```bash
mkdir -p lecture-guide-img
ffmpeg -hide_banner -v error -ss 140 -t 20 -i lecture.mp4 -an \
  -vf "fps=1/4,scale=640:-1,drawtext=text='%{pts\:hms}':x=8:y=8:fontsize=28:fontcolor=yellow:box=1:boxcolor=black@0.6,tile=3x2" \
  -frames:v 1 -q:v 4 sheet_140.jpg                                      # tiles at +0, +4, … +16 s
ffmpeg -hide_banner -v error -ss 149 -i lecture.mp4 -frames:v 1 -q:v 3 lecture-guide-img/step-02-download-rstudio.jpg
```

Embed with a caption that says what the reader should see, and an alt text:

```markdown
![The RStudio download page - pick "Download RStudio desktop and server"](lecture-guide-img/step-02-download-rstudio.jpg){fig-alt="Browser on the RStudio download page"}
```

Full resolution, `-q:v 3`, no cropping and no annotation (boxes and arrows are not part of
this skill yet — leave frames whole, and say in your report if a step really needs one).
Aim for one screenshot per step and stop around 25. If you cannot view images, place each
frame by timing alone (first change inside the interval, plus one second) and say so when
you report.

When the document is written, `quarto render lecture-guide.qmd` once if `quarto` is
installed: it proves every image path resolves and leaves `lecture-guide.html` beside it.
In pi, writing a `.qmd` runs the Quarto linter automatically; fix what it reports.

## Step 5 — dry run, always

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run
```

Resolves every time, prints what will happen and estimates the final duration **without
rendering**. Read it back and check it against what was asked. Show this plan to the user
before rendering anything long.

## Step 6 — render in the background, then do the writing

A render takes minutes and needs nothing from you while it runs, so do not sit on it.
Start it detached, with its output going to a log:

```bash
nohup bash -c 'vedit apply lecture.mp4 edits.json -o lecture-edited.mp4; echo "exit $?"' \
  > render.log 2>&1 < /dev/null &
echo $! > render.pid
```

That returns immediately. **Now write the companion** (guide or summary — see "Written
companion" above; its screenshots read the *source* file, so they do not wait on the render
either) or anything else that was asked for and does not depend on the finished file. Then
collect the render — wait on the PID, bounded, and read the tail of the log:

```bash
for i in $(seq 120); do kill -0 "$(cat render.pid)" 2>/dev/null || break; sleep 15; done
tail -n 25 render.log
```

That waits at most 30 minutes (give the call a matching `timeout`); if the loop runs out,
say the render is still going rather than guessing. The log ends with `wrote <path>
(<duration>)`, the pasteable chapter list, the output path, and `exit 0` — anything other
than `exit 0` is a failure, and `error: …` above it says why. **Compare the duration to
the dry-run estimate** — if they disagree, something is wrong; do not report success. The source file is never modified; always write to a new
path. If there was nothing else to do while it rendered, run `vedit apply` in the
foreground instead and skip the log.

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
| Screenshots from the edited file | Its clock is not the transcript's — cuts and speed-ups have moved everything. Grab from the source. |
| A screenshot at the transcript timestamp | Shows the moment *before* the click. Take the first screen change between that line and the next, plus a second. |
| A guide as `.md` | The companion is a `.qmd` with a YAML header; the screenshots need a folder beside it. |

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

Had the user also ticked the guide: start that render with `nohup … &`, list the screen
changes, and for each step of the transcript grab and `read` the frame after its change,
writing `lecture-guide.qmd` and `lecture-guide-img/` while the render runs — then collect
the render log and report both.
