"""Markup tests for `vedit.review_page.render_page` — the page half of Phase 7/8.

Builds duck-typed `Step`/`Shot` objects with `types.SimpleNamespace` (per the frozen
contract) so this module never imports from `vedit.review`. No video, no server, no ffmpeg.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from vedit.review_page import render_page


def make_shot(key, caption, image, *, at, candidates=None, thumbs=None, plain="", boxes=None, crop=None):
    candidates = candidates or []
    thumbs = thumbs or []
    return types.SimpleNamespace(
        key=key,
        caption=caption,
        image=Path(image),
        sidecar=Path(image).with_suffix(".json"),
        spec={"at": at} if at is not None else None,
        at=at,
        attrs="",
        line=0,
        candidates=candidates,
        plain=plain,
        thumbs=thumbs,
        boxes=boxes or [],
        crop=crop,
    )


def make_step(index, heading, shots, div=None):
    return types.SimpleNamespace(index=index, heading=heading, shots=shots, div=div)


class FakeInfo:
    def __init__(self, width, height):
        self.width = width
        self.height = height


@pytest.fixture
def steps(tmp_path):
    img_dir = tmp_path / "guide-img"
    img_dir.mkdir()

    shot_1a = make_shot(
        "a", "First shot", img_dir / "step-01-a.jpg", at=64.0,
        candidates=[61.0, 65.0, 72.6],
        thumbs=["cand-01-0.jpg", "cand-01-1.jpg", "cand-01-2.jpg"],
        plain="plain-01-0.jpg",
        boxes=[
            {"x": 360, "y": 678, "w": 168, "h": 28, "label": "Download"},
            {"x": 10, "y": 20, "w": 30, "h": 40},
        ],
        crop={"x": 200, "y": 400, "w": 900, "h": 500},
    )
    shot_1b = make_shot("b", "Second shot", img_dir / "step-01-b.jpg", at=70.0,
                         candidates=[67.0], thumbs=["cand-01b-0.jpg"], plain="plain-01-1.jpg")
    step1 = make_step(1, "Step one --- setup", [shot_1a, shot_1b])

    weird_label = "</SCRIPT><b>Weird</b>"
    shot_2a = make_shot(
        "a", "Caption with <b>bold</b> markup", img_dir / "step-02-a.jpg", at=64.0,
        candidates=[60.0], thumbs=["cand-02-0.jpg"], plain="plain-02-0.jpg",
        boxes=[{"x": 1, "y": 2, "w": 3, "h": 4, "label": weird_label}],
        crop={"margin": 160},
    )
    shot_2b = make_shot("b", "No sidecar shot", img_dir / "step-02-b.jpg", at=None)
    step2 = make_step(2, "Step two --- no sidecar", [shot_2a, shot_2b])

    return [step1, step2]


@pytest.fixture
def info():
    return FakeInfo(1920, 1080)


@pytest.fixture
def source(tmp_path):
    return tmp_path / "source.mp4"


def _seed(out, step, shot):
    m = re.search(
        r'<script type="application/json" class="seed" data-step="%d" data-shot="%d">(.*?)</script>' % (step, shot),
        out, re.S,
    )
    assert m, f"no seed script for step {step} shot {shot}"
    return json.loads(m.group(1))


def test_still_and_plain_ids(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert 'id="still-1-0"' in out
    assert 'id="still-1-1"' in out
    assert 'id="plain-1-0"' in out
    assert 'id="status-2-0"' in out


def test_moment_buttons_have_data_attrs_and_label(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert re.search(
        r'<button class="moment" data-step="1" data-shot="0" data-at="61">Use 1:01\.0 \(61s\)</button>',
        out,
    )
    assert re.search(
        r'<button class="moment" data-step="1" data-shot="0" data-at="72\.6">Use 1:12\.6 \(72\.6s\)</button>',
        out,
    )
    # generic shape check for every candidate button rendered
    for m in re.finditer(r'<button class="moment"[^>]*>(Use [^<]+)</button>', out):
        assert re.match(r'Use \d+:\d{2}\.\d \(\d+(?:\.\d+)?s\)', m.group(1))


def test_tool_buttons_and_crop_shortcuts(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert out.count('<button class="tool active" data-tool="box"') == 3   # one per sidecar'd shot
    assert out.count('<button class="tool" data-tool="crop"') == 3
    assert 'class="tool active" data-tool="box"' in out
    assert 'class="nocrop"' in out
    assert 'class="margincrop"' in out


def test_mode_toggle_and_new_image_controls(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert 'class="mode active" data-mode="edit" data-step="1"' in out
    assert 'class="mode" data-mode="new" data-step="1"' in out
    assert 'class="draft" data-step="1" data-shot="new"' in out
    assert 'class="caption" data-step="1"' in out
    assert 'class="addimage" data-step="1"' in out
    # nothing has been staged yet for the new-image line
    assert "nothing staged yet" in out


def test_no_sidecar_shot_has_no_controls(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert "No sidecar for this image" in out
    # step 2 shot 1 (index 1, "b") has no `at`: no plain/status/tool/action/draft markup
    # for it, even though its <figure> wrapper still legitimately carries data-step/shot.
    assert 'id="plain-2-1"' not in out
    assert 'id="status-2-1"' not in out
    assert 'class="label" data-step="2" data-shot="1"' not in out
    assert 'data-tool="box" data-step="2" data-shot="1"' not in out
    assert 'class="keep" data-step="2" data-shot="1"' not in out
    assert 'class="apply" data-step="2" data-shot="1"' not in out
    assert 'class="undobox" data-step="2" data-shot="1"' not in out
    assert 'class="clearboxes" data-step="2" data-shot="1"' not in out
    assert 'class="draft" data-step="2" data-shot="1"' not in out
    assert re.search(r'class="seed"[^>]*data-step="2" data-shot="1"', out) is None


def test_apply_undo_clear_buttons_present(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    for step, shot in [(1, 0), (1, 1), (2, 0)]:
        assert f'<button class="apply" data-step="{step}" data-shot="{shot}">Apply</button>' in out
        assert f'<button class="undobox" data-step="{step}" data-shot="{shot}">Undo box</button>' in out
        assert f'<button class="clearboxes" data-step="{step}" data-shot="{shot}">Clear boxes</button>' in out


def test_seed_json_round_trips_boxes_and_crop(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    seed = _seed(out, 1, 0)
    shot = steps[0].shots[0]
    assert seed["at"] == shot.at
    assert seed["boxes"] == shot.boxes
    assert seed["crop"] == shot.crop


def test_seed_json_survives_a_label_with_script_close_and_html(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    seed = _seed(out, 2, 0)
    shot = steps[1].shots[0]
    assert seed["boxes"] == shot.boxes
    assert seed["boxes"][0]["label"] == "</SCRIPT><b>Weird</b>"
    raw = re.search(r'class="seed" data-step="2" data-shot="0">(.*?)</script>', out, re.S).group(1)
    assert "<" not in raw                                # every < is \u003c, whatever the tag's case
    assert seed["crop"] == {"margin": 160}


def test_no_sidecar_shot_has_no_seed(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert not re.search(r'class="seed"[^>]*data-step="2" data-shot="1"', out)


def test_draft_summary_line_format(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert (
        '<p class="draft" data-step="1" data-shot="0">'
        'moment 64 s · 2 boxes · crop 200,400 900x500 · saved</p>'
    ) in out
    assert (
        '<p class="draft" data-step="1" data-shot="1">'
        'moment 70 s · 0 boxes · crop none · saved</p>'
    ) in out
    assert (
        '<p class="draft" data-step="2" data-shot="0">'
        'moment 64 s · 1 box · crop margin · saved</p>'
    ) in out


def test_caption_is_escaped(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert "&lt;b&gt;bold&lt;/b&gt;" in out
    assert "<b>bold</b>" not in out


def test_shots_grid_and_step_ids(steps, tmp_path, info, source):
    out = render_page(steps, tmp_path, info, source, serve=True)
    assert '<section class="step" id="step-1" data-step="1">' in out
    assert '<section class="step" id="step-2" data-step="2">' in out
    assert 'class="shots" style="--cols:2"' in out


def test_serve_flag_and_frame_dims(steps, tmp_path, info, source):
    served = render_page(steps, tmp_path, info, source, serve=True)
    static = render_page(steps, tmp_path, info, source, serve=False)
    assert "const SERVE = true;" in served
    assert "const SERVE = false;" in static
    assert f"const FRAME_W = {info.width}, FRAME_H = {info.height};" in served


def test_node_check_on_extracted_script(steps, tmp_path, info, source):
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    out = render_page(steps, tmp_path, info, source, serve=True)
    m = re.search(r"<script>(.*)</script>\s*$", out, re.S)
    assert m, "no <script> block found"
    script_path = tmp_path / "review.js"
    script_path.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(script_path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
