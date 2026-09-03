"""`vedit review`: the click-driven review page and the decisions it applies.

A guide is built against the synthetic clip: a boxed still with an explicit crop, a
plain still, an image with no sidecar, a still whose crop already contains any nearby
box, and a `::: {layout-ncol=2}` div holding two shots. Page rendering itself
(`src/vedit/review_page.py`) is a separate module owned elsewhere; these tests only
check that `build()` produces a non-empty page, not its markup.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vedit import VeditError, review
from util import ffmpeg, resolution

HAS_TESSERACT = shutil.which("tesseract") is not None


def make_still(tmp_path: Path, video: Path, name: str, spec: dict) -> Path:
    img_dir = tmp_path / "g-guide-img"
    img_dir.mkdir(exist_ok=True)
    spec_file = tmp_path / f"{name}.json"
    spec_file.write_text(json.dumps(spec))
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(video), str(spec_file),
                           "-o", str(img_dir / f"{name}.png"), "-q"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return img_dir / f"{name}.png"


def make_text_video(tmp_path: Path) -> Path:
    """An 800x400 clip whose only readable text ("Download RStudio") is on screen for the
    first 3 of its 6 seconds, so a moment past t=3 can't re-resolve a text anchor there."""
    video = tmp_path / "text.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=white:s=800x400:r=5:d=6",
           "-vf", "drawtext=text='Download RStudio':x=100:y=200:fontsize=30:fontcolor=black:"
                  "enable=between(t\\,0\\,3)",
           "-pix_fmt", "yuv420p", str(video))
    return video


def make_guide(tmp_path: Path, video: Path) -> Path:
    """Steps in document order: 1 box+crop, 2 plain, 3 no-sidecar, 4 crop-already-wide,
    5 a two-shot `layout-ncol` div."""
    for name, spec in (
        ("step-01-box", {"at": 2, "highlights": [{"x": 125, "y": 90, "w": 80, "h": 60,
                                                   "label": "Click here"}],
                         "dim": 0.3, "crop": {"x": 60, "y": 40, "w": 260, "h": 200}}),
        ("step-02-plain", {"at": 12}),
        ("step-03-contain", {"at": 5, "highlights": [{"x": 125, "y": 90, "w": 40, "h": 30}],
                             "dim": 0.3, "crop": {"x": 0, "y": 0, "w": 300, "h": 300}}),
        ("step-04-a", {"at": 8}),
        ("step-04-b", {"at": 15}),
    ):
        make_still(tmp_path, video, name, spec)
    (tmp_path / "scenes.txt").write_text("0.5 0.9\n4.0 0.2\n9.5 0.3\n20.0 0.1\n")
    qmd = tmp_path / "g-guide.qmd"
    qmd.write_text(
        "---\ntitle: t\n---\n\n"
        "## 0:00 --- Start\n\n"
        "### Step 1 --- Click the thing\n\nText.\n\n"
        '![The thing, boxed](g-guide-img/step-01-box.png){fig-alt="a"}\n\n'
        "### Step 2 --- Look at the screen\n\n"
        "![The whole screen](g-guide-img/step-02-plain.png)\n\n"
        "![No sidecar](g-guide-img/missing.png)\n\n"
        "### Step 3 --- Already cropped wide\n\n"
        '![Contained](g-guide-img/step-03-contain.png){fig-alt="c"}\n\n'
        "### Step 4 --- Two shots\n\n"
        "::: {layout-ncol=2}\n"
        '![Shot A](g-guide-img/step-04-a.png){fig-alt="a"}\n\n'
        '![Shot B](g-guide-img/step-04-b.png){fig-alt="b"}\n'
        ":::\n"
    )
    return qmd


def test_parse_guide_lists_images_and_groups_a_layout_div(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    steps = review.parse_guide(qmd)
    assert [s.index for s in steps] == [1, 2, 3, 4, 5]
    assert steps[0].heading == "Step 1 --- Click the thing"
    assert steps[0].shots[0].caption == "The thing, boxed" and steps[0].shots[0].key == "a"
    assert steps[0].shots[0].at == 2 and steps[0].div is None
    assert steps[1].shots[0].at == 12
    assert steps[2].shots[0].spec is None and steps[2].shots[0].at is None   # no sidecar

    grouped = steps[4]
    assert [sh.key for sh in grouped.shots] == ["a", "b"]
    assert grouped.shots[0].caption == "Shot A" and grouped.shots[1].caption == "Shot B"
    assert grouped.div is not None
    lines = qmd.read_text().splitlines()
    open_line, close_line = grouped.div
    assert "layout-ncol" in lines[open_line] and lines[close_line].strip() == ":::"
    assert grouped.shots[0].line == open_line + 1


def test_candidate_times_bracket_the_moment_and_follow_scene_changes():
    scenes = [0.5, 4.0, 9.5, 20.0]
    assert review.candidate_times(12.0, scenes, 30.0) == [9.0, 10.5, 15.0, 21.0]
    assert review.candidate_times(0.2, [], 30.0) == [3.0]                # nothing before 0
    assert review.candidate_times(29.95, [], 30.0) == [27.0]             # nothing past the end
    assert 12.0 not in review.candidate_times(12.0, [11.8], 30.0)        # never the moment itself


def test_missing_images_in_the_guide_are_an_error(tmp_path):
    (tmp_path / "empty.qmd").write_text("# nothing\n")
    with pytest.raises(VeditError, match="embeds no images"):
        review.parse_guide(tmp_path / "empty.qmd")


def test_build_writes_page_frames_and_manifest(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    page = review.build(qmd, out, width=320)
    assert page.name == "review.html" and page.exists() and page.read_text().strip()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["width"] == 640 and manifest["height"] == 360
    assert manifest["page_width"] == 320 and manifest["serve"] is False
    steps = manifest["steps"]
    assert steps["1"]["shots"][0]["at"] == 2 and steps["1"]["div"] is None
    assert steps["2"]["shots"][0]["at"] == 12
    assert steps["3"]["shots"][0]["at"] is None
    grouped = steps["5"]
    assert grouped["div"] is not None and [sh["key"] for sh in grouped["shots"]] == ["a", "b"]
    assert (out / "frame-2.jpg").exists()
    assert resolution(out / "frame-2.jpg") == (320, 180)
    assert len(list(out.glob("thumb-*.jpg"))) >= 2


def test_frame_cache_is_reused_across_builds(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    frames = sorted(out.glob("frame-*.jpg")) + sorted(out.glob("thumb-*.jpg"))
    assert frames
    before = {f: f.stat().st_mtime_ns for f in frames}
    review.build(qmd, out, width=320)
    after_frames = sorted(out.glob("frame-*.jpg")) + sorted(out.glob("thumb-*.jpg"))
    assert after_frames == frames                                         # no new files
    assert {f: f.stat().st_mtime_ns for f in after_frames} == before      # none re-extracted


def test_moment_decision_rewrites_at_and_rerenders(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    before = (tmp_path / "g-guide-img" / "step-02-plain.png").read_bytes()
    image, message = review.apply_decision(manifest, 2, {"action": "moment", "at": 20.5})
    assert "20.5s" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())
    assert spec["at"] == 20.5 and spec["source"]
    assert image.read_bytes() != before                                  # a different frame


def test_box_decision_replaces_highlights_and_grows_a_crop_that_no_longer_fits(tmp_path, media):
    """The fixture clip is 640x360, so the mirrored still.py pad rule
    (max(6, round(min(W,H)/48))) gives pad=8; the box {400,200,100,50} pads out to
    {392,192,116,66}, and the old crop {60,40,260,200} is grown just enough to hold it."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    image, message = review.apply_decision(
        manifest, 1, {"action": "box", "box": {"x": 400, "y": 200, "w": 100, "h": 50}, "label": "Here"})
    spec = json.loads((tmp_path / "g-guide-img" / "step-01-box.json").read_text())
    assert spec["highlights"] == [{"x": 400, "y": 200, "w": 100, "h": 50, "label": "Here"}]
    assert spec["crop"] == {"x": 60, "y": 40, "w": 448, "h": 218}        # grown, not replaced with a margin
    assert "box set to x 400" in message and "crop grown to x 60 y 40 w 448 h 218" in message
    assert image.exists()
    # Without a label the previous one carries over.
    review.apply_decision(manifest, 1, {"action": "box", "box": {"x": 10, "y": 10, "w": 50, "h": 40}})
    spec = json.loads((tmp_path / "g-guide-img" / "step-01-box.json").read_text())
    assert spec["highlights"][0]["label"] == "Here"


def test_box_decision_leaves_an_explicit_crop_alone_when_it_still_contains_the_box(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    review.apply_decision(manifest, 4, {"action": "box", "box": {"x": 150, "y": 150, "w": 50, "h": 50}})
    spec = json.loads((tmp_path / "g-guide-img" / "step-03-contain.json").read_text())
    assert spec["crop"] == {"x": 0, "y": 0, "w": 300, "h": 300}          # unchanged: still contains it
    assert spec["highlights"] == [{"x": 150, "y": 150, "w": 50, "h": 50}]


def test_crop_decision_sets_and_removes_an_explicit_crop(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    review.apply_decision(manifest, 4, {"action": "crop", "crop": {"margin": 50}})
    spec = json.loads((tmp_path / "g-guide-img" / "step-03-contain.json").read_text())
    assert spec["crop"]["margin"] == 50
    review.apply_decision(manifest, 4, {"action": "crop", "crop": None})
    spec = json.loads((tmp_path / "g-guide-img" / "step-03-contain.json").read_text())
    assert "crop" not in spec


def test_crop_decision_grows_a_rect_that_would_exclude_the_highlight(tmp_path, media):
    """Phase 7 let `vedit still` refuse a too-small explicit crop; Phase 8 grows it first,
    so the render always succeeds. Step 4's highlight is {125,90,40,30}; pad=8 (640x360
    fixture) pads it to {117,82,56,46}; the drawn crop {0,0,40,30} is grown to hold that."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    image, message = review.apply_decision(
        manifest, 4, {"action": "crop", "crop": {"x": 0, "y": 0, "w": 40, "h": 30}})
    spec = json.loads((tmp_path / "g-guide-img" / "step-03-contain.json").read_text())
    assert spec["crop"] == {"x": 0, "y": 0, "w": 173, "h": 128}
    assert "crop grown to x 0 y 0 w 173 h 128" in message and image.exists()


def test_bad_decisions_are_refused(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    with pytest.raises(VeditError, match="numeric 'at'"):
        review.apply_decision(manifest, 2, {"action": "moment"})
    with pytest.raises(VeditError, match="no sidecar"):
        review.apply_decision(manifest, 3, {"action": "keep"})
    with pytest.raises(VeditError, match="unknown decision"):
        review.apply_decision(manifest, 1, {"action": "dance"})
    assert review.apply_decision(manifest, 1, {"action": "keep"})[1] == "kept"
    with pytest.raises(VeditError, match="not in the review manifest"):
        review.apply_decision(manifest, 99, {"action": "keep"})
    with pytest.raises(VeditError, match="no shot index"):
        review.apply_decision(manifest, 1, {"action": "keep", "shot": 5})


def test_add_image_creates_then_extends_a_layout_div_and_refuses_a_fourth(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    original_lines = qmd.read_text().splitlines()

    image_b, message_b = review.add_image(manifest, 2, {"at": 6, "caption": "Second angle"})
    assert image_b.name == "step-02-plain-b.png" and image_b.exists()
    assert "added step-02-plain-b.png" in message_b
    after_first = qmd.read_text()
    assert "::: {layout-ncol=2}" in after_first
    assert "g-guide-img/step-02-plain-b.png" in after_first

    review.build(qmd, out, width=320)                                    # reload before the next add
    manifest = json.loads((out / "manifest.json").read_text())
    step2 = manifest["steps"]["2"]
    assert len(step2["shots"]) == 2 and step2["div"] is not None

    image_c, message_c = review.add_image(manifest, 2, {"at": 9})
    assert image_c.name == "step-02-plain-c.png" and "added step-02-plain-c.png" in message_c
    after_second_lines = qmd.read_text().splitlines()
    assert any("layout-ncol=3" in l for l in after_second_lines)

    # Everything outside the touched div is byte-identical: the file before step 2's image
    # and the file from step 3's image onward never moved.
    prefix_end = next(i for i, l in enumerate(original_lines) if "step-02-plain.png" in l)
    assert original_lines[:prefix_end] == after_second_lines[:prefix_end]
    suffix_before = next(i for i, l in enumerate(original_lines) if "step-03-contain.png" in l)
    suffix_after = next(i for i, l in enumerate(after_second_lines) if "step-03-contain.png" in l)
    assert original_lines[suffix_before:] == after_second_lines[suffix_after:]

    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    with pytest.raises(VeditError, match="at most 3"):
        review.add_image(manifest, 2, {"at": 11})


def test_apply_all_replays_a_mixed_ordered_log(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    log = {
        "1": [{"action": "keep", "shot": 0}],
        "2": [{"action": "moment", "at": 20, "shot": 0},
              {"action": "add", "at": 25, "caption": "Zoomed"}],
        "3": [{"action": "keep", "shot": 0, "note": "fine"}],
    }
    (out / "review.json").write_text(json.dumps(log))
    lines = review.apply_all(out)
    assert lines == [
        "step 2: moment set to 20s -> step-02-plain.png",
        "step 2: added step-02-plain-b.png",
        "step 3: note only",
    ]
    assert json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())["at"] == 20
    assert (tmp_path / "g-guide-img" / "step-02-plain-b.png").exists()
    assert "g-guide-img/step-02-plain-b.png" in qmd.read_text()


def test_served_page_applies_decisions_and_frames_over_http(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320, serve=True)
    server = review.serve(out, 0, quiet=True)
    port = server.server_address[1]
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert page.strip()
        still = urllib.request.urlopen(f"http://127.0.0.1:{port}/../g-guide-img/step-01-box.png")
        assert still.status == 200 and still.headers["content-type"] == "image/png"

        # /frame?at= extracts on demand and is then cached (same bytes, same mtime).
        frame_bytes_1 = urllib.request.urlopen(f"http://127.0.0.1:{port}/frame?at=6").read()
        frame_file = out / "frame-6.jpg"
        assert frame_file.exists()
        mtime = frame_file.stat().st_mtime_ns
        frame_bytes_2 = urllib.request.urlopen(f"http://127.0.0.1:{port}/frame?at=6").read()
        assert frame_bytes_1 == frame_bytes_2 and frame_file.stat().st_mtime_ns == mtime

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/frame?at=notanumber")
        assert exc.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/frame?at=9999")
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/review.json")
        assert exc.value.code == 404                                     # nothing decided yet

        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/decide", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())

        reply = post({"step": 2, "shot": 0, "action": "moment", "at": 6})
        assert reply["ok"] and reply["step"] == 2 and reply["shot"] == 0
        assert reply["image"].endswith("step-02-plain.png")
        log = json.loads((out / "review.json").read_text())
        assert log == {"2": [{"shot": 0, "action": "moment", "at": 6}]}

        post({"step": 2, "shot": 0, "action": "keep", "note": "still good"})
        log = json.loads((out / "review.json").read_text())
        assert len(log["2"]) == 2                                        # the log grows as a list
        remote = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/review.json").read())
        assert remote == log

        bad = post({"step": 3, "shot": 0, "action": "keep"})
        assert not bad["ok"] and "no sidecar" in bad["error"]
        log = json.loads((out / "review.json").read_text())
        assert log["3"][0]["error"] == bad["error"]                     # kept, marked, skipped by --apply

        add_reply = post({"step": 2, "action": "add", "at": 9, "caption": "Second angle"})
        assert add_reply["ok"] and add_reply["reload"] is True
        manifest = json.loads((out / "manifest.json").read_text())
        assert len(manifest["steps"]["2"]["shots"]) == 2                 # rebuilt with the new shot

        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../../etc/passwd")
    finally:
        server.shutdown()


def test_cli_builds_the_page(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "review", str(qmd), "--width", "320"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(tmp_path / "g-guide-review" / "review.html")
    assert "vedit review" in proc.stderr and "--apply" in proc.stderr


def test_apply_all_skips_refused_decisions_and_lands_a_second_add_after_the_first(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    log = {"2": [{"action": "crop", "shot": 0, "crop": {"x": 0, "y": 0, "w": 10, "h": 10},
                  "error": "re-render failed: error: crop excludes the highlight"},
                 {"action": "add", "at": 25, "caption": "Second"},
                 {"action": "add", "at": 27, "caption": "Third", "crop": None, "box": {"x": 10, "y": 10, "w": 50, "h": 40}}]}
    (out / "review.json").write_text(json.dumps(log))
    lines = review.apply_all(out)
    assert lines[0].startswith("step 2: skipped crop (refused when made")
    assert lines[1:] == ["step 2: added step-02-plain-b.png", "step 2: added step-02-plain-c.png"]
    text = qmd.read_text()
    assert text.count(":::") == 4 and text.count("layout-ncol=3") == 1   # the fixture's div plus one new, extended once
    assert text.index("step-02-plain-b.png") < text.index("step-02-plain-c.png")
    third = json.loads((tmp_path / "g-guide-img" / "step-02-plain-c.json").read_text())
    assert "crop" not in third and third["highlights"][0]["w"] == 50    # explicit null: no crop


def test_add_image_preserves_crlf_and_refuses_a_guide_that_moved(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    crlf = qmd.read_bytes().replace(b"\n", b"\r\n")
    qmd.write_bytes(crlf)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    review.add_image(manifest, 2, {"at": 25})
    after = qmd.read_bytes()
    assert b"\r\n" in after and b"\n" not in after.replace(b"\r\n", b"")   # still CRLF throughout
    assert after.replace(b"step-02-plain-b", b"").count(b"\r\n") >= crlf.count(b"\r\n")
    # Someone edits the guide above the step: the manifest's line numbers are stale.
    qmd.write_bytes(b"extra line\r\n" + after)
    with pytest.raises(VeditError, match="changed since the review page was built"):
        review.add_image(manifest, 1, {"at": 25})


def test_review_folder_outside_the_guide_folder_still_serves_its_page_and_frames(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path.parent / f"{tmp_path.name}-rev"      # a sibling whose name starts with the guide folder's
    review.build(qmd, out, width=320, serve=True)
    server = review.serve(out, 0, quiet=True)
    port = server.server_address[1]
    try:
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/").status == 200
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/frame-12.jpg").status == 200
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert f'src="../{tmp_path.name}/g-guide-img/step-01-box.png"' in page   # how the page reaches the guide folder
        still = urllib.request.urlopen(f"http://127.0.0.1:{port}/../{tmp_path.name}/g-guide-img/step-01-box.png")
        assert still.status == 200
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../../etc/passwd")
    finally:
        server.shutdown()


def test_add_image_skips_a_suffix_whose_sidecar_or_check_image_exists(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    img = tmp_path / "g-guide-img"
    (img / "step-02-plain-b.json").write_text("{}")                     # an orphan sidecar
    (img / "step-02-plain-c.check.png").write_bytes(b"")               # an orphan measuring twin
    new_image, _ = review.add_image(manifest, 2, {"at": 25})
    assert new_image.name == "step-02-plain-d.png"
    assert (img / "step-02-plain-b.json").read_text() == "{}"


# ---- Phase 8: `edit`, several boxes, crop auto-grow, anchor drop on moment change ----------

def test_edit_with_at_boxes_and_crop_renders_once(tmp_path, media, monkeypatch):
    """A single Apply carrying a new moment, two boxes and a crop that already contains
    both (pad=8 on this 640x360 fixture) must hit `vedit still` exactly once."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())

    calls = []
    original_render = review._render

    def counting_render(video, spec, image):
        calls.append(1)
        return original_render(video, spec, image)

    monkeypatch.setattr(review, "_render", counting_render)

    image, message = review.apply_decision(manifest, 2, {
        "action": "edit", "at": 10,
        "boxes": [{"x": 50, "y": 50, "w": 40, "h": 30, "label": "A"},
                  {"x": 300, "y": 250, "w": 60, "h": 40, "label": "B"}],
        "crop": {"x": 0, "y": 0, "w": 400, "h": 340},
    })
    assert len(calls) == 1
    assert "moment set to 10s" in message and "boxes set (2)" in message
    assert "crop set to x 0 y 0 w 400 h 340" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())
    assert spec["at"] == 10
    assert spec["highlights"] == [
        {"x": 50, "y": 50, "w": 40, "h": 30, "label": "A"},
        {"x": 300, "y": 250, "w": 60, "h": 40, "label": "B"},
    ]
    assert spec["crop"] == {"x": 0, "y": 0, "w": 400, "h": 340}
    assert image.exists()


def test_edit_boxes_empty_removes_highlights_and_a_margin_crop(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())

    review.apply_decision(manifest, 1, {"action": "crop", "crop": {"margin": 50}})
    spec = json.loads((tmp_path / "g-guide-img" / "step-01-box.json").read_text())
    assert spec["crop"] == {"margin": 50}

    image, message = review.apply_decision(manifest, 1, {"action": "edit", "boxes": []})
    assert "highlights removed" in message and "no highlights to bound the margin" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-01-box.json").read_text())
    assert spec["highlights"] == [] and "crop" not in spec and "dim" not in spec
    assert image.exists()


def test_apply_all_replays_an_edit_decision(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    log = {"2": [{"action": "edit", "shot": 0, "at": 20,
                  "boxes": [{"x": 10, "y": 10, "w": 30, "h": 20}]}]}
    (out / "review.json").write_text(json.dumps(log))
    lines = review.apply_all(out)
    assert lines == ["step 2: moment set to 20s; box set to x 10 y 10 w 30 h 20 -> step-02-plain.png"]
    spec = json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())
    assert spec["at"] == 20 and spec["highlights"] == [{"x": 10, "y": 10, "w": 30, "h": 20}]


def test_legacy_moment_box_and_crop_actions_still_apply(tmp_path, media):
    """Old review.json logs (pre-Phase-8) replay unchanged: moment/box/crop are `edit`
    with one field under the hood."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    review.apply_decision(manifest, 2, {"action": "moment", "at": 7})
    review.apply_decision(manifest, 2, {"action": "box", "box": {"x": 5, "y": 5, "w": 20, "h": 15}})
    review.apply_decision(manifest, 2, {"action": "crop", "crop": {"margin": 30}})
    spec = json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())
    assert spec["at"] == 7
    assert spec["highlights"] == [{"x": 5, "y": 5, "w": 20, "h": 15}]
    assert spec["crop"]["margin"] == 30


def test_served_edit_decision_returns_the_sidecars_at_boxes_and_crop(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320, serve=True)
    server = review.serve(out, 0, quiet=True)
    port = server.server_address[1]
    try:
        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/decide", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())

        reply = post({"step": 2, "shot": 0, "action": "edit", "at": 6,
                      "boxes": [{"x": 20, "y": 20, "w": 30, "h": 20, "label": "X"}]})
        assert reply["ok"]
        assert reply["at"] == 6
        assert reply["boxes"] == [{"x": 20, "y": 20, "w": 30, "h": 20, "label": "X"}]
        assert reply["crop"] is None
    finally:
        server.shutdown()


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_edit_moment_drops_a_text_anchor_that_is_absent_on_the_new_frame(tmp_path):
    """The anchor is on screen only for t < 3; moving to t=5 can't re-resolve it, so it is
    dropped (not fatal) and reported, while a plain measured box beside it survives."""
    video = make_text_video(tmp_path)
    make_still(tmp_path, video, "step-01-anchor", {
        "at": 1,
        "highlights": [{"text": "Download RStudio"}, {"x": 500, "y": 300, "w": 40, "h": 30}],
        "dim": 0.3,
    })
    qmd = tmp_path / "t-guide.qmd"
    qmd.write_text("---\ntitle: t\n---\n\n![Shot](g-guide-img/step-01-anchor.png)\n")
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())

    image, message = review.apply_decision(manifest, 1, {"action": "edit", "at": 5})
    assert "moment set to 5s" in message
    assert "'Download RStudio' is not on this frame" in message and "was removed" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-01-anchor.json").read_text())
    assert spec["at"] == 5
    assert [hi for hi in spec["highlights"] if "text" in hi] == []       # the anchor is gone
    assert any(hi.get("x") == 500 for hi in spec["highlights"])          # the plain box survived
    assert image.exists()


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_edit_moment_on_a_still_whose_only_highlight_is_an_anchor_leaves_a_plain_frame(tmp_path):
    """The take 6 step-16 case: one text anchor, dim, margin crop. Off the frame, the anchor
    goes -- and with it the dim and the margin crop, which `vedit still` refuses without
    highlights -- so the moment still changes and the user draws a box next."""
    video = make_text_video(tmp_path)
    make_still(tmp_path, video, "step-01-only", {
        "at": 1, "highlights": [{"text": "Download RStudio", "label": "Click"}],
        "dim": 0.3, "crop": {"margin": 40},
    })
    qmd = tmp_path / "t-guide.qmd"
    qmd.write_text("---\ntitle: t\n---\n\n![Shot](g-guide-img/step-01-only.png)\n")
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    image, message = review.apply_decision(manifest, 1, {"action": "edit", "at": 5})
    assert "moment set to 5s" in message and "was removed" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-01-only.json").read_text())
    assert spec["at"] == 5 and not spec.get("highlights") and "dim" not in spec and "crop" not in spec
    assert image.exists()


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_shot_boxes_includes_a_text_anchors_resolved_rect(tmp_path):
    video = make_text_video(tmp_path)
    make_still(tmp_path, video, "step-01-anchor", {
        "at": 1, "highlights": [{"text": "Download RStudio", "label": "Get it"}], "dim": 0.3,
    })
    qmd = tmp_path / "t-guide.qmd"
    qmd.write_text("---\ntitle: t\n---\n\n![Shot](g-guide-img/step-01-anchor.png)\n")
    steps = review.parse_guide(qmd)
    boxes = steps[0].shots[0].boxes
    assert len(boxes) == 1 and boxes[0]["label"] == "Get it"
    assert boxes[0]["w"] > 0 and boxes[0]["h"] > 0
    resolved = steps[0].shots[0].spec["highlights"][0]["resolved"]
    assert boxes[0] == {**resolved, "label": "Get it"}


def test_anchor_miss_is_recognised_whatever_quotes_the_text_carries():
    m = review.ANCHOR_NOT_FOUND_RE.search(
        "re-render failed: error: highlights[1].text: \"Don't create a Start Menu folder\" was not found on screen. The closest")
    assert m and m.group("index") == "1"


def test_edit_clearing_boxes_and_setting_a_margin_crop_together_leaves_no_crop(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    _, message = review.apply_decision(manifest, 1, {"action": "edit", "boxes": [], "crop": {"margin": 40}})
    spec = json.loads((tmp_path / "g-guide-img" / "step-01-box.json").read_text())
    assert not spec.get("highlights") and "crop" not in spec and "dim" not in spec
    assert "crop removed (no highlights" in message


# ---- Phase 9: arrows in the boxes vocabulary, and remove ----------------------------------

def test_parse_boxes_accepts_plain_boxes_and_arrows(tmp_path):
    boxes = review._parse_boxes([
        {"x": 1, "y": 2, "w": 3, "h": 4, "label": "A"},
        {"shape": "arrow", "x1": 10, "y1": 20, "x2": 30, "y2": 40, "label": "B"},
    ])
    assert boxes == [
        {"x": 1, "y": 2, "w": 3, "h": 4, "label": "A"},
        {"shape": "arrow", "x1": 10, "y1": 20, "x2": 30, "y2": 40, "label": "B"},
    ]


def test_parse_boxes_rejects_bad_items():
    error = r'boxes\[0\] needs x, y, w, h \(or shape "arrow" with x1, y1, x2, y2\)'
    with pytest.raises(VeditError, match=error):
        review._parse_boxes([{"x": 1, "y": 1, "w": 1}])            # missing h
    with pytest.raises(VeditError, match=error):
        review._parse_boxes(["not a dict"])
    with pytest.raises(VeditError, match=error):
        review._parse_boxes([{"shape": "arrow", "x1": 1, "y1": 1, "x2": 2}])   # missing y2
    with pytest.raises(VeditError, match=r'boxes\[1\] needs'):
        review._parse_boxes([{"x": 1, "y": 1, "w": 1, "h": 1},
                             {"shape": "arrow", "x1": 1, "y1": 1}])


def test_shot_boxes_includes_a_measured_arrow():
    spec = {"highlights": [
        {"shape": "arrow", "x1": 10, "y1": 20, "x2": 30, "y2": 40, "label": "Click"},
        {"x": 5, "y": 5, "w": 10, "h": 10},
    ]}
    assert review._shot_boxes(spec) == [
        {"shape": "arrow", "x1": 10, "y1": 20, "x2": 30, "y2": 40, "label": "Click"},
        {"x": 5, "y": 5, "w": 10, "h": 10},
    ]


def test_shot_boxes_uses_a_text_arrows_cached_line_and_skips_an_unresolved_one():
    spec = {"highlights": [
        {"shape": "arrow", "text": "Install", "from": "left", "line": {"x1": 1, "y1": 2, "x2": 3, "y2": 4}},
        {"shape": "arrow", "text": "Next"},          # not yet resolved: no x1..y2, no line
    ]}
    assert review._shot_boxes(spec) == [{"shape": "arrow", "x1": 1, "y1": 2, "x2": 3, "y2": 4}]


def test_boxes_message_counts_by_kind():
    box = {"x": 1, "y": 2, "w": 3, "h": 4}
    arrow = {"shape": "arrow", "x1": 1, "y1": 2, "x2": 3, "y2": 4}
    assert review._boxes_message([box]) == "box set to x 1 y 2 w 3 h 4"
    assert review._boxes_message([arrow]) == "arrow set from x 1 y 2 to x 3 y 4"
    assert review._boxes_message([box, box]) == "boxes set (2)"          # unchanged all-box wording
    assert review._boxes_message([box, arrow]) == "boxes set (1 box, 1 arrow)"
    assert review._boxes_message([box, box, arrow, arrow]) == "boxes set (2 boxes, 2 arrows)"
    assert review._boxes_message([arrow, arrow]) == "boxes set (2 arrows)"


def test_edit_with_arrow_writes_sidecar_with_no_dim_and_reply_carries_it(tmp_path, media):
    """Arrow-only highlights leave nothing bright to dim around. Depends on Builder A's
    still.py arrow support to actually render."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    image, message = review.apply_decision(manifest, 2, {
        "action": "edit",
        "boxes": [{"shape": "arrow", "x1": 50, "y1": 50, "x2": 150, "y2": 120, "label": "Click"}],
    })
    assert "arrow set from x 50 y 50 to x 150 y 120" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())
    assert spec["highlights"] == [{"shape": "arrow", "x1": 50, "y1": 50, "x2": 150, "y2": 120, "label": "Click"}]
    assert "dim" not in spec
    assert image.exists()


def test_edit_with_mixed_box_and_arrow_sets_dim(tmp_path, media):
    """Depends on Builder A's still.py arrow support to actually render."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    image, message = review.apply_decision(manifest, 2, {
        "action": "edit",
        "boxes": [{"x": 50, "y": 50, "w": 40, "h": 30},
                  {"shape": "arrow", "x1": 200, "y1": 200, "x2": 260, "y2": 260}],
    })
    assert "boxes set (1 box, 1 arrow)" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())
    assert spec["dim"] == 0.35
    assert image.exists()


def test_edit_arrow_crop_grows_to_the_arrows_extent(tmp_path, media):
    """`_highlight_extents`/`_grow_crop` via `still.arrow_extent`/`arrow_thickness`
    (Builder A). Depends on Builder A's still.py arrow support to actually render."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    image, message = review.apply_decision(manifest, 2, {
        "action": "edit",
        "boxes": [{"shape": "arrow", "x1": 500, "y1": 300, "x2": 400, "y2": 200}],
        "crop": {"x": 0, "y": 0, "w": 50, "h": 50},
    })
    assert "crop grown to" in message
    spec = json.loads((tmp_path / "g-guide-img" / "step-02-plain.json").read_text())
    crop = spec["crop"]
    assert crop["x"] <= 400 and crop["y"] <= 200
    assert crop["x"] + crop["w"] >= 500 and crop["y"] + crop["h"] >= 300
    assert image.exists()


def test_served_edit_decision_reply_carries_an_arrow(tmp_path, media):
    """Depends on Builder A's still.py arrow support to actually render."""
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320, serve=True)
    server = review.serve(out, 0, quiet=True)
    port = server.server_address[1]
    try:
        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/decide", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())

        reply = post({"step": 2, "shot": 0, "action": "edit",
                      "boxes": [{"shape": "arrow", "x1": 10, "y1": 10, "x2": 80, "y2": 90, "label": "Here"}]})
        assert reply["ok"]
        assert reply["boxes"] == [{"shape": "arrow", "x1": 10, "y1": 10, "x2": 80, "y2": 90, "label": "Here"}]
    finally:
        server.shutdown()


def test_remove_image_refuses_a_one_shot_step_and_a_bad_index(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    with pytest.raises(VeditError, match="only one image"):
        review.remove_image(manifest, 2, {"shot": 0})
    with pytest.raises(VeditError, match="no shot index"):
        review.remove_image(manifest, 5, {"shot": 5})
    with pytest.raises(VeditError, match="not in the review manifest"):
        review.remove_image(manifest, 99, {"shot": 0})


def test_remove_image_unwraps_the_div_and_moves_the_files(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    before_lines = qmd.read_text().splitlines()
    a_line = next(l for l in before_lines if "step-04-a.png" in l)

    moved, message = review.remove_image(manifest, 5, {"shot": 1})     # remove Shot B
    assert message == "removed step-04-b.png (moved to review/removed/)"
    removed_dir = out / "removed"
    assert moved == removed_dir / "step-04-b.png" and moved.exists()
    assert (removed_dir / "step-04-b.json").exists()                  # sidecar moved too
    assert not (tmp_path / "g-guide-img" / "step-04-b.png").exists()  # never deleted, moved

    after_lines = qmd.read_text().splitlines()
    assert "layout-ncol" not in qmd.read_text()                       # one shot left: div unwrapped
    assert not any("step-04-b.png" in l for l in after_lines)
    assert a_line in after_lines                                      # the surviving image line, untouched


def test_remove_image_decrements_ncol_from_a_three_shot_div(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    third, _ = review.add_image(manifest, 5, {"at": 18, "caption": "Shot C"})
    review.build(qmd, out, width=320)                                 # reload: 3 shots now
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["steps"]["5"]["shots"]) == 3

    moved, _ = review.remove_image(manifest, 5, {"shot": 1})          # remove the middle shot
    assert moved.name == "step-04-b.png"
    text = qmd.read_text()
    assert "layout-ncol=2" in text and "layout-ncol=3" not in text
    assert "step-04-a.png" in text and "step-04-b.png" not in text and third.name in text


def test_remove_image_appends_a_suffix_on_a_name_collision(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    removed_dir = out / "removed"
    removed_dir.mkdir()
    (removed_dir / "step-04-b.png").write_bytes(b"already here")

    moved, _ = review.remove_image(manifest, 5, {"shot": 1})
    assert moved.name == "step-04-b-1.png" and moved.exists()
    assert (removed_dir / "step-04-b.png").read_bytes() == b"already here"   # untouched
    assert (removed_dir / "step-04-b-1.json").exists()      # the bundle shares one suffix


def test_remove_image_refuses_a_stale_index_an_outside_image_and_a_changed_div(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    with pytest.raises(VeditError, match="is now step-04-b.png, not step-04-c.png"):
        review.remove_image(manifest, 5, {"shot": 1, "image": "step-04-c.png"})
    outside = tmp_path.parent / "elsewhere.png"        # the guide folder is tmp_path itself
    stale = json.loads(json.dumps(manifest))
    stale["steps"]["5"]["shots"][1]["image"] = str(outside)
    with pytest.raises(VeditError, match="outside the guide folder"):
        review.remove_image(stale, 5, {"shot": 1})
    # an image line added by hand inside the div, without moving the target's own line
    lines = review._guide_lines(qmd)
    open_line, close_line = manifest["steps"]["5"]["div"]
    blank = next(j for j in range(open_line + 1, close_line) if lines[j].strip() == "")
    lines[blank] = "![Shot D](g-guide-img/step-04-a.png)"       # same line count, one more image
    qmd.write_text("\n".join(lines))
    with pytest.raises(VeditError, match="now holds 3 images, the page knew 2"):
        review.remove_image(manifest, 5, {"shot": 0})
    assert (tmp_path / "g-guide-img" / "step-04-a.png").exists()     # nothing moved on a refusal


def test_remove_image_preserves_crlf(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    crlf = qmd.read_bytes().replace(b"\n", b"\r\n")
    qmd.write_bytes(crlf)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    review.remove_image(manifest, 5, {"shot": 1})
    after = qmd.read_bytes()
    assert b"\r\n" in after and b"\n" not in after.replace(b"\r\n", b"")
    assert b"layout-ncol" not in after


def test_remove_image_refuses_when_the_guide_has_changed(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    qmd.write_text("extra line\n" + qmd.read_text())
    with pytest.raises(VeditError, match="changed since the review page was built"):
        review.remove_image(manifest, 5, {"shot": 1})


def test_apply_all_replays_an_add_then_a_remove(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    log = {"5": [{"action": "add", "at": 18, "caption": "Shot C"},
                 {"action": "remove", "shot": 1}]}
    (out / "review.json").write_text(json.dumps(log))
    lines = review.apply_all(out)
    assert lines[0].startswith("step 5: added ")
    assert lines[1] == "step 5: removed step-04-b.png (moved to review/removed/)"
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["steps"]["5"]["shots"]) == 2
    text = qmd.read_text()
    assert "layout-ncol=2" in text and "step-04-b.png" not in text


def test_served_remove_reloads_and_a_second_removal_refuses(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320, serve=True)
    server = review.serve(out, 0, quiet=True)
    port = server.server_address[1]
    try:
        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/decide", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())

        reply = post({"step": 5, "shot": 1, "action": "remove"})
        assert reply["ok"] and reply["reload"] is True
        assert reply["message"] == "removed step-04-b.png (moved to review/removed/)"
        manifest = json.loads((out / "manifest.json").read_text())
        assert len(manifest["steps"]["5"]["shots"]) == 1              # rebuilt, div unwrapped
        log = json.loads((out / "review.json").read_text())
        assert log["5"][0]["action"] == "remove"

        bad = post({"step": 2, "shot": 0, "action": "remove"})        # step 2 has only one shot
        assert not bad["ok"] and "only one image" in bad["error"]
        log = json.loads((out / "review.json").read_text())
        assert log["2"][0]["error"] == bad["error"]
    finally:
        server.shutdown()


def test_add_image_caption_cannot_break_the_image_line(tmp_path, media):
    qmd = make_guide(tmp_path, media.video)
    out = tmp_path / "rev"
    review.build(qmd, out, width=320)
    manifest = json.loads((out / "manifest.json").read_text())
    review.add_image(manifest, 2, {"at": 25, "caption": 'Click "Next" [twice]\nthen wait'})
    line = next(l for l in qmd.read_text().splitlines() if "step-02-plain-b" in l)
    assert line == '![Click "Next" (twice) then wait](g-guide-img/step-02-plain-b.png){fig-alt="Click \'Next\' (twice) then wait"}'
