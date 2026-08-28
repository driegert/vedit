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
uv tool install --editable .                             # reinstall after changing code
```

> **Agent note on project startup:** read the "auto-editor 29.3.1 behaviour" table below
> before changing anything in `render.py`. Every row was established empirically against
> the installed binary, several contradict auto-editor's own documentation site, and each
> one caused a real bug during the initial build. Do not "fix" code that looks redundant
> there without re-verifying against the binary first.

## Project Structure

```
auto-edit/
  src/vedit/
    __init__.py     # VeditError — message is safe to show verbatim to an agent
    media.py        # tool discovery (prefers sys.prefix/bin), ffprobe, MediaInfo
    spec.py         # JSON spec parsing + strict validation, time parsing
    render.py       # the pipeline: spans, slides, concat
    cli.py          # argparse entry point: apply / probe / example
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

## Architecture

Slide positions split the **source** timeline into spans. Each span is rendered separately
by auto-editor (cutting away everything outside it), slides are rendered as matching clips,
and ffmpeg's concat demuxer joins them.

Two details keep the output exact, and both are load-bearing:

* **Edits are clipped per span, and cuts are subtracted from speed ramps.** Otherwise a
  ramp belonging to another span resurrects footage this span deliberately cut. A cut
  always wins over a speed ramp covering the same footage.
* **Segments carry PCM audio; AAC is encoded once at the end.** Concatenating AAC segments
  accumulates ~6ms of priming padding per junction — progressive drift over a lecture with
  several slides. With PCM intermediates a test edit lands on exactly 36.000000s / 1080
  frames instead of 36.023s.

Video is stream-copied by the concat step, so it is encoded only once. Output is staged at
`.vedit-<name>` beside the destination and moved into place only on success; writing over
the source is refused.

Spec validation is deliberately strict — a silently ignored malformed entry produces a
plausible-looking video that is not what was asked for. Prefer a clear error naming the key.

## Testing

```bash
./run-tests.sh              # or: uv run pytest
uv run pytest -k slides     # one area
```

59 tests, about a minute — they render real video through the actual CLI, so they catch
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

Both `edit-video` entries point at `git_repos/auto-edit/skills/edit-video`. **If the spec
schema changes, update SKILL.md in the same commit** — that pairing is the whole reason the
skill lives here. The entries are untracked in those two repos; leave committing them to Dave.
