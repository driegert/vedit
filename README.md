# vedit

Declarative video editing for agents. You write a JSON spec; `vedit` applies it.

```bash
vedit probe lecture.mp4
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4
```

```json
{
  "cuts":     [["0:00", "1:30"], ["14:05", "end"]],
  "speed":    [{"range": ["5:00", "8:00"], "factor": 5, "label": "5x - waiting"}],
  "text":     [{"range": ["9:00", "9:20"], "text": "the menu moved in v4"}],
  "slides":   [{"at": "8:00", "text": "Part 2: Multitaper", "seconds": 3}],
  "chapters": [{"at": "8:00", "title": "Installing the toolchain"}],
  "toc_card": true,
  "audio":    {"normalize": "ebu", "target": -16}
}
```

`cuts` remove, `speed` compresses (with an optional floating `label`), `text` adds floating
captions, `slides` insert full-screen cards, `chapters` produce real chapter markers plus a
pasteable list, `toc_card` renders a contents card, and `audio` fixes a too-quiet recording.

```bash
vedit transcribe lecture.mp4 -o transcript.txt   # timestamped, for proposing chapters
vedit still lecture.mp4 still.json -o step.jpg     # one frame, highlighted/cropped, for a guide
vedit example --still                            # starter spec for a still
```

Times accept `"1:30"`, `"1:02:03"`, `90`, `"90s"`, `"start"`, `"end"`. Ranges are
half-open, `[start, stop)`. **Every time refers to the original video** — earlier cuts
never shift later timestamps. Slides take exactly one of `text` or `image`.

## Install

```bash
uv tool install --editable .     # from this directory
```

Needs `ffmpeg`/`ffprobe` on the system; `auto-editor` comes in as a dependency.

## Tests

```bash
./run-tests.sh
```

Renders real video through the CLI; takes about a minute. See `CLAUDE.md` for what is
covered and how to extend it.

## Why this exists

`auto-editor` does the cuts and speed ramps well, but its CLI has sharp edges that an
LLM agent reliably falls off. All of the following were verified against **auto-editor
29.3.1**, and `vedit` exists to absorb them:

| Trap | Behaviour |
|---|---|
| Omitting `--when-silent nil` | Every silent passage is removed too, silently. |
| `--cut-out A,B C,D` | The second range is parsed as an *input filename*. Repeat the flag instead. |
| `--set-speed 2,20sec,25sec` | The factor comes **first**, not the range. |
| `--set-speed` over a `--cut-out` | The speed ramp **wins** and resurrects the cut footage. |
| `00:01:30` | Timecode is not supported — only `90sec`, `90s`, or bare frame numbers. |
| `start` / `end` / negative times | Documented on the website, but rejected by this build. |
| Two input files | They do not concatenate; you get one file's output. |
| `-preset` / `-crf` | Not accepted; misparsed as input filenames. |
| A video with no audio | The default `--edit audio` fails outright; needs `--edit none`. |

## How it works

Cuts and speed ramps go to `auto-editor`, whose ranges are all source-relative.

Slides cannot be done in `auto-editor` at all: a v3 timeline referencing two different
source files fails with `Failed to allocate buffer for new frame`. So slide positions
split the source into spans, each span is rendered separately, slides are rendered as
matching clips, and ffmpeg's concat demuxer joins them.

Two details keep that exact:

* **Edits are clipped to each span, and cuts are subtracted from speed ramps.** Otherwise
  a ramp belonging to another span resurrects footage the span had cut away. A cut always
  wins over a speed ramp covering the same footage.
* **Segments carry PCM audio and are encoded to AAC once, at the end.** Concatenating AAC
  segments accumulates priming padding at every junction — about 6 ms each, which becomes
  audible drift over a lecture with several slides. With PCM intermediates a test edit
  lands on exactly 36.000000s / 1080 frames instead of 36.023s.

The video is stream-copied by the concat step, so it is encoded only once.

## The agent skill

`skills/edit-video/SKILL.md` teaches an agent to write the spec. It lives here, with the
code whose schema it documents, and is symlinked into both skill directories so there is
one source of truth:

```
~/.claude/skills/edit-video    -> git_repos/auto-edit/skills/edit-video
~/.pi/agent/skills/edit-video  -> git_repos/auto-edit/skills/edit-video
```

## Notes

Output is staged beside the destination and moved into place only on success, so a failed
render never leaves a partial file. Writing over the source file is refused.
