# vedit

Declarative video editing for agents. Applies a JSON edit spec — cuts, speed ramps, and
title-card or image slides — to a video, wrapping `auto-editor` and `ffmpeg`. Built so
small local models (the Pi agents) can edit lecture recordings without touching either
tool's raw CLI.

## Quick Reference

```bash
vedit probe lecture.mp4                                  # duration, fps, resolution, audio
vedit example                                            # print a starter spec
vedit apply lecture.mp4 edits.json -o out.mp4 --dry-run  # resolve times, estimate, no render
vedit apply lecture.mp4 edits.json -o out.mp4            # render
vedit transcribe lecture.mp4 -o transcript.txt           # [m:ss] one line per sentence
vedit still lecture.mp4 still.json -o step.jpg           # one frame: highlights (placed by "text" via OCR, or x/y/w/h), dim, crop, grid (+ .check twin, .json sidecar, grounding)
vedit ocr lecture.mp4 --at 64 [--grep rtools]            # the frame's text with full-frame pixel boxes: what a "text" anchor can name
vedit sheet lecture.mp4 1035 1037 1040 -o sheet.jpg       # a stamped contact sheet of several moments
vedit review lecture-guide.qmd --serve                    # click-driven review of a guide's screenshots; decisions re-render the stills
vedit example --still                                    # a starter still spec
vedit snippet out.mp4 --url https://videos.example.org/x.mp4  # paste-ready LMS embed HTML
uv tool install --editable .                             # reinstall after changing code
```

> **Agent note on project startup:** read the "auto-editor 29.3.1 behaviour" table below
> before changing anything in `render.py`. Every row was established empirically against
> the installed binary, several contradict auto-editor's own documentation site, and each
> one caused a real bug during the initial build. Do not "fix" code that looks redundant
> there without re-verifying against the binary first.

## Project Structure

```
vedit/
  src/vedit/
    __init__.py     # VeditError — message is safe to show verbatim to an agent
    media.py        # tool discovery (prefers sys.prefix/bin), ffprobe, MediaInfo
    spec.py         # JSON spec parsing + strict validation, time parsing
    render.py       # the pipeline: spans, slides, concat
    transcribe.py   # `vedit transcribe`: faster-whisper server, sentence timestamps
    geometry.py     # Rect, shared by still (what is drawn) and ocr (what was read)
    ocr.py          # tesseract passes, line grouping, the strict matcher, locate() for text anchors, the dump
    ground.py       # the advisory check on hand-placed (x/y) boxes: does the outline cover the label's text?
    sheet.py        # `vedit sheet`: a labelled contact sheet of several moments
    review.py       # `vedit review`: guide parsing, the frame cache, the server, and decision application (moment / box / crop / add)
    review_page.py  # the review page itself: HTML + inline CSS/JS, duck-typed against review.Step/Shot
    still.py        # `vedit still`: one annotated frame (or image) for illustrated guides
    snippet.py      # `vedit snippet`: paste-ready LMS embed HTML with inlined chapters
    cli.py          # argparse entry point: apply / probe / transcribe / still / ocr / sheet / review / snippet / example
  skills/edit-video/SKILL.md   # the agent-facing skill (symlinked out, see below)
```

## auto-editor 29.3.1 behaviour

Verified empirically. **These are version-specific** — if auto-editor is upgraded,
re-verify every row before trusting it, because the code is built around them.

| Behaviour | Consequence |
|---|---|
| Omitting `--when-silent nil` also strips every silent passage | Always passed; without it the edit is silently destructive |
| `--cut-out A,B C,D` parses the second range as an *input filename* | Repeat the flag instead, once per range |
| `--set-speed` takes the factor **first**: `2,600,750` | Easy to write backwards |
| `--set-speed` overrides `--cut-out` and **resurrects cut footage** | Cuts are subtracted from speed ramps in `_edit_flags` |
| Timecode `00:01:30` is rejected | Only `90sec`, `90s`, or bare frame numbers. vedit converts to frames |
| `start` / `end` / negative times are rejected | Documented on the website, absent from this build |
| Two input files do not concatenate | Slides must be rendered separately and joined by ffmpeg |
| `-preset` / `-crf` are misparsed as input filenames | `AE_ENCODE` vs `FF_ENCODE` exist for exactly this reason |
| Default `--edit audio` fails on a video with no audio stream | `--edit none` is passed when `not info.has_audio` |
| A v3 timeline referencing two different `src` files fails | `Failed to allocate buffer for new frame` — no multi-source timelines |

Bare integers are frames; vedit converts all spec times to frames via `MediaInfo.frame_at`
so the arithmetic is exact.

## ffmpeg and STT behaviour

Also established empirically, and just as expensive to rediscover:

| Behaviour | Consequence |
|---|---|
| `loudnorm`'s own second pass lands ~2dB under target | It resamples to 192kHz internally and the level does not survive the trip back to the source rate. Confirmed three ways (loudnorm, ebur128, volumedetect) and they agree. loudnorm is used to MEASURE only; the correction is applied as a plain `volume` gain, which hits the target exactly |
| `alimiter` oversampled to 192 kHz (`aresample=192000,alimiter,aresample=48000`) takes ~2.8 dB off the *voice* and stops 2.3 dB under its own ceiling | Same loss as loudnorm's dynamic mode, same cause. The limiter that handles click transients (`_audio_filter`, engaged when the gain needed exceeds the true-peak headroom, up to `MAX_LIMITING` = 12 dB) runs at the source rate, where it lands on the target and the ceiling exactly (−16.24 LUFS / −1.50 dBTP on the `clicky` fixture). The fixture's clicks are 1 ms on purpose: EBU loudness is energy per 400 ms block, and a 5 ms click carries as much energy as the tone around it, so limiting *it* lowered the measured loudness |
| `loudnorm` rejects `I` outside -70..-5 | The measurement pass uses a fixed `I=-24`; `input_*` do not depend on it |
| `drawtext` expands `%{...}` even from a `textfile` | `expansion=none` is required or `"100%{pts}"` renders as a timestamp |
| An MP4 chapter track cannot start after 0 | ffmpeg silently pins the first chapter; Matroska would keep the offset. One rule is used for both: the first chapter always starts at 0 |
| The faster-whisper server **used to** return text only (fixed server-side 2026-08-28) | `/v1/audio/transcriptions` now honours `response_format=verbose_json` (segments with `start`/`end`; `timestamp_granularities[]=word` for words) and `srt`/`vtt`. `transcribe.py` asks for `verbose_json` and stamps every segment at its own start. Windows survive only as a request bound (`--window`, default 1800 s): measured on a 19-minute file, 120 s windows were no faster — the server is serial — and cost punctuation at every cut. A server that still answers bare `{"text": ...}` degrades to one mark per window |

| The `color` lavfi source hands out `253,0,0` for `red` | It negotiates YUV and converts back. `still.rgb_of` appends `,format=rgb24` to the source so the triple is exact — it resolves *any* ffmpeg colour name by asking ffmpeg, and an unknown name fails there |
| `drawbox`/`drawgrid` take YUV only, and there is no ellipse filter | `still.py` draws every outline, the dim and the grid lines in one `geq` pass in rgb24 — one colour space until the encoder. ~1.5–3 s per 1080p still, all cores |
| A model writes highlight coordinates blind and fixes misses by trial and error (observed: seven attempts on one still, the grid grabbed only afterwards) | every `still` run with `highlights` also writes a gridded `.check` twin beside the output, so the verification read doubles as the measuring pass; an explicit `grid` suppresses the twin (the main output already carries it) |
| A `drawtext` `x=`/`y=` expression containing a comma breaks option parsing | `No option name near '…'` — quote them: `x='max(7,min(700,w-tw-7))'` |
| `drawtext` runs before `crop`, so a label clamped to the *frame* can be cropped off | Label placement in `_label_filters` clamps to the crop rectangle when there is one |
| `ffprobe` gives a JPEG a 0.04 s duration (`image2`) but a PNG/WebP/BMP (`*_pipe`) none at all | `media.probe(still=True)` lets a missing duration through for pictures and sets `MediaInfo.is_image` from the format name — checked on JPEG only at first, and every PNG *output* failed its post-render measurement (Codex caught it) |
| `-ss T` returns the first frame whose time is **≥ T** (0.11 s at 30 fps is frame 4, not 3) | `still.py` resolves `at` to a frame index up front, refuses a time inside the last frame (no frame follows it — ffmpeg would write nothing), and seeks half a frame early so a rounding error cannot skip a frame |

The STT endpoint is `http://lilripper:8552/v1/audio/transcriptions` (faster-whisper
`large-v3`, CUDA, int8_float16), overridable with `VEDIT_STT_URL`. `/api/transcribe` is the
older raw-body route Octavius uses for short voice commands.

## Architecture

Slide positions split the **source** timeline into spans. Each span is rendered separately
by auto-editor (cutting away everything outside it), slides are rendered as matching clips,
and ffmpeg's concat demuxer joins them.

`output_time()` in `render.py` is the centre of gravity: captions and chapter marks are
written in SOURCE time and must land at OUTPUT time after cuts, ramps, slides and the
contents card. `chapter_titles` (2026-08-30) derives one Overlay per chapter at parse time
(spec.py, after the sorts): the chapter's name floats in a solid black box, white text, top
of frame, for 4 s or until the next chapter, whichever comes first — the unobtrusive
replacement for per-section slides, which stop the audio and add time (Dave, after the
first real run: "the inserted slides _really_ break up the flow"). Zero render-side code:
derived overlays ride the ordinary `text` machinery, output-time mapping included. Three
edges from the Codex round: the title starts at the **first surviving source instant** at
or after the chapter (`first_surviving` walks the cuts), because a chapter inside cut
footage validly snaps to the boundary while a fully-cut overlay fails `check_timeline` —
without the shift, turning titles on rejected the spec; `false` is off and `{}` is
all-defaults, but `0`/`""`/`[]` are errors, never a silent no-op; and `seconds` counts
source footage, so a ramp over the chapter start compresses the title on screen. Derived
titles are appended after user captions and the stable sort keeps them there, so at the
same instant and position the chapter title draws on top. A chapter at 0:00 with a
toc_card lands on the first footage frame, not on the card (`after_slide=True`). Its
`after_slide` flag picks which side of a card a timestamp falls on —
a caption belongs over the footage, a chapter mark on the card that introduces it. The
duration estimate in `plan()` comes from the same function, so the two cannot disagree.

`_chapter_marks()` is the single canonical chapter sequence, used by both the ffmetadata
file and the printed list. Keep it that way: they diverged once, and the printed list
promised a chapter the file did not contain.

Two details keep the output exact, and both are load-bearing:

* **Edits are clipped per span, and cuts are subtracted from speed ramps.** Otherwise a
  ramp belonging to another span resurrects footage this span deliberately cut. A cut
  always wins over a speed ramp covering the same footage.
* **Segments carry PCM audio; AAC is encoded once at the end.** Concatenating AAC segments
  accumulates ~6ms of priming padding per junction — progressive drift over a lecture with
  several slides. With PCM intermediates a test edit lands on exactly 36.000000s / 1080
  frames instead of 36.023s.

Video is stream-copied by the concat step, so it is encoded only once. Output is staged at
`.vedit-<name>` beside the destination — any pre-existing entry there (a leftover, or a
planted symlink to the source) is removed first — measured, and moved into place only if its
duration is within `DURATION_TOLERANCE` (0.25 s) of the plan's estimate; writing over the
source is refused. `still` does the same with the output size.

Spec validation is deliberately strict — a silently ignored malformed entry produces a
plausible-looking video that is not what was asked for. Prefer a clear error naming the key.

### `vedit still`

The same shape, one frame at a time, for the illustrated guide the `edit-video` skill
writes: `{at, highlights[], dim, crop, grid, max_width, pad}` → one ffmpeg command → a `.jpg`
or `.png`. The invariant is the **output size**: `output_size()` computes it from the crop
and `max_width`, `render()` measures the staged file with ffprobe and refuses to install a
mismatch. Two rules keep the agent's job simple: coordinates are **always full-frame
pixels** (or `"NN%"`) even when cropping, because the crop is applied last; and a highlight
outside an explicit crop is an error, not a silent omission. `grid: true` renders a
labelled pixel grid for the measuring pass — the agent reads coordinates off it, then
re-renders without it. An image input (by suffix) skips `at` and `-ss`. Every successful render writes the spec back as a **sidecar** (`step.jpg` → `step.json`, the raw spec plus `source`/`output` metadata keys the parser accepts and ignores, so a sidecar is itself a valid spec) for later reproduction or tweaking; the write is skipped when the spec argument already *is* the sidecar, and the `.check` twin gets none.

**Text anchors and grounding (`ocr.py`, `ground.py`; 2026-08-31, redesigned 2026-09-01).** The
first design OCR-*checked* boxes the model had placed by hand, and its first real run (take 5
of the Windows R setup guide) came out worse than the run before it: the check passed a box
on the wrong row (a half-score "RTools" inside the box beat a full-score "RTools 4.5" two rows
up), sent a box to the search field ("gear" fuzzy-matched "Sear" at 0.75), called a button a
MISS because "restart" sat in a banner while the white-on-blue caption was unreadable, and
measured coverage against the padded rectangle so an outline through the text still passed —
and its lines printed *before* the `wrote` lines, so a `| tail -2` hid them. The redesign is
subtractive: the model names *what* and OCR decides *where*. A highlight may be
`{"text": "RTools 4.5"}` (plus `near: [x, y]` or `occurrence: N` when the words appear more
than once — a browser tab title repeats the page's link, "Source" is on every pane); `locate`
resolves it or raises an error naming the candidates, or the five closest lines and the
`x/y/w/h` fallback. The sidecar records the `resolved` rect and a re-run warns if it moved by
more than `pad`. `vedit ocr` prints the same lines for the model to choose from.

Reading: three tesseract passes merged — 960x540 tiles with 320 px overlap at 3x (any word up
to 320 px wide sits whole in some tile, away from the edges tesseract reads badly), the same
on the *inverted* frame (a dark-theme console reads as light-on-dark and is otherwise
dropped), and a plain 1x pass (which reads underlined links the upscale spoils) — about
20 s at 1080p, once per still (`rows_for` caches). Dedupe keeps one reading per box, drops
specks (under 7x4 px), fragments of a bigger reading ("RStudio-2026" inside the filename,
cut at a tile edge) and tall boxes spanning two lines; every rule that guessed "the less
confident nested one is junk" deleted a real word somewhere on the fixture frames, so junk
read off a real word's pixels stays and is merely barred from *starting* a line in
`lines()` (words are grouped left to right — top to bottom split a line whenever
baselines differed by a pixel).

Matching: a token that is short (under 5 chars) or carries a digit matches exactly only,
with l/I/O/o/@ folded to digits in digit-bearing tokens; a missing digit-bearing token voids
the candidate; an exact match scores 1 whatever tesseract's confidence (it scores words it
does not know, `install.packages`, at 0), a near match 0.9 ("installed" for "installer"
ranks below the exact one on the same line), a conf-0 word may stand in for a long token,
up to two junk words between matches are skipped, a token OCR split is rejoined by squashed
comparison, and a long word that *starts* with the token is the token with glued characters
— never the reverse, which is how the truncated reading once passed for the `.exe`. Every
occurrence on a line is a separate hit. `locate` needs 0.75 (one dropped "you" of four) and
prefers exact readings when any exist. `ground` checks only hand-placed labelled boxes: an
in-box hit counts only if nothing on the frame scores higher, a MISS needs a full-score
hit, a fragment elsewhere is "could not verify", coverage is measured inside the outline
(thickness + 4 px), the lines print *after* the `wrote` lines with a one-line summary, and a
MISS says to anchor by `text` or read the twin — never "set x y". Known blind spots, by
design and by measurement: white-on-blue button captions and icons; those stay measured.

Fixtures: `tests/fixtures/ground_truth.json` (committed) against real frames of the setup
recording (`tests/fixtures/frames/`, gitignored, `extract.sh`); `test_fixtures.py` skips
without them. The cases are the take 5 false verdicts and the text each step should anchor
to, including the ones OCR is known not to read — when one of those starts resolving, move
it up. Expect readings to shift when the tile geometry or passes change: every rule above
was adjusted against those frames, not reasoned out.

**Review (`review.py`, 2026-09-02).** The last judgement the pipeline could not make:
whether a frame is the *useful* one. In take 6 the model's text-picked moments were right
except where a terminal had scrolled (the box landed on the cmd.exe title bar, the only
place OCR could read "quarto install tinytex") or the console was mid-spew; fixing those
used to mean typing to the model. `vedit review guide.qmd` parses the guide's images in
order (with the heading above each), extracts the plain frame at each sidecar's `at` and
up to four candidates (±3 s, the scene change before and after from `scenes.txt` + 1 s),
and writes `<guide>-review/review.html` + `manifest.json`. The page (inline CSS/JS, no
dependencies) offers Keep / Use this moment / drag a box on the plain frame (coordinates
scaled back to full-frame pixels) / a label / a note. `--serve` runs a stdlib
`ThreadingHTTPServer` on 127.0.0.1 that serves the guide folder (refusing anything that
resolves outside it), records every decision in `review.json`, and applies moment and box
decisions immediately: `apply_decision` edits the sidecar (`at`; or `highlights` replaced
by one x/y/w/h box keeping the old label, and an explicit crop replaced by a margin crop
since it may not contain the new box; a text anchor's `resolved` dropped so it re-resolves)
and re-runs `vedit still` in a subprocess, returning the image URL for the page to reload.
Without `--serve`, the page collects the JSON in a textarea and `vedit review --apply`
replays it. Geometry never goes back through the model; the `note` field is for it, once.

**Round two (2026-09-02, after first use).** Three asks from the first session: a moment
click gave no visible sign it worked and left the drawing frame at the old moment; there
was no way to change a crop; and a step could not hold two screenshots (the Rtools step
wants the version list *and* the installer link on the next page). So: the page half moved
to `review_page.py` (duck-typed against `Step`/`Shot`, never imports `review`), frames
are cached by time (`frame-{t:g}.jpg` at page width, `thumb-{t:g}.jpg` at half) so a
rebuild after an add costs only the new shot, and `GET /frame?at=T` extracts on demand.
A moment click marks the candidate, swaps the plain frame to that moment (no box), shows
a busy overlay until the re-rendered still has loaded, and blocks drawing until the new
frame is in. A Box/Crop toggle per shot: a crop drag sets an explicit `crop`, "No crop"
removes it, "Margin crop" sets `{"margin": 160}`; a box drawn inside an explicit crop
keeps the crop (only a crop that no longer contains the box falls back to the margin).
"New image" mode stages a moment + optional box/crop, "Add image" posts `action: add`:
`add_image` renders `<stem>-b.jpg` (then `-c`, `-d`; a suffix is skipped if its image,
sidecar, or `.check` twin exists), then edits the `.qmd` by line index — wrapping the
step's image line in `::: {layout-ncol=2}` … `:::` with blank lines around it, or
inserting before the closing `:::` of an existing div and bumping `layout-ncol` (max 3)
— and `parse_guide` groups a layout div's images into one step (keys a, b, c). The edit
is validated against the file before *and after* the render (the manifest's line numbers
are only good while the guide is as it was built), inserted lines take the anchor line's
CRLF/LF, and the write goes through a temp file + rename; so does `review.json`, which
is now a log — one ordered list per step — with a refused decision kept and marked
`error` so `--apply` skips it, and `apply_all` rebuilding the manifest after each add so
a second add lands after the first. The GET handler admits the guide folder *or* the
review folder by path parts (a `-o` outside the guide folder used to 404 its own page).
Codex reviewed the first cut: the atomic/re-validated splice, the suffix reservation,
the log write, the still-reload error path and the drawing block came from that round;
its "grounding can fail after the files are written" finding was set aside (grounding is
findings-only and returns 0). Tests: `tests/test_review.py` (grouping, the frame cache,
every decision, the containment rule, a refused crop carrying vedit's message, add
creating then extending a div byte-for-byte, a fourth refused, CRLF, guide drift, orphan
suffixes, a mixed log replay, the served round trip with `/frame` caching and an add over
HTTP, both containment layouts) and `tests/test_review_page.py` (markup ids/classes the
JS relies on, escaping, and `node --check` on the extracted script).

**Round three (2026-09-02, after a full session of clicking).** The log from that session
was the brief: three crops refused because a snug crop excludes the 22 px *pad* `vedit
still` puts round a box; a moment click on a text-anchored still refused ("text not found
on this frame") while the page's drawing frame had already moved, so the box drawn next
landed at the old moment; every drag its own render, with the OCR re-resolve on a moment
change (~20 s) wasted whenever a drawn box replaced the anchor a second later; and one box
per still. So the page now holds a **draft per shot** — seeded from the sidecar (`Shot.boxes`
as full-frame rects, a text anchor contributing its `resolved` rect; `Shot.crop`), drawn on
the canvas at all times, every Box-tool drag *appending* a box with the label field's text,
Undo/Clear boxes, Crop-tool drags replacing the crop — and one **Apply** posts a single
`edit` decision with only the dirty fields (`at`, `boxes`, `crop`). Server side `_apply_edit`
does the work: `boxes` replaces the highlights (`[]` removes them and any margin crop, and
that invariant is enforced after every field); a rect crop is **grown** to contain each
highlight's padded extent (pad rule mirrored from `still.py:281`, clamped to the frame) and
the message says so; a box added under an explicit crop grows that crop instead of Phase 7's
replace-with-margin; a moment change re-resolves anchors and, when `vedit still` reports
`highlights[i].text … was not found`, drops that anchor and renders again (once per anchor,
keyed by index so an apostrophe in the text doesn't matter), dropping `dim` and a margin crop
when nothing is left; the reply carries the sidecar's resulting `at`/`boxes`/`crop`, read
back under the lock, and the page adopts them so a grown crop or a dropped anchor shows on
the canvas. Old `moment`/`box`/`crop` log entries are `edit` with one field. In New-image
mode the step's canvases share one staged draft, so a moment click swaps all of them and
switching back to Edit restores each shot's own frame; a draft is frozen while its Apply is
in flight; a failed Add keeps what was staged; an added caption is flattened to one line
with `[`/`]` and `"` replaced so it cannot break the image line. The seed JSON escapes every
`<` as `\u003c` (`</SCRIPT>` closes a script tag too). Codex again: accepted the in-flight
freeze, the shared-canvas fix, the caption sanitiser, the post-fields invariant, the
index-keyed recovery, the escaping and the refused-only-is-not-decided nit; set aside, as in
round two, "grounding can fail after the files are written".

**Round four (2026-09-03): arrows, and removing an image.** Dave's ask after round three was
"remove an added image" plus arrows if they were cheap. They were: an arrow is one more mask in
the same `geq` pass (below), so the page's Arrow tool is a drag from tail to tip that pushes
`{shape: "arrow", x1, y1, x2, y2, label?}` into the same `boxes` draft the Box tool fills, and
the server's `_parse_boxes` accepts it everywhere `boxes` is read (`edit`, the legacy `box`,
`add`), sets `dim` only when a box or ellipse is present (an arrow leaves nothing bright, and
`still` refuses dim with arrows alone), and grows a crop to `still.arrow_extent`. **Remove
image** is a two-click button on every shot of a step with two or more; the server's
`remove_image` deletes the image line and the one blank line `add_image` paired it with,
unwraps the div when one image remains (the outer blank lines stay: they were only added when
the neighbours were text) or decrements `layout-ncol`, and **moves** the image, sidecar and
`.check` twin into `<out_dir>/removed/` (`-1`, `-2` on a collision) — never deletes. Add then
remove gives the original guide back byte for byte, LF and CRLF, for a div created around a
single image and for one extended. `remove` rebuilds the page like `add` and replies
`reload: true`; a one-shot step is refused ("remove it by editing the guide"). Built as before:
contract, three builders (Opus on `still`, Sonnet on server and page), Codex review. Codex's
catches this time: a quick double click could post a second `remove` against an index the
rebuild had shifted (the buttons now disable per step until the reply, and the decision
carries the image name for the server to refuse a mismatch); a hand-added image line inside
the div passed the drift check (the div's image lines are now recounted); the moved files were
not bounded to the guide folder (they are); the bundle's collision suffix was chosen per file;
and the page accepted a 24 px arrow that `still` refuses below 40 at 1080p (same formula on
both sides now). Set aside: making the guide edit and the file moves one transaction — guide
first is the safe order (a failed move leaves an unreferenced file, the other order a broken
image), and a rebuild failure already reads "removed X, but the page could not be rebuilt".

**Arrows (`still.py`, 2026-09-03).** `{"shape": "arrow"}` with either `text` + `from`
(`left`/`right`/`above`/`below`, default `left`) + `length` (default `max(60, min(w,h)/9)` = 120
at 1080p) — OCR finds the text, the tip stops `max(6, pad/2)` px short of the near edge's
midpoint, the tail is `length` further out, clamped to the frame and refused with the side to
try when that leaves less than the head needs — or measured `x1, y1` (tail) and `x2, y2`
(tip). Default thickness `max(4, min(w,h)/135)` = 8 at 1080p; head length `max(18, 4t)`,
half-width `max(9, 2t)`; below `max(24, L + 8)` px there is no room for a head and it is an
error. The shaft is "distance to the tail→base segment below t/2" (`clip` for the parameter,
`hypot` for the distance) and the head a filled triangle from three `gte(cross, 0)` half-planes
with the winding fixed in Python; the apex sits half a pixel past the tip because geq samples
pixel centres and an apex on an integer coordinate was drawn in three orientations and dropped
in the fourth. Constants are formatted to three decimals and parenthesised (a `%g` with six
digits moves a 1080p head edge by a pixel). `arrow_thickness`/`arrow_head`/`arrow_extent` are
module functions the review server imports; `extent` is the endpoints' bbox grown by
`max(hw, t) + 1`, `pad` is 0 and refused on arrows (use `length` or move the tail). The label
sits beyond the tail with its *near* edge `size/2` px off — the box's half-extent along the
arrow is `(|ux|·tw + |uy|·th)/2` inside the drawtext expression — because centring it a fixed
distance out put a wide label over the shaft on the first real frame. An arrow contributes
nothing to the un-dimmed area, so `dim` with arrows only is an error; grounding skips arrows
and counts only boxes and ellipses; a text arrow's sidecar records `resolved` (the OCR rect) and
`line` (the endpoints drawn), both accepted-and-ignored keys on input.

**`pad` (2026-08-30):** the named rectangle is inflated by `pad` px on every side before
drawing (`Highlight.target` is what the spec said, `Highlight.rect` what is drawn,
`Highlight.extent` the drawn rect clipped to the frame; default `max(6, min(w,h)/48)` ≈ 22
at 1080p, top-level `pad` sets the default and each highlight may override). A box's `rect`
*is* its extent — clamped, so the ring stays closed at a frame edge; an ellipse keeps its
true centre and radii (`rect` may leave the frame, geq just never evaluates those pixels),
because clamping the bounding box first squashed it into a different ellipse — Codex caught
that on review. An ellipse is inscribed in the inflated rectangle, so its outline is `pad`
px outside the target only at the four cardinal points. Motivation: the first real run on a 1080p screencast
(`~/Videos/editing/windows_R_setup`) produced boxes of exactly the text's line height
(`h: 24–28`) with zero tolerance, and four of them clipped their target by 5–15 px — a
grid-read coordinate is good to about a dozen pixels, so hugging boxes clip on every such
miss. Crop `margin` and the explicit-crop containment check use the extent (the error names
both rectangles), the label sits above the padded rect, and for a nonzero pad the plan
prints `pad N -> x y w h` (the extent). Tests pin `"pad": 0` on the geometry fixtures and cover the default,
the override, edge clamping, and the plan/margin arithmetic.

`snippet` (2026-08-30) emits paste-ready LMS embed HTML: a Vidstack player pinned to
`@vidstack/cdn@1.15.6` on jsDelivr, the video by `--url`, and the file's embedded chapters
inlined as a base64 `data:` WebVTT track — so the snippet has no sidecar and the only
external dependency besides the video is the pinned CDN. Verified against Blackboard
Ultra (2026-08-30): its editor passes `<link>`/`<script type=module>`/custom elements
through, so the pasted snippet renders the full player, chapters and all. The pin is
load-bearing twice over: pasted course items can never be re-pointed in bulk, and the
package's own chunk imports are *absolute pinned jsDelivr URLs*, which is also why naive
self-hosting of the assets doesn't work (vendoring would mean rewriting those URLs —
considered, parked). Cue titles are sanitized (`-->` → `→`, newlines flattened, HTML
escaped, empty → `Chapter N`) so no chapter title can break the VTT grammar or the HTML
attribute it rides in. A chapterless video still gets a player, minus the track, with a
note on stderr. Tests: `tests/test_snippet.py`.

## Testing

```bash
./run-tests.sh              # or: uv run pytest
uv run pytest -k slides     # one area
```

398 tests, about ten minutes (the OCR cases on real frames are ~20 s each) — they render real video through the actual CLI, so they catch
flag-composition bugs that unit tests would not. Fixtures (a 30s clip with audio, a 30s
silent clip, a 900x900 RGBA image) are built by ffmpeg once per session in `tests/conftest.py`.

```
tests/
  util.py                # ffprobe helpers, pixel sampling, CLI runner
  test_timing.py         # exact duration and frame counts -- the core invariant
  test_slides.py         # text expansion, letterboxing, colours
  test_validation.py     # 25 malformed specs, each must fail cleanly
  test_safety.py         # source protection, debris, dry-run estimates, probe/example
  test_silent_source.py  # videos with no audio stream
  test_transcribe.py     # verbose_json parsing, window offsets, fail-closed server errors
  test_still.py          # exact ring/dim/crop pixels, arrow shaft/head/label pixels, output size, image input, the error table
  test_ocr.py            # tokens, line grouping, dedupe, the matcher, locate(), the dump; tesseract on an ffmpeg-drawn image
  test_ground.py         # the verdict logic on synthetic rows; CLI output order; text anchors through the CLI and sidecar
  test_sheet.py          # contact sheet size and labels
  test_fixtures.py       # text anchors and verdicts on real screencast frames (skips without tests/fixtures/frames/)
  test_review.py         # decision application, the .qmd edits for added and removed images, and the served round trip
  test_review_page.py    # the page's markup contract with its JS, and a node --check of the script
```

The invariant is **exact** duration and frame count, never "looks about right" — every
expected number is derivable by hand from the spec. Tests invoke `python -m vedit.cli`
from the working tree, so they never test a stale install.

Some behaviours cannot be asserted on a filter string, so they are tested through the
pixels: `%{pts}` staying literal is checked by confirming two frames of one static slide
are byte-identical, and letterboxing by sampling an edge pixel and a centre pixel.

**When fixing a bug, add the case and watch it fail first.** The suite was mutation-tested
at the initial commit — dropping `expansion=none`, removing the cut-subtraction from speed
ramps, and disabling the source-overwrite guard each produced a failure. A green suite that
cannot go red is worth nothing. Note the flagship "everything at once" case did *not* catch
the speed-ramp mutation, because its cut and speed ranges do not overlap; interaction bugs
need their own dedicated case.

## Development

`uv` only — never pip/pipx. The install is editable via a `.pth`, so **source edits take
effect immediately — no reinstall needed** (verified). Re-run
`uv tool install --editable . --force` only when `pyproject.toml` changes: new dependency,
renamed entry point, changed version constraint.

Note the tool's venv resolved to Python 3.14, not the 3.12 in `.python-version`; the
project only requires `>=3.12`, so both are fine, but check the venv's version before
blaming a syntax error on the source. `auto-editor` comes in as a dependency;
`ffmpeg`/`ffprobe` must be on the system. Note the auto-editor PyPI package is only a shim that downloads a ~40MB native
binary on first run — there is no Python API to import.

## The skill

`skills/edit-video/SKILL.md` teaches an agent to write the spec, and lives here because it
documents this repo's schema. It is symlinked into both skill directories, which are
themselves symlinks into other repos:

```
~/.claude/skills   -> git_repos/config_files/claude/skills
~/.pi/agent/skills -> git_repos/pi_harness/skills     # where the Pi agents read from
```

Both `edit-video` entries point at `git_repos/vedit/skills/edit-video`. **If the spec
schema changes, update SKILL.md in the same commit** — that pairing is the whole reason the
skill lives here. The entries are untracked in those two repos; leave committing them to Dave.

## Open items

- **Contents card at high chapter counts (noted 2026-08-30).** The model tends to propose
  many sections — 12 on a 20-minute setup screencast — and that may well be right, but it
  wants a look once lectures go through this: is the chapter count "good", and does the card
  still read? Today `_toc_slide` (`render.py`) is a single column whose font size is
  `min(height/14, height/(rows+2))` with a floor of 14 px — 67 px per line at 12 chapters
  on 1080p, 45 px at 20, 32 px at 30 — and `drawtext` does not wrap, so a long title just
  runs off the right edge. If the counts stay high, the fix is a **two-column card**
  (split the entries at the midpoint, render two `drawtext` blocks with `x` at ~5 % and
  ~52 %, keep one font size for both columns), possibly automatic past a threshold
  (~14 entries) with a `toc_card.columns` override. Nothing to do until a lecture run shows
  whether the count or the layout is the problem.
