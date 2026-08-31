"""`vedit still`: one annotated frame from a video, or an annotated copy of an image.

Invariants exercised here:

- Malformed specs fail cleanly (no traceback, no output file, a message naming the
  offending key) -- same contract as `apply`, checked in `test_validation.py`.
- The rendered geometry is exact and derivable by hand: highlight rings land on the
  pixels the geq expression says they should, `dim` scales brightness by exactly
  `1 - dim` outside the highlights, `crop`/`max_width` produce the exact resolution
  `still_mod.output_size()` predicts, and percentages resolve against the real frame
  size.
- `--dry-run`'s `estimate` line matches the resolution a real render produces for the
  same spec.
- An image input skips `at` entirely (and rejects it); a video input requires it.
- No debris (`.vedit-*`) survives a failed render.

Output is PNG throughout, per house style for pixel comparisons -- lossless, so the
`PIX_TOL = 3` below is slack for the source video's own h264 encoding, not for
`still`'s renderer. The BOX/ELLIPSE highlight coordinates were chosen by sampling the
640x360 `testsrc2` fixture ahead of time so the outline colour is never coincidentally
close to the background it is drawn over -- otherwise "the ring is outline-coloured"
and "the interior matches source" could both pass by accident.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from util import pixel, region_mean, resolution

PIX_TOL = 3

# A flat solid-green patch of the 640x360 testsrc2 fixture at t=0 (verified by direct
# sampling), used for the box highlight so the blue outline never blends into it.
BOX = {"shape": "box", "x": 125, "y": 90, "w": 80, "h": 60, "thickness": 4, "color": "#0000ff",
       "pad": 0}
DEFAULT_PAD = 8  # max(6, round(360 / 48)) for the 640x360 fixture

# A flat solid-blue patch, used for the ellipse so a red outline stands out from it.
ELLIPSE = {"shape": "ellipse", "x": 330, "y": 10, "w": 80, "h": 80, "thickness": 4, "color": "red",
           "pad": 0}


def run_still(workdir: Path, source: Path, spec: dict, out: Path,
              *extra: str) -> subprocess.CompletedProcess:
    """Invoke `vedit still` from the working tree, so tests never hit a stale install."""
    spec_file = workdir / "spec.json"
    spec_file.write_text(json.dumps(spec))
    return subprocess.run(
        [sys.executable, "-m", "vedit.cli", "still", str(source), str(spec_file),
         "-o", str(out), *extra],
        capture_output=True, text=True,
    )


def render_still(workdir: Path, source: Path, spec: dict, name: str = "out.png") -> Path:
    """Render and assert it succeeded, returning the output path."""
    out = workdir / name
    proc = run_still(workdir, source, spec, out)
    assert proc.returncode == 0, f"vedit still failed:\n{proc.stdout}\n{proc.stderr}"
    assert out.exists(), "vedit reported success but wrote no file"
    return out


def assert_rgb(actual: tuple[int, int, int], expected: tuple[int, int, int],
               tol: int = PIX_TOL, msg: str = "") -> None:
    assert all(abs(a - e) <= tol for a, e in zip(actual, expected)), (
        f"{msg}: got {actual}, expected {expected} (+/- {tol})"
    )


# ---- malformed specs: each must fail cleanly, naming the offending key ------------------

BAD_SPECS = [
    ("unknown top-level key",
        lambda: {"cutz": 1}),
    ("unknown highlight key",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10,
                                          "bogus": 1}]}),
    ("highlights not a list",
        lambda: {"at": 0, "highlights": "nope"}),
    ("highlight not an object",
        lambda: {"at": 0, "highlights": [1]}),
    ("missing w/h",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0}]}),
    ("negative x",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": -5, "y": 0, "w": 10, "h": 10}]}),
    ("x+w past the frame",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 630, "y": 0, "w": 20, "h": 10}]}),
    ("w below the minimum",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 3, "h": 10}]}),
    ("thickness too thick",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10,
                                          "thickness": 6, "pad": 0}]}),
    ("bad shape",
        lambda: {"at": 0, "highlights": [{"shape": "triangle", "x": 0, "y": 0, "w": 10, "h": 10}]}),
    ("bad colour name",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10,
                                          "color": "reddish"}]}),
    ("colour with alpha",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10,
                                          "color": "red@0.5"}]}),
    ("empty label",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10,
                                          "label": ""}]}),
    ("label over 80 characters",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10,
                                          "label": "x" * 81}]}),
    ("label is not a string",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10,
                                          "label": 5}]}),
    ("too many highlights",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 10 * i, "y": 10, "w": 8, "h": 8}
                                         for i in range(13)]}),
    ("dim without highlights",
        lambda: {"at": 0, "dim": 0.5}),
    ("dim above the maximum",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10}],
                 "dim": 1.5}),
    ("crop margin and x together",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10}],
                 "crop": {"margin": 5, "x": 0}}),
    ("crop margin without highlights",
        lambda: {"at": 0, "crop": {"margin": 5}}),
    ("highlight outside an explicit crop",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10}],
                 "crop": {"x": 50, "y": 50, "w": 20, "h": 20}}),
    ("crop below the minimum size",
        lambda: {"at": 0, "crop": {"x": 0, "y": 0, "w": 10, "h": 10}}),
    ("highlight inside an explicit crop but its pad outside it",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 50, "y": 50, "w": 20, "h": 20}],
                 "crop": {"x": 48, "y": 48, "w": 24, "h": 24}}),
    ("pad negative",
        lambda: {"at": 0, "pad": -1, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10}]}),
    ("pad per highlight too large",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10, "pad": 401}]}),
    ("pad is not a whole number",
        lambda: {"at": 0, "pad": 2.5, "highlights": [{"shape": "box", "x": 0, "y": 0, "w": 10, "h": 10}]}),
    ("grid too low",
        lambda: {"at": 0, "grid": 1}),
    ("grid too high",
        lambda: {"at": 0, "grid": 30}),
    ("grid is not a number",
        lambda: {"at": 0, "grid": "lots"}),
    ("max_width below the minimum",
        lambda: {"at": 0, "max_width": 10}),
    ("boolean where a pixel is expected",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": True, "y": 0, "w": 10, "h": 10}]}),
    ("percentage over 100",
        lambda: {"at": 0, "highlights": [{"shape": "box", "x": "120%", "y": 0, "w": 10, "h": 10}]}),
    ("at past the last frame",
        lambda: {"at": 30}),
    ("at is a boolean",
        lambda: {"at": True}),
    ("spec is a JSON list",
        lambda: [{"at": 0}]),
]

BAD_SPEC_NEEDLES = [
    "unknown key", "unknown key", "must be a list", "expected an object",
    "missing", "must be between 0 and 640", "runs past", "at least 4x4",
    "too thick", "not valid", "not a colour", "alpha", "must not be empty",
    "under 80 characters", "must be a string", "at most 12", "nothing to leave bright",
    "must be between 0 and 0.95", "not both", "needs \"highlights\"", "outside the crop",
    "at least 16x16", "grown by its pad", "must be between 0 and 400", "must be between 0 and 400",
    "whole number", "whole number between 2 and 25", "whole number between 2 and 25",
    "not a number", "must be between 64 and 8192", "boolean", "must be between 0% and 100%",
    "past the last frame", "boolean", "must be a JSON object",
]

assert len(BAD_SPECS) == len(BAD_SPEC_NEEDLES)


@pytest.mark.parametrize("build,needle", [
    pytest.param(b, n, id=i) for (i, b), n in zip(BAD_SPECS, BAD_SPEC_NEEDLES)
])
def test_bad_still_spec_is_rejected_cleanly(tmp_path, media, build, needle):
    out = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, build(), out)

    assert proc.returncode != 0, f"expected a failure, got success:\n{proc.stderr}"
    assert "Traceback" not in proc.stderr, f"leaked a traceback:\n{proc.stderr}"
    assert needle in proc.stderr, f"expected {needle!r} in:\n{proc.stderr}"
    assert not out.exists(), "a rejected spec still produced an output file"


def test_a_good_still_spec_still_succeeds(tmp_path, media):
    """Guards the table above against becoming vacuously true."""
    out = render_still(tmp_path, media.video, {"at": 0})
    assert resolution(out) == (640, 360)


# ---- image vs. video: "at" is exactly backwards for the two -----------------------------

def test_at_is_forbidden_for_an_image(tmp_path, media):
    out = tmp_path / "out.png"
    proc = run_still(tmp_path, media.image, {"at": 0}, out)
    assert proc.returncode != 0
    assert "does not apply" in proc.stderr
    assert not out.exists()


def test_image_input_needs_no_at_and_keeps_its_resolution(tmp_path, media):
    out = render_still(tmp_path, media.image,
                       {"highlights": [{"shape": "box", "x": 100, "y": 100, "w": 200, "h": 150,
                                        "color": "yellow"}]})
    assert resolution(out) == (900, 900)


def test_video_input_requires_at(tmp_path, media):
    out = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, {}, out)
    assert proc.returncode != 0
    assert '"at"' in proc.stderr
    assert not out.exists()


def test_at_accepts_several_time_formats(tmp_path, media):
    for at in (3, "3", "0:03", "3s"):
        proc = run_still(tmp_path, media.video, {"at": at}, tmp_path / "out.png", "--dry-run")
        assert proc.returncode == 0, f"at={at!r}: {proc.stderr}"


# ---- output contract: stdout, suffix, overwrite guard, missing spec ---------------------

def test_stdout_on_success_is_only_the_output_path(tmp_path, media):
    out = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, {"at": 0}, out, "-q")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(out)


def test_dry_run_shows_command_and_writes_nothing(tmp_path, media):
    out = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, {"at": 0}, out, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert not out.exists()
    assert proc.stdout == ""
    assert "command" in proc.stderr
    assert "dry run: nothing was written" in proc.stderr


def test_output_suffix_must_be_jpg_or_png(tmp_path, media):
    out = tmp_path / "out.gif"
    proc = run_still(tmp_path, media.video, {"at": 0}, out)
    assert proc.returncode != 0
    assert ".jpg or .png" in proc.stderr
    assert not out.exists()


def test_writing_over_the_source_is_refused(tmp_path, media):
    """The image case: same suffix as the source, so the suffix check does not mask it."""
    import shutil
    source = tmp_path / "slide.png"
    shutil.copy(media.image, source)
    before = source.read_bytes()

    proc = run_still(tmp_path, source, {}, source)

    assert proc.returncode != 0
    assert "refusing" in proc.stderr
    assert source.read_bytes() == before, "the source file was modified"


def test_missing_spec_file_is_rejected(tmp_path, media):
    out = tmp_path / "out.png"
    proc = subprocess.run(
        [sys.executable, "-m", "vedit.cli", "still", str(media.video),
         str(tmp_path / "nope.json"), "-o", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "does not exist" in proc.stderr
    assert not out.exists()


# ---- geometry and pixels: no highlights -------------------------------------------------

def test_plain_grab_matches_the_source_frame(tmp_path, media):
    out = render_still(tmp_path, media.video, {"at": 0})
    for x, y in [(0, 0), (320, 180), (639, 359), (100, 250), (500, 50)]:
        assert_rgb(pixel(out, 0, x, y), pixel(media.video, 0, x, y), msg=f"({x},{y})")


# ---- box highlight: ring, interior, and untouched neighbours -----------------------------

def test_box_highlight_ring_and_interior(tmp_path, media):
    r = BOX
    t = r["thickness"]
    blue = (0, 0, 255)
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})

    # Ring pixels: forced to the highlight colour.
    assert_rgb(pixel(out, 0, r["x"], r["y"]), blue, msg="top-left corner")
    assert_rgb(pixel(out, 0, r["x"] + r["w"] - 1, r["y"] + r["h"] - 1), blue,
               msg="bottom-right corner")
    assert_rgb(pixel(out, 0, r["x"] + t - 1, r["y"] + r["h"] // 2), blue,
               msg="ring, mid-left edge")

    # Just inside the ring: untouched, equals the source.
    inside = (r["x"] + t, r["y"] + t)
    assert_rgb(pixel(out, 0, *inside), pixel(media.video, 0, *inside), msg="just inside the ring")

    # Just outside the box on both sides: untouched.
    left = (r["x"] - 1, r["y"])
    right = (r["x"] + r["w"], r["y"])
    assert_rgb(pixel(out, 0, *left), pixel(media.video, 0, *left), msg="just outside, left")
    assert_rgb(pixel(out, 0, *right), pixel(media.video, 0, *right), msg="just outside, right")

    # Far from the highlight: matches the plain grab (no dim was requested).
    assert_rgb(pixel(out, 0, 500, 300), pixel(media.video, 0, 500, 300), msg="far from the box")


# ---- pad: the outline stands off the named rectangle ------------------------------------

def test_pad_draws_the_ring_outside_the_target_by_default(tmp_path, media):
    r = {k: v for k, v in BOX.items() if k != "pad"}
    blue = (0, 0, 255)
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})

    p = DEFAULT_PAD
    assert_rgb(pixel(out, 0, r["x"] - p, r["y"] - p), blue, msg="ring, pad px outside the target")
    assert_rgb(pixel(out, 0, r["x"] + r["w"] - 1 + p, r["y"] + r["h"] - 1 + p), blue,
               msg="ring, bottom-right, pad px outside")
    for name, pt in {"target corner": (r["x"], r["y"]),
                     "target edge": (r["x"] + r["w"] // 2, r["y"]),
                     "outside the ring": (r["x"] - p - 1, r["y"] - p - 1)}.items():
        assert_rgb(pixel(out, 0, *pt), pixel(media.video, 0, *pt), msg=name)


def test_pad_can_be_set_per_still_and_per_highlight(tmp_path, media):
    a = {k: v for k, v in BOX.items() if k != "pad"}
    b = {"shape": "box", "x": 400, "y": 200, "w": 60, "h": 40, "thickness": 4, "color": "#0000ff",
         "pad": 0}
    out = render_still(tmp_path, media.video, {"at": 0, "pad": 12, "highlights": [a, b]})
    blue = (0, 0, 255)
    assert_rgb(pixel(out, 0, a["x"] - 12, a["y"] - 12), blue, msg="top-level pad applies")
    assert_rgb(pixel(out, 0, a["x"], a["y"]), pixel(media.video, 0, a["x"], a["y"]),
               msg="a's own corner untouched")
    assert_rgb(pixel(out, 0, b["x"], b["y"]), blue, msg="per-highlight pad 0 overrides")


def test_pad_is_clamped_at_the_frame_edge(tmp_path, media):
    r = {"shape": "box", "x": 0, "y": 30, "w": 40, "h": 20, "thickness": 3, "color": "#0000ff",
         "pad": 10}
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})
    blue = (0, 0, 255)
    assert_rgb(pixel(out, 0, 0, 20), blue, msg="ring hugs the left edge instead of going negative")
    assert_rgb(pixel(out, 0, 49, 20), blue, msg="right edge still padded")


def test_pad_clamps_at_the_right_and_bottom_edges_too(tmp_path, media):
    r = {"shape": "box", "x": 600, "y": 330, "w": 40, "h": 30, "thickness": 3, "color": "#0000ff",
         "pad": 10}
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})
    blue = (0, 0, 255)
    assert_rgb(pixel(out, 0, 639, 359), blue, msg="ring closes on the frame's last pixel")
    assert_rgb(pixel(out, 0, 590, 320), blue, msg="top-left corner padded normally")


def test_pad_grows_an_ellipse_without_moving_its_centre(tmp_path, media):
    r = {k: v for k, v in ELLIPSE.items() if k != "pad"}
    red = (255, 0, 0)
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})
    p = DEFAULT_PAD
    centre = (r["x"] + r["w"] // 2, r["y"] + r["h"] // 2)
    assert_rgb(pixel(out, 0, r["x"] - p, centre[1]), red, msg="leftmost point moved out by pad")
    assert_rgb(pixel(out, 0, r["x"], centre[1]), pixel(media.video, 0, r["x"], centre[1]),
               msg="the target's own leftmost point is now inside the ring")
    assert_rgb(pixel(out, 0, *centre), pixel(media.video, 0, *centre), msg="centre untouched")


def test_a_padded_ellipse_at_the_frame_edge_keeps_its_shape(tmp_path, media):
    # Grown by 10 the bounding box would start at y = -8; the ellipse must keep its centre
    # (the leftmost point stays on the centre row) rather than be squashed into the frame.
    r = {"shape": "ellipse", "x": 330, "y": 2, "w": 80, "h": 80, "thickness": 4, "color": "red",
         "pad": 10}
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})
    red = (255, 0, 0)
    cy = r["y"] + r["h"] // 2
    assert_rgb(pixel(out, 0, r["x"] - 10, cy), red, msg="leftmost point on the true centre row")
    assert_rgb(pixel(out, 0, r["x"] - 10, cy - 12), pixel(media.video, 0, r["x"] - 10, cy - 12),
               msg="a squashed ellipse would have put ring here")


def test_explicit_crop_may_equal_the_padded_extent_exactly(tmp_path, media):
    r = {k: v for k, v in BOX.items() if k != "pad"}
    p = 9
    crop = {"x": r["x"] - p, "y": r["y"] - p, "w": r["w"] + 2 * p, "h": r["h"] + 2 * p}
    out = render_still(tmp_path, media.video, {"at": 0, "pad": p, "highlights": [r], "crop": crop})
    assert resolution(out) == (crop["w"], crop["h"])
    assert_rgb(pixel(out, 0, 0, 0), (0, 0, 255), msg="ring corner at the crop origin")


def test_thickness_is_judged_against_the_padded_rectangle(tmp_path, media):
    # 10x10 with thickness 6 is "too thick" unpadded (see BAD_SPECS); with the default pad
    # the ring is drawn on a 26x26 rectangle and is fine.
    r = {"shape": "box", "x": 100, "y": 100, "w": 10, "h": 10, "thickness": 6, "color": "#0000ff"}
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})
    assert_rgb(pixel(out, 0, 100 - DEFAULT_PAD, 100 - DEFAULT_PAD), (0, 0, 255), msg="ring drawn")


def test_pad_appears_in_the_plan_and_crop_margin_includes_it(tmp_path, media):
    r = {k: v for k, v in BOX.items() if k != "pad"}
    out_path = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, {"at": 0, "highlights": [r], "pad": 9,
                                             "crop": {"margin": 30}}, out_path)
    assert proc.returncode == 0, proc.stderr
    assert f"pad 9 -> x {r['x'] - 9} y {r['y'] - 9} w {r['w'] + 18} h {r['h'] + 18}" in proc.stderr
    assert resolution(out_path) == (r["w"] + 2 * (30 + 9), r["h"] + 2 * (30 + 9))


# ---- ellipse highlight: centre, leftmost point, and the untouched corner ----------------

def test_ellipse_highlight_centre_edge_and_corner(tmp_path, media):
    r = ELLIPSE
    red = (255, 0, 0)
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [r]})

    centre = (r["x"] + r["w"] // 2, r["y"] + r["h"] // 2)
    leftmost = (r["x"], r["y"] + r["h"] // 2)
    corner = (r["x"], r["y"])

    assert_rgb(pixel(out, 0, *centre), pixel(media.video, 0, *centre), msg="ellipse centre")
    assert_rgb(pixel(out, 0, *leftmost), red, msg="ellipse leftmost point")
    assert_rgb(pixel(out, 0, *corner), pixel(media.video, 0, *corner),
               msg="ellipse bounding-box corner (must not be reached)")


# ---- dim: brightness scaled outside highlights, untouched inside ------------------------

def test_dim_scales_brightness_outside_the_box_only(tmp_path, media):
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [BOX], "dim": 0.5})

    outside = (10, 10)
    src = pixel(media.video, 0, *outside)
    expected = tuple(round(c * 0.5) for c in src)
    assert_rgb(pixel(out, 0, *outside), expected, msg="outside the box, dimmed")

    inside = (BOX["x"] + BOX["w"] // 2, BOX["y"] + BOX["h"] // 2)
    assert_rgb(pixel(out, 0, *inside), pixel(media.video, 0, *inside), msg="inside the box, undimmed")


# ---- crop: exact resolution, origin mapping, and highlight offset -----------------------

def test_crop_rect_maps_the_origin_and_offsets_the_highlight(tmp_path, media):
    crop = {"x": 100, "y": 70, "w": 200, "h": 150}
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [BOX], "crop": crop})

    assert resolution(out) == (crop["w"], crop["h"])
    assert_rgb(pixel(out, 0, 0, 0), pixel(media.video, 0, crop["x"], crop["y"]),
               msg="output origin")

    ring = (BOX["x"] - crop["x"], BOX["y"] - crop["y"])
    assert_rgb(pixel(out, 0, *ring), (0, 0, 255), msg="ring, offset into the crop")


def test_crop_margin_boxes_the_highlights(tmp_path, media):
    margin = 30
    out = render_still(tmp_path, media.video, {"at": 0, "highlights": [BOX],
                                               "crop": {"margin": margin}})
    assert resolution(out) == (BOX["w"] + 2 * margin, BOX["h"] + 2 * margin)


def test_crop_margin_clamps_to_the_frame(tmp_path, media):
    margin = 30
    corner_box = {"shape": "box", "x": 10, "y": 10, "w": 80, "h": 60, "color": "blue", "pad": 0}
    out_path = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video,
                     {"at": 0, "highlights": [corner_box], "crop": {"margin": margin}}, out_path)
    assert proc.returncode == 0, proc.stderr
    assert "clamped to the frame" in proc.stderr

    # x0 = 10-30 clamps to 0, y0 likewise; x1 = 10+80+30 = 120, y1 = 10+60+30 = 100.
    assert resolution(out_path) == (120, 100)


# ---- max_width: downscale only, never upscale --------------------------------------------

def test_max_width_downscales_and_keeps_aspect(tmp_path, media):
    out = render_still(tmp_path, media.video, {"at": 0, "max_width": 320})
    assert resolution(out) == (320, 180)


def test_max_width_larger_than_the_frame_is_a_no_op(tmp_path, media):
    out_path = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, {"at": 0, "max_width": 800}, out_path)
    assert proc.returncode == 0, proc.stderr
    assert "scale" not in proc.stderr
    assert resolution(out_path) == (640, 360)


# ---- grid: line pixels, and off-line pixels stay source ----------------------------------

def test_grid_lines_land_on_the_predicted_pixels(tmp_path, media):
    out = render_still(tmp_path, media.video, {"at": 0, "grid": 4})
    assert_rgb(pixel(out, 0, 160, 100), (255, 220, 0), msg="on a vertical grid line")
    assert_rgb(pixel(out, 0, 100, 100), pixel(media.video, 0, 100, 100), msg="off any grid line")


def test_grid_true_means_ten_divisions(tmp_path, media):
    out_path = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, {"at": 0, "grid": True}, out_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "10 divisions" in proc.stderr


# ---- percentages resolve against the real frame size -------------------------------------

def test_percentage_coordinates_resolve_against_the_frame(tmp_path, media):
    spec = {"at": 0, "highlights": [{"shape": "box", "x": "50%", "y": "25%", "w": "10%",
                                     "h": "10%", "color": "lime", "thickness": 3, "pad": 0}]}
    out = render_still(tmp_path, media.video, spec)
    # 50% of 640 = 320, 25% of 360 = 90: the box's top-left corner, forced to lime.
    assert_rgb(pixel(out, 0, 320, 90), (0, 255, 0), msg="percentage-positioned ring")


# ---- labels: plan names them, and they visibly darken a region --------------------------

def test_label_text_appears_in_the_plan(tmp_path, media):
    spec = {"at": 0, "highlights": [{"shape": "box", "x": 125, "y": 90, "w": 80, "h": 60,
                                     "label": "1. Download"}]}
    out_path = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, spec, out_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "'1. Download'" in proc.stderr


def test_label_darkens_the_region_above_the_box(tmp_path, media):
    spec = {"at": 0, "highlights": [{"shape": "box", "x": 125, "y": 90, "w": 80, "h": 60,
                                     "label": "Hello there"}]}
    out = render_still(tmp_path, media.video, spec)
    region = (125, 50, 205, 86)  # just above the box, inside the label's textbox
    before = region_mean(media.video, 0, *region)
    after = region_mean(out, 0, *region)
    assert after < before - 10, f"label textbox did not darken the region above ({after} vs {before})"


def test_label_drops_below_the_box_when_there_is_no_room_above(tmp_path, media):
    spec = {"at": 0, "highlights": [{"shape": "box", "x": 125, "y": 0, "w": 80, "h": 60,
                                     "label": "Top"}]}
    out = render_still(tmp_path, media.video, spec)
    region = (125, 64, 205, 100)  # just below the box, since it sits at the top edge
    before = region_mean(media.video, 0, *region)
    after = region_mean(out, 0, *region)
    assert after < before - 10, f"label textbox did not darken the region below ({after} vs {before})"


# ---- dry-run estimate vs. the real render -------------------------------------------------

def test_dry_run_estimate_matches_the_real_render(tmp_path, media):
    spec = {"at": 0, "highlights": [BOX], "crop": {"margin": 50}, "max_width": 100}
    out_path = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, spec, out_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr

    import re
    match = re.search(r"^estimate\s+(\d+)x(\d+)$", proc.stderr, re.MULTILINE)
    assert match, f"no estimate line found in:\n{proc.stderr}"
    estimate = (int(match.group(1)), int(match.group(2)))

    real = render_still(tmp_path, media.video, spec, name="real.png")
    assert resolution(real) == estimate


# ---- the printed example must validate against a real fixture ---------------------------

def test_the_printed_still_example_validates_for_the_fixture(tmp_path, media):
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "example", "--still"],
                          capture_output=True, text=True, check=True)
    example = json.loads(proc.stdout)
    example["at"] = 5  # the shipped "2:29" only fits a real lecture, not the 30s fixture

    out_path = tmp_path / "out.png"
    proc = run_still(tmp_path, media.video, example, out_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr


# ---- safety: a failed render leaves no staging file behind ------------------------------

def test_a_failed_still_render_leaves_no_debris(tmp_path, media):
    """A component of the output path is a plain file, so `mkdir(parents=True)` fails.

    Cleanly, via cli.py's OSError handler -- no traceback, and no `.vedit-*` staging
    file, since the failure happens before ffmpeg ever runs.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    out = blocker / "sub" / "out.png"

    proc = run_still(tmp_path, media.video, {"at": 0}, out)

    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert not out.exists()
    assert not list(tmp_path.rglob(".vedit-*")), "a staging file was left behind"


# ---- the .check measuring twin --------------------------------------------------------
#
# Every render with highlights (and no explicit grid) writes `<stem>.check<suffix>`
# beside the output: the same still with the labelled coordinate grid drawn over the
# highlights, so the verification read doubles as the measuring pass. The clean file
# is what gets embedded; the twin exists so a missed box can be corrected from the
# grid labels instead of by trial and error.

GRID_RGB = (255, 220, 0)  # still_mod.GRID_RGB


def test_highlights_write_a_gridded_check_twin(media, tmp_path):
    out = tmp_path / "step.png"
    proc = run_still(tmp_path, media.video, {"at": 1, "highlights": [BOX]}, out)
    assert proc.returncode == 0, proc.stderr
    check = tmp_path / "step.check.png"
    assert check.exists(), "no .check twin was written"
    assert "step.check.png" in proc.stderr
    assert resolution(check) == resolution(out)
    # The vertical grid line at x=192 (640/10 * 3) crosses the BOX interior; y=120 is
    # clear of the ring (border 125..129), the horizontal line at 108..109, and every
    # label. On the twin that pixel is grid-coloured; on the clean file it is the flat
    # green testsrc2 patch under the box.
    assert_rgb(pixel(check, 0, 192, 120), GRID_RGB)
    clean = pixel(out, 0, 192, 120)
    assert max(abs(a - b) for a, b in zip(clean, GRID_RGB)) > 3 * PIX_TOL


def test_no_check_twin_without_highlights_or_with_explicit_grid(media, tmp_path):
    plain = tmp_path / "plain.png"
    proc = run_still(tmp_path, media.video, {"at": 1}, plain)
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "plain.check.png").exists()

    gridded = tmp_path / "gridded.png"
    proc = run_still(tmp_path, media.video, {"at": 1, "highlights": [BOX], "grid": True}, gridded)
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "gridded.check.png").exists()


# ---- the sidecar: the spec written back beside each rendered image ----------------------

def run_still_spec_file(workdir: Path, source: Path, spec_path: Path, out: Path,
                        *extra: str) -> subprocess.CompletedProcess:
    """Like run_still, but with the spec already on disk (e.g. a sidecar)."""
    return subprocess.run(
        [sys.executable, "-m", "vedit.cli", "still", str(source), str(spec_path),
         "-o", str(out), *extra],
        capture_output=True, text=True,
    )


def test_sidecar_written_and_reproduces_the_still(media, tmp_path):
    out = tmp_path / "step.png"
    spec = {"at": 1, "highlights": [BOX], "dim": 0.2}
    proc = run_still(tmp_path, media.video, spec, out)
    assert proc.returncode == 0, proc.stderr
    sidecar = tmp_path / "step.json"
    assert sidecar.exists(), "no sidecar beside the output"
    assert "step.json" in proc.stderr
    record = json.loads(sidecar.read_text())
    assert record["at"] == 1
    assert record["highlights"] == [BOX]
    assert record["dim"] == 0.2
    assert record["source"] == str(media.video)
    assert record["output"] == str(out)
    assert not (tmp_path / "step.check.json").exists(), "the .check twin gets no sidecar"

    # The sidecar is itself a valid spec (source/output are accepted-and-ignored) and
    # reproduces the image byte-for-byte.
    again = tmp_path / "again.png"
    proc2 = run_still_spec_file(tmp_path, media.video, sidecar, again)
    assert proc2.returncode == 0, proc2.stderr
    assert again.read_bytes() == out.read_bytes()


def test_sidecar_does_not_clobber_a_spec_at_its_own_path(media, tmp_path):
    # run_still writes the spec to <workdir>/spec.json; naming the output spec.png makes
    # that very file the sidecar path. The hand-written spec must survive untouched.
    spec = {"at": 1, "highlights": [BOX]}
    proc = run_still(tmp_path, media.video, spec, tmp_path / "spec.png")
    assert proc.returncode == 0, proc.stderr
    assert json.loads((tmp_path / "spec.json").read_text()) == spec
