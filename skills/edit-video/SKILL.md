---
name: edit-video
description: Edit a video — remove sections, speed up slow passages, label them with floating text, add chapters and a contents card, fix quiet audio, and write a companion guide (with annotated screenshots) or summary from the transcript. Given a video with no or vague instructions, it surveys the recording and offers a menu of edits. Use when asked to cut, trim, shorten, speed up, caption, chapter, or clean up the sound of a recording (lecture, lab, screencast).
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

If this harness prompts for every write, tell the user once, before starting: `/workspace`
(typed by them, in the folder that holds the video) lets the rest of the run go without
prompts inside that folder and makes the recording itself undeletable. Do not ask again.


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
wait; do not render anything yet. In pi the call is literally

```
ask_user({
  question: "Which edits do you want?",
  multi: true,
  options: [
    { label: "Speed up the dead air (labelled)", description: "RStudio 5:33-7:30, Positron 8:05-10:16, ... 11.5 min becomes ~70 s" },
    { label: "Trim the start and end", description: "3.3 s before the first word, 4.8 s after the last" }
  ]
})
```

— `multi: true` is a parameter, not a word in the question; without it the user gets a
single pick and can choose only one edit.

1. **Speed up the dead air** — 10x for any silence longer than 30 seconds (waits,
   downloads, installs), 5x for everything shorter, listing them with original times. With a floating label over each
   stretch saying what is being skipped, or without.
2. **Trim the start and end** — dead air before the talking starts or after it stops.
3. **Chapters and a contents card** — needs a transcript; chapter markers plus a pasteable
   `0:00 Title` list, and optionally a contents card at the start.
4. **Chapter titles on screen** — each chapter's name floats over the footage in a black
   box (white text) for ~4 s at the chapter start (`"chapter_titles": true`). The audio
   never stops and no time is added. **Recommend this**; offer full-screen section slides
   only as the alternative, saying that slides insert a few silent seconds each and
   interrupt the flow.
5. **Sound level** — report the measured LUFS. If it is well below −16, recommend
   normalising and say how far off it is; if it is fine, say so and leave it out.
6. **Floating captions** — a note over a moment of footage ("the menu moved in v4").
7. **A written companion** (`.qmd`) — from the transcript: a **step-by-step guide with a
   screenshot at each step** (the control to click boxed and zoomed in on) if it is an
   instruction video, a **detailed summary** if it is a lecture (see "Written companion").
8. **A Blackboard embed** — a paste-ready player snippet (`vedit snippet`) with the
   chapter navigation inlined, plus the upload command for the video host. The upload
   itself waits until the user has reviewed the render (see "Publishing").
   Worth offering only when item 3 (chapters) is also taken.
9. **Other one-offs** — an audio-only export (`.m4a`) for listening, a still frame for a
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
  "speed":    [{"range": ["5:00", "8:00"], "factor": 10, "label": "10x - waiting for download"}],
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
| `chapter_titles` | `true` (or `{"seconds", "position", "color", "background", "font_size"}`): float each chapter's name over the footage at its start — black box, white text, top of frame, ~4 s. Adds no time; audio never stops. Prefer this over `slides`. `seconds` counts source footage: a cut moves the title to the surviving frame, a speed ramp over it compresses it. |
| `toc_card` | `true` (or `{"seconds", "title", "background", "color", "font_size"}`) for a contents card at the start. Needs `chapters`. Defaults: 5 s, title `Contents`, white text on black. The automatic `font_size` is sized to the row count, not the title length (about 77 px at 1080p), so long chapter titles can run off the right edge -- set `font_size` (40-56 at 1080p) when they do. |
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

Screen recordings often carry mouse-click transients far louder than the voice. `vedit`
handles that itself: when a plain gain would push the clicks over the true-peak ceiling
before the voice reached the target, it limits the clicks (the log says `limiting
transients`) and the voice still lands on target. Only if that would take more than 12 dB
of limiting does it stop short — the log then says `capping the gain` and names the level
it reached; report that level rather than calling it done at −16.

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
screen to it. **Structure the document by the video's chapters**: one `##` heading per
chapter, titled `M:SS — Chapter name` with the time and name taken *verbatim* from the
pasteable chapter list (the dry run prints it — those are edited-video times, already
computed), the steps as `###` under their chapter, and in the YAML header `toc: true` with
`toc-depth: 2`, so the document's sidebar is exactly the video's table of contents and a
reader can jump between the two. **Number the steps once, continuously, across the
whole document** — `### Step 1 — …` through `### Step N — …`, the number written into
the heading text, never restarting at 1 under a new chapter heading. (Markdown ordered
lists renumber at every break, which is exactly why the numbers live in the headings —
and they then match the `step-NN` image file names.) Screenshots come from the **source** video (the
transcript's clock; the edited file has been cut and sped up) and go in a folder beside
the document,
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
change inside it, one second after it (dialogs finish drawing), and **confirm the moment
by its text, not by looking at it**:

```bash
vedit ocr lecture.mp4 --at 149 --grep 'download|rstudio'
```

That prints every line of text on the frame with its full-frame pixel box — a few hundred
tokens that stay in your context, against 1–2.5k for an image that is soon retired. It
answers the questions that decide a frame: is the dialog I want on screen (its title and
its buttons are in the dump)? is the command fully typed (`quarto install tinyte` is not)?
what exactly does the control say — so the guide writes `rmarkdown` because the screen
does, not `markdown` because the transcript heard that. Without `--grep` you get the whole
frame, top to bottom; read it once per new screen. **Names of things come from the
screen, never from the transcript**: file names, package names, menu items, button
captions are copied out of the dump. The transcript says what to *say*; the screen says
what things are *called*.

OCR does miss things — light-on-dark button captions, icons, some terminal text. A control
absent from the dump may still be on screen. That, and only that, is when you look at a
picture. When several changes fall close together, or the dump cannot tell two moments
apart, make one contact sheet and look at that: one `read` for up to twelve candidates,
each stamped with its time:

```bash
vedit sheet lecture.mp4 140 144 148 152 156 160 -o sheet_140.jpg
```

**Look at one or two images per turn, and write down what you saw.** Every image you
`read` costs 1–2.5k tokens and is re-sent with every request until it is retired: the
harness sends only the most recent images (six by default) and turns each older one into a
one-line placeholder naming the file. Once six newer images have arrived, a frame is gone
from your view, so note its verdict in your reply the moment you look
(`sheet_140: the Finish button is up from 148 s — use 149`) — and never `read` seven at
once, since the first would be retired before you saw it.

### Drawing the reader's eye

A whole 1920x1080 frame with one small button on it does not tell the reader where to
look. When a step is about **one control** — a button, a menu item, a field, a checkbox —
mark it and, usually, crop to it. `vedit still` does both from one spec.

**Name the text; never measure it.** For anything that *is* text — a link, a menu item, a
radio option, a checkbox caption, a file name — the highlight is the control's on-screen
text and OCR places the box:

```json
{"at": 64,
 "highlights": [{"text": "RTools 4.5", "label": "Click: RTools 4.5"}],
 "dim": 0.35, "crop": {"margin": 160}, "max_width": 1280}
```
```bash
mkdir -p lecture-guide-img
vedit still lecture.mp4 step-02.json -o lecture-guide-img/step-02-download-rtools.jpg
```

Copy the text out of the `vedit ocr` dump as it is printed there. The run either places
the box — the plan says `text 'RTools 4.5' -> x 25 y 262 w 101 h 17 (OCR)` — or refuses
with a message that says exactly why:

- *appears N times on screen* — the same words are elsewhere too (a browser tab title
  repeats the page's link; "Source" is on every pane). The message lists each place with
  its box: add `"occurrence": 2` (the number in that list) or `"near": [x, y]` (the closest
  wins), or name a longer stretch of the line so only one matches.
- *was not found on screen* — with the closest lines it did read. Either your text differs
  from what is printed (a version number, a hyphen, a wrapped word), or it is a control OCR
  cannot read. Then, and only then, measure.

A box placed by `text` is done: nothing to verify, nothing to look at. Its `.check` twin is
still written, but you need not read it.

**Measuring, for what OCR cannot read** — an icon, a light-on-dark button caption, a
terminal line the dump did not show. Grab the gridded frame and `read` it:
`echo '{"at": 149, "grid": true}' | vedit still lecture.mp4 - -o grid_149.jpg`. The yellow
lines fall every tenth of the frame and carry their full-frame pixel coordinate; a control
between the `1152` and `1344` lines and just under the `648` line is at about
`x 1210, y 690`. Write `x`/`y`/`w`/`h` from those labels — the control's own edges, where
the text or button starts and stops; `pad` adds the breathing room, so do not pre-widen
`w`/`h` by guesswork — and render:

```json
{"at": 1365,
 "highlights": [{"x": 393, "y": 165, "w": 50, "h": 55, "label": "Click: gear icon"}],
 "dim": 0.35, "crop": {"margin": 160}, "max_width": 1280}
```

Every run with a measured box also writes the `.check` twin — `step-19.check.jpg`, the same
still with the grid drawn over your boxes — and prints **`grounding` lines**: OCR looked
for each label's text and says whether the outline encloses it. `LIKELY MISS` / `CLIPS the
text` means it found the words somewhere else, and names where: read the twin, then either
switch the highlight to `"text"` (the message shows OCR can read it after all) or fix the
coordinates from the grid labels. *Could not verify* means OCR has no opinion — the twin is
the judge. `read` the twin of every measured box, one image per turn, and write the
verdict into your reply. A coordinate read off the grid is good to about a dozen pixels and
the default pad absorbs that; if the result still clips one side, that side's coordinate is
off — fix it rather than growing the box. One correction is normal; a second guess without
reading the grid is how boxes end up on the wrong button. **Never apply a coordinate you
were handed without reading the twin** — a number from a checker, a reviewer, or your own
memory of an earlier frame is a hint, and the grid is the measurement.

Coordinates are always **full-frame pixels** (or percentages), even when cropping — the
crop is applied last — so nothing is re-derived after zooming in. Embed the clean file;
the `.check` twin never appears in the guide.

Every render also writes the spec back beside the image — `step-02-download-rtools.json`,
with `source`, `output` and, for a text anchor, the `resolved` box recorded — and the
sidecar is itself a valid spec. That is each screenshot's recipe: to tweak one later, edit
the sidecar and re-run
`vedit still lecture.mp4 lecture-guide-img/step-02-download-rtools.json -o lecture-guide-img/step-02-download-rtools.jpg`
(a re-run whose OCR lands somewhere else than the recorded box says so with a `note` line).
The sidecars are part of the guide's working set — keep them with the images, never list
them among the deletable working files.

The output of `vedit still` ends with a one-line `grounding` summary on stderr and the
output path on stdout, in that order — so `2>&1 | tail -2` shows both, and `tail -1` shows
only the path. If you filter the output, keep `grounding`, `note` and `error` lines.

| Key | Meaning |
|---|---|
| `at` | The moment, in source time. Omit when the input is an image (`.jpg`/`.png`) rather than a video. |
| `highlights[]` | Either `text` — the control's on-screen text, copied from `vedit ocr`; OCR places the box, with `near: [x, y]` or `occurrence: N` when it appears more than once — or `x`, `y`, `w`, `h` in full-frame pixels or percentages (`"40%"`) for what OCR cannot read: the control's **own edges**, not a box around it. Never both. `shape` `box` (default), `ellipse` or `arrow` (next row); `color` (default `red`); `thickness`; `label` — a few words, drawn just above (or below) the shape (defaults to the text). |
| `shape: "arrow"` | Points at a control instead of framing it. Name the text and the side it comes from: `{"shape": "arrow", "text": "Install", "from": "left"}` (`from` `left`/`right`/`above`/`below`, default `left`; `length` default ≈ 120 px; the tip stops just short of the text and a side with no room is refused with the side to try). Measured form `x1`, `y1` (tail), `x2`, `y2` (tip) for what OCR cannot read. `label` sits just beyond the tail. No `pad`; never grounded; `dim` needs a box or ellipse besides. |
| `pad` | How far the rectangle is inflated on every side before the outline is drawn (an ellipse is inscribed in the inflated rectangle). Top level or per highlight. Default ≈ 22 px at 1080p; raise it to 30–40 for a small control, or to draw the eye to a region rather than frame it exactly. |
| `dim` | 0–0.95: darken everything *outside* the highlights. 0.3–0.5 is plenty. Needs `highlights`. |
| `crop` | `{"margin": N}` — the highlights plus N pixels around them (start at 120–200, enough to recognise the window) — or explicit `{"x", "y", "w", "h"}`. Highlights must lie inside it; a crop that would remove one is rejected. |
| `grid` | `true` (10 divisions) or 2–25. A measuring aid for you — never in the guide. Rarely needed by hand: any run with `highlights` writes a gridded `.check` twin beside the output automatically (an explicit `grid` suppresses the twin). |
| `max_width` | Downscale the result; 1280 is plenty for HTML. Never upscales. |

One thing per screenshot: one highlight, or two numbered labels (`"1. Open File"`,
`"2. Pick Import"`) when the order matters. Show the whole screen when the reader needs to
recognise a situation (a fresh window, a dialog that appeared); crop when the step is one
control. `vedit example --still` prints a complete spec.

Embed with a caption that says what the reader should see, and an alt text:

```markdown
![The RStudio download page - pick "Download RStudio desktop and server"](lecture-guide-img/step-02-download-rstudio.jpg){fig-alt="Browser on the RStudio download page"}
```

Whole frames go in at full resolution; an annotated crop can take `"max_width": 1280`.
A box or ellipse with a short label does most jobs; an arrow (`"shape": "arrow"`, above) is
for a control that a box would hide or that sits in a crowd of look-alikes. Aim for one
screenshot per step and stop around 25. If you cannot view images, place each frame by
timing alone (first change inside the interval, plus one second), skip the highlights,
and say so when you report.

When the document is written, `quarto render lecture-guide.qmd` once if `quarto` is
installed: it proves every image path resolves and leaves `lecture-guide.html` beside it.
In pi, writing a `.qmd` runs the Quarto linter automatically. When it reports issues,
fix **only the reported lines** with `edit` — never write the file again from scratch: a
regenerated file reproduces its own mistakes, and seeing the same report twice means you
are in exactly that loop.

**Do not delete anything — even at the end.** The survey frames, contact sheets, grids,
`.check` twins, candidate stills, `scenes.txt`, and render logs cost nothing while they
sit there, and they are the evidence if a screenshot or chapter needs revisiting once the
user watches the result. When everything is verified, your final report **names** what can
be cleaned up (only files you created: explicit names or narrow globs, never a directory)
and says it is waiting on their review — delete only when the user has looked at the work
and says so.

### Handing the screenshots to the user for review

Whether a frame is the *useful* one — the typed command rather than the progress bars
after it, the dialog rather than the second before it — is the reader's judgement, not
OCR's. So once the guide is rendered, start the review server **detached** (it runs
until the user stops it, so a foreground call would never return) and offer it:

```bash
nohup vedit review lecture-guide.qmd --serve > review.log 2>&1 < /dev/null &
sleep 5; head -n 1 review.log        # serving http://127.0.0.1:8765/ ...
```

Tell the user the URL from the log, not one you remember: the port moves (`--port`) when
8765 is taken. It serves only on this machine.

The page shows every screenshot in guide order with its caption, the plain frame to
draw boxes and a crop on, and a few alternative moments (a few seconds either side, the
nearest screen changes from `scenes.txt`). The user clicks **Keep**, picks a moment,
draws as many boxes and arrows as the step needs and a crop, then **Apply** (one render), or adds
a second screenshot to a step (**New image** → **Add image**, which writes
`<stem>-b.jpg` and wraps the step's image line in a `::: {layout-ncol=2}` div), or removes
one of a step's images (**Remove image**, twice: the image line leaves the guide, the div
unwraps when one image is left, and the files move to `<guide>-review/removed/`); each
applied decision is written to `<guide>-review/review.json` and the sidecar or the
guide is edited and the still rendered — so no coordinate or time ever comes back
through you. A crop the user draws is grown to keep the boxes visible; a text anchor
that is not on a newly chosen frame is dropped, and the user draws a box instead. Say the URL and stop. When the user
says the review is done, read `review.json` **once**. It is a log: one list per step,
in order. Act only on two kinds of entry: an `edit` (or old-style `moment`) that
carries an `at` — the frame changed, so update that image's caption and alt text to
match it — and an `add` (the new image line
is already in the guide with the caption the user typed, or `(caption pending)`; write
the caption and alt text, and a sentence in the step if the second screen needs one);
plus any `note`. A `remove` entry needs nothing from you — the image line is already gone —
unless the step's text described that screen. Entries marked `error` were refused and did nothing. Never re-derive a
box or a crop from a note when the review already placed it, and never touch the
`:::` div lines the review wrote.

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

## Publishing — the Blackboard snippet

If the user took the Blackboard embed option (or asks later), prepare the pieces —
but **the upload waits until the user has reviewed the render**. Emit the snippet with
the intended URL, print the upload command, and say both are ready once they have
watched the result:

```bash
vedit snippet lecture-edited.mp4 --url "https://<video-host>/<course>/<name>.mp4" \
  -o lecture-snippet.html
# after the user has reviewed the render and said to publish:
rclone copyto lecture-edited.mp4 <remote>:videos/<course>/<name>.mp4
```

Run the upload yourself only when the user has already seen the render and asks for the
embed — a publish is outward-facing and theirs to trigger.

The rclone remote name and the public base URL are machine configuration, not part of
this skill — if you do not know them, ask the user (or check their notes) rather than
guessing a domain.

The snippet is self-contained: a version-pinned Vidstack player with the chapters read
from the rendered file's own metadata and inlined as a data: URI — nothing else to host,
and pasting it into a Blackboard Ultra item (source view, `<>` in the editor) is the whole
job. Print the snippet path and say to paste its contents. Never hand-edit the asset URLs
or the base64 track; regenerate with `vedit snippet` instead. If the rclone remote
is missing, still emit the snippet with the intended URL and hand the user the upload
command — do not try to configure credentials yourself.

## Common mistakes

| Mistake | What happens |
|---|---|
| Compensating for earlier cuts | Later ranges land in the wrong place. Always use original timestamps. |
| A slide for every speed-up | Works, but pads the video and interrupts it. Use `speed[].label` instead. |
| A slide for every chapter | Slides stop the audio and add time. `"chapter_titles": true` shows the same name without breaking the flow. |
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
| Chapters re-typed into an embed | `vedit snippet` reads them from the rendered file; regenerate, never hand-edit the base64. |
| Re-measuring highlight coordinates after a crop | Coordinates are always full-frame; the crop is applied last. Measure once, on the gridded full frame. |
| A gridded frame or a `.check` file in the guide | The grid is for you. Embed the clean output; the `.check` twin stays out. |
| Typing `x`/`y` for a link, a menu item, a file name | That is text: `"text": "RTools 4.5"` and OCR places the box. Every mis-placed box so far was a model estimating pixels. |
| A measured box nobody looked at | Off by a hundred pixels it boxes the wrong button. `read` the `.check` twin of every `x`/`y` box before embedding the clean one; a `text` box needs no look. |
| Applying a coordinate you were handed | A checker's or reviewer's `x`/`y` is a hint; the grid is the measurement. Eleven hinted coordinates were applied verbatim in one run, and all eleven were wrong. |
| Fixing a missed box by trial and error | The `.check` twin already shows the answer: read the grid labels beside the control and set `x`/`y` once. |
| Ignoring a `grounding … LIKELY MISS` line | OCR found the label's text where the box is not. Read the twin, then anchor by `text` or re-measure. |
| Piping `vedit still` through `tail -1` | The grounding summary is the last stderr line and the path is stdout; `tail -1` on the merged stream hides the verdict. `tail -2`, or `grep -E 'grounding\|note\|error'`. |
| Reading candidate frames one at a time to pick a moment | `vedit ocr --at` first (is the dialog there? is the command fully typed?), `vedit sheet` for the rest; a single frame only when the dump cannot tell. |
| Package, file and menu names from the transcript | The transcript hears `markdown` for `rmarkdown` and "cable extra" for `kableExtra`. Names come from the `vedit ocr` dump or the frame. |
| The `install.packages` line from the survey notes | A dependency scrolling past (`gridExtra`) is not what was typed (`kableExtra`). Copy the command from `vedit ocr` at the moment it was typed. |
| Image files numbered by your own count | `step-16-tinytex.jpg` under `### Step 22` confuses everyone; name each file by the guide step it sits under. |
| Fixing a "wrong screencap" by guessing again | Offer `vedit review --serve` and let the user click the moment or draw the box; then apply their `review.json` notes to the text only. |
| Step numbers restarting at 1 in each chapter | One counter for the whole guide: `Step 1`–`Step N` in the `###` headings, continuing across chapter headings. |
| The `.json` sidecars listed as deletable clutter | Each is a screenshot's recipe, written automatically by `still`. They stay beside the images. |
| Rewriting the `.qmd` when the linter reports issues | The rewrite reproduces them. `edit` the reported lines, nothing else. |
| Deleting working files as a routine last step | Nothing is deleted by default. Name what can go in the report and wait for the user to review and ask. |
| Uploading to the video host before the user has reviewed the render | Emit the snippet and print the upload command; the upload itself is the user's call. |
| Composing `ffmpeg drawbox`/`crop` by hand | `vedit still` does it from a spec, checks the highlight is inside the crop, and verifies the output size. |
| Five `read`s in one turn | Each is 1–2.5k tokens and you will mix up which picture was which. One or two per turn, and say what each showed. |
| Looking back at an old frame | Once six newer images have arrived it is a placeholder. The note you wrote when you looked is what remains — so write it then. |

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
  "speed": [{"range": ["4:20", "6:10"], "factor": 10, "label": "10x - waiting for download"},
            {"range": ["12:00", "13:30"], "factor": 10, "label": "10x - installing"}],
  "chapters": [{"at": "1:30", "title": "Downloading R"},
               {"at": "6:10", "title": "Installing RStudio"},
               {"at": "13:30", "title": "First Quarto document"}],
  "chapter_titles": true,
  "toc_card": true,
  "audio": {"normalize": "ebu", "target": -16}
}
```

```bash
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run   # check
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4             # render
```

Had the user also ticked the guide: start that render with `nohup … &`, list the screen
changes, and for each step of the transcript grab and `read` the frame after its change —
boxing and cropping the ones that are about a single control (each checked against its `.check` twin) — writing
`lecture-guide.qmd` and `lecture-guide-img/` while the render runs; then collect the render
log and report both.
