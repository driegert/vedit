"""The HTML/CSS/JS half of `vedit review` — the click-driven page itself.

Kept separate from `review.py` (which owns parsing, the manifest, and the HTTP server) so
the two halves can change independently. This module is duck-typed against the `Step`/
`Shot` shapes documented in the Phase 7/8 contracts and must not import from `.review`.

Layout, in order, per step: a mode toggle (Edit an existing shot / stage a New image) and
a note field, then the shots side by side in `.shots` (one column per shot, reflowing to a
single column under ~900px). Each shot is: the current still with a busy overlay for the
re-render round trip, the plain drawing frame + canvas (box tool red-solid, crop tool
blue-dashed), the candidate-moment thumbnails, the box/crop tool buttons, a one-line draft
summary, and keep/apply/label/status.

Phase 8: every sidecar'd shot gets a client-side **draft** — `{at, boxes, crop}` seeded
from the shot's own fields via a per-shot inline JSON blob (`<script type="application/
json" class="seed">`) — so a moment click, several box drags, and a crop drag accumulate
locally and go out as ONE `edit` decision on Apply, instead of one render per drag. New-
image mode stages the same draft shape (empty, no sidecar backing) and posts it as `add`.
"""

from __future__ import annotations

import html
import json
import os

MARGIN_CROP = 160


def _clock(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


def _boxes_label(n: int) -> str:
    return f"{n} box" if n == 1 else f"{n} boxes"


def _crop_label(crop) -> str:
    if crop is None:
        return "crop none"
    if "margin" in crop:
        return "crop margin"
    return f"crop {crop['x']},{crop['y']} {crop['w']}x{crop['h']}"


def _draft_line(at, boxes, crop, *, saved: bool) -> str:
    if at is None:
        return "nothing staged yet — click a moment above"
    parts = [f"moment {at:g} s", _boxes_label(len(boxes)), _crop_label(crop), "saved" if saved else "unsaved"]
    return " · ".join(parts)


def _seed_json(shot) -> str:
    """A per-shot draft seed, safe to embed verbatim inside `<script type="application/json">`.

    JSON text can contain a literal `</script` (e.g. inside a box label) that would end the
    tag early no matter what `type` it declares — the HTML tokenizer doesn't care. Escaping
    every `<` as the JSON escape `\\u003c` keeps the tag intact whatever the case of the tag
    (`</SCRIPT>` ends it too); `JSON.parse` decodes it back for free.
    """
    payload = {"at": shot.at, "boxes": list(getattr(shot, "boxes", None) or []), "crop": getattr(shot, "crop", None)}
    return json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")


def _shot_html(step_idx: int, shot_idx: int, shot, rel) -> str:
    esc = html.escape
    sid = f"{step_idx}-{shot_idx}"
    still_src = esc(rel(shot.image))

    if shot.at is None:
        return f'''
      <figure class="shot" data-step="{step_idx}" data-shot="{shot_idx}">
        <div class="stillwrap"><img id="still-{sid}" src="{still_src}"></div>
        <figcaption class="caption">{esc(shot.caption)}</figcaption>
        <p class="hint">No sidecar for this image, so no moment or box to change.</p>
      </figure>'''

    boxes = list(getattr(shot, "boxes", None) or [])
    crop = getattr(shot, "crop", None)
    cand_figs = "".join(
        f'<figure class="cand"><img src="{esc(th)}" loading="lazy">'
        f'<figcaption><button class="moment" data-step="{step_idx}" data-shot="{shot_idx}" '
        f'data-at="{t:g}">Use {_clock(t)} ({t:g}s)</button></figcaption></figure>'
        for th, t in zip(shot.thumbs, shot.candidates)
    )
    at_meta = f'at {_clock(shot.at)} ({shot.at:g}s)'
    draft_line = esc(_draft_line(shot.at, boxes, crop, saved=True))
    return f'''
      <figure class="shot" data-step="{step_idx}" data-shot="{shot_idx}">
        <div class="stillwrap">
          <img id="still-{sid}" src="{still_src}">
          <div class="rerender" data-step="{step_idx}" data-shot="{shot_idx}"><span>re-rendering&hellip;</span></div>
        </div>
        <figcaption class="caption">{esc(shot.caption)} <span class="meta">{esc(shot.image.name)} &middot; {at_meta}</span></figcaption>
        <div class="draw" data-step="{step_idx}" data-shot="{shot_idx}">
          <img id="plain-{sid}" src="{esc(shot.plain)}">
          <canvas></canvas>
        </div>
        <script type="application/json" class="seed" data-step="{step_idx}" data-shot="{shot_idx}">{_seed_json(shot)}</script>
        <p class="hint">Drag to draw a box (red) or the crop (blue, dashed); coordinates convert to full-frame pixels. Press Apply to save.</p>
        <div class="cands">{cand_figs}</div>
        <div class="tools">
          <button class="tool active" data-tool="box" data-step="{step_idx}" data-shot="{shot_idx}">Box</button>
          <button class="tool" data-tool="crop" data-step="{step_idx}" data-shot="{shot_idx}">Crop</button>
          <button class="nocrop" data-step="{step_idx}" data-shot="{shot_idx}">No crop</button>
          <button class="margincrop" data-step="{step_idx}" data-shot="{shot_idx}">Margin crop</button>
          <button class="undobox" data-step="{step_idx}" data-shot="{shot_idx}">Undo box</button>
          <button class="clearboxes" data-step="{step_idx}" data-shot="{shot_idx}">Clear boxes</button>
        </div>
        <p class="draft" data-step="{step_idx}" data-shot="{shot_idx}">{draft_line}</p>
        <div class="actions">
          <button class="keep" data-step="{step_idx}" data-shot="{shot_idx}">Keep</button>
          <button class="apply" data-step="{step_idx}" data-shot="{shot_idx}">Apply</button>
          <input class="label" data-step="{step_idx}" data-shot="{shot_idx}" placeholder="label for the next box (optional)">
          <span class="status" id="status-{sid}"></span>
        </div>
      </figure>'''


def _step_html(s, rel) -> str:
    esc = html.escape
    shots_html = "".join(_shot_html(s.index, i, shot, rel) for i, shot in enumerate(s.shots))
    cols = min(len(s.shots), 3) or 1
    new_draft_line = esc(_draft_line(None, [], None, saved=False))
    return f'''
<section class="step" id="step-{s.index}" data-step="{s.index}">
  <h2>{s.index}. {esc(s.heading)}</h2>
  <div class="stepactions">
    <div class="modes">
      <button class="mode active" data-mode="edit" data-step="{s.index}">Edit</button>
      <button class="mode" data-mode="new" data-step="{s.index}">New image</button>
    </div>
    <input class="note" data-step="{s.index}" placeholder="note for the writer (optional)">
  </div>
  <div class="newimage">
    <p class="draft" data-step="{s.index}" data-shot="new">{new_draft_line}</p>
    <input class="caption" data-step="{s.index}" placeholder="caption for the new image">
    <button class="addimage" data-step="{s.index}">Add image</button>
  </div>
  <div class="shots" style="--cols:{cols}">{shots_html}</div>
</section>'''


_CSS = '''
:root { --ink:#1b2530; --muted:#5c6b7a; --rule:#d6dde3; --box:#d1352b; --crop:#2b6cb0; --ok:#2f7d4f; --paper:#f3f5f7; }
body { margin:0; background:var(--paper); color:var(--ink); font:15px/1.5 system-ui, sans-serif; }
main { max-width:1500px; margin:0 auto; padding:24px; }
h1 { font-size:1.4rem; margin:0 0 4px; } .lede { color:var(--muted); margin:0 0 20px; }
.step { border-top:1px solid var(--rule); padding:18px 0 22px; }
.step h2 { font-size:1.05rem; margin:0 0 4px; }
.stepactions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:6px 0; }
.modes { display:flex; gap:6px; }
.newimage { display:none; margin:8px 0 14px; padding:10px 12px; border:1px dashed var(--rule); border-radius:6px; background:#fff; }
.step.new-mode .newimage { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
.newimage .draft { flex-basis:100%; }
.shots { display:grid; grid-template-columns:repeat(var(--cols,1), 1fr); gap:16px; align-items:start; margin-top:10px; }
@media (max-width:900px) { .shots { grid-template-columns:1fr; } }
figure.shot { margin:0; border:1px solid var(--rule); border-radius:6px; padding:10px; background:#fff; }
figure { margin:0; } figcaption { font-size:.8rem; color:var(--muted); margin-top:4px; }
.caption .meta { color:var(--muted); font-weight:400; }
.stillwrap { position:relative; display:inline-block; max-width:100%; }
.stillwrap img { max-width:100%; display:block; border:1px solid var(--rule); background:#fff; }
.rerender { position:absolute; inset:0; display:none; align-items:center; justify-content:center;
  background:rgba(255,255,255,.85); font-weight:600; color:var(--muted); }
.rerender.busy { display:flex; }
.draw { position:relative; display:inline-block; max-width:100%; margin-top:8px; }
.draw img { display:block; max-width:100%; border:1px solid var(--rule); }
.draw canvas { position:absolute; inset:0; width:100%; height:100%; cursor:crosshair; }
.draw.loading canvas { cursor:progress; } .draw.loading img { opacity:.5; }
.hint { font-size:.8rem; color:var(--muted); margin:4px 0 0; }
.cands { display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }
.cand img { width:170px; border:1px solid var(--rule); display:block; }
.cand button { margin-top:4px; }
button.moment.selected { outline:2px solid var(--ok); background:#eaf7ee; }
.tools { display:flex; gap:6px; align-items:center; margin-top:10px; flex-wrap:wrap; }
button.tool[data-tool="box"] { border-color:var(--box); color:var(--box); }
button.tool[data-tool="crop"] { border-color:var(--crop); color:var(--crop); border-style:dashed; }
button.tool.active, button.mode.active { box-shadow:inset 0 0 0 1px currentColor; font-weight:600; }
button.undobox, button.clearboxes { border-color:var(--box); color:var(--box); }
.draft { margin:6px 0 0; font-size:.85rem; color:var(--muted); } .draft.ok { color:var(--ok); } .draft.err { color:var(--box); }
.actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px; }
button { font:inherit; padding:4px 10px; border:1px solid var(--rule); border-radius:4px; background:#fff; cursor:pointer; }
button.keep { border-color:var(--ok); color:var(--ok); }
button.apply { border-color:var(--ok); color:var(--ok); font-weight:600; }
button.nocrop, button.margincrop { border-color:var(--crop); color:var(--crop); }
input { font:inherit; padding:4px 8px; border:1px solid var(--rule); border-radius:4px; min-width:220px; }
.status { font-size:.85rem; color:var(--muted); } .status.ok { color:var(--ok); } .status.err { color:var(--box); }
.step.decided h2::before { content:"\\2713 "; color:var(--ok); }
#out { width:100%; min-height:120px; font:12px/1.4 ui-monospace, monospace; }
'''

_SCRIPT = '''
const SERVE = __SERVE__;
const FRAME_W = __FRAME_W__, FRAME_H = __FRAME_H__;
const decisions = {};
const drafts = {};
const newDrafts = {};
const modeState = {};
const toolState = {};
const out = document.getElementById('out');

function render() { out.value = JSON.stringify(decisions, null, 2); }
function pushDecision(step, item) {
  if (!decisions[step]) decisions[step] = [];
  decisions[step].push(item);
  render();
}
function status(step, shot, text, cls) {
  const el = document.getElementById('status-' + step + '-' + shot);
  if (el) { el.textContent = text; el.className = 'status ' + (cls || ''); }
}
function decided(step) {
  const el = document.getElementById('step-' + step);
  if (el) el.classList.add('decided');
}
function overlayEl(step, shot) {
  return document.querySelector('.rerender[data-step="' + step + '"][data-shot="' + shot + '"]');
}
function showOverlay(step, shot) { const el = overlayEl(step, shot); if (el) el.classList.add('busy'); }
function hideOverlay(step, shot) { const el = overlayEl(step, shot); if (el) el.classList.remove('busy'); }

function stepMode(step) { return modeState[step] || 'edit'; }
function toolKey(step, shot) { return step + ':' + shot; }
function currentTool(step, shot) { return toolState[toolKey(step, shot)] || 'box'; }

// --- drafts: one per sidecar'd shot (seeded from the page), one per step for New-image
// staging. Every drag/click mutates a draft in place; nothing is posted until Apply/Add.
function draftKey(step, shot) { return step + ':' + shot; }
function blankDirty() { return {at: false, boxes: false, crop: false}; }
function freshDraft(seed) {
  return {
    at: seed ? seed.at : undefined,
    boxes: (seed && seed.boxes) ? seed.boxes.slice() : [],
    crop: seed ? (seed.crop === undefined ? null : seed.crop) : null,
    dirty: blankDirty(),
    saved: true,
  };
}
function blankNewDraft() { return {at: undefined, boxes: [], crop: null, dirty: blankDirty(), saved: false}; }

document.querySelectorAll('script.seed').forEach(el => {
  const step = +el.dataset.step, shot = +el.dataset.shot;
  const seed = JSON.parse(el.textContent);
  drafts[draftKey(step, shot)] = freshDraft(seed);
});

function newDraftFor(step) {
  if (!newDrafts[step]) newDrafts[step] = blankNewDraft();
  return newDrafts[step];
}
function resetNewDraft(step) { newDrafts[step] = blankNewDraft(); renderDraftLine(step, 'new'); redrawStep(step); }
function activeDraft(step, shot) {
  return stepMode(step) === 'new' ? newDraftFor(step) : drafts[draftKey(step, shot)];
}

function boxesLabel(n) { return n + (n === 1 ? ' box' : ' boxes'); }
function cropLabel(crop) {
  if (crop === null || crop === undefined) return 'crop none';
  if (crop.margin !== undefined) return 'crop margin';
  return 'crop ' + crop.x + ',' + crop.y + ' ' + crop.w + 'x' + crop.h;
}
function draftLine(d) {
  if (!d || d.at === undefined) return 'nothing staged yet — click a moment above';
  return ['moment ' + d.at + ' s', boxesLabel(d.boxes.length), cropLabel(d.crop), d.saved ? 'saved' : 'unsaved'].join(' · ');
}
function draftEl(step, shot) {
  return document.querySelector('.draft[data-step="' + step + '"][data-shot="' + shot + '"]');
}
function renderDraftLine(step, shot) {
  const d = shot === 'new' ? newDraftFor(step) : drafts[draftKey(step, shot)];
  const el = draftEl(step, shot);
  if (el) { el.textContent = draftLine(d); el.className = 'draft'; }
}

// Canvas always renders the *active* draft (the shot's own in Edit mode, the step's shared
// staging draft in New-image mode) — every box red-solid with its label, the crop blue-
// dashed. Called on image load, on resize, and after every draft mutation.
function drawCanvas(step, shot) {
  const wrap = document.querySelector('.draw[data-step="' + step + '"][data-shot="' + shot + '"]');
  if (!wrap) return;
  const cv = wrap.querySelector('canvas');
  if (!cv || !cv.width || !cv.height) return;
  const d = activeDraft(step, shot);
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!d) return;
  const s = cv.width / FRAME_W;
  (d.boxes || []).forEach(b => {
    ctx.lineWidth = 2; ctx.setLineDash([]); ctx.strokeStyle = '#d1352b';
    ctx.strokeRect(b.x * s, b.y * s, b.w * s, b.h * s);
    if (b.label) {
      ctx.fillStyle = '#d1352b'; ctx.font = '12px sans-serif';
      ctx.fillText(b.label, b.x * s + 2, Math.max(10, b.y * s - 4));
    }
  });
  if (d.crop && d.crop.margin === undefined) {
    ctx.lineWidth = 2; ctx.strokeStyle = '#2b6cb0'; ctx.setLineDash([7, 5]);
    ctx.strokeRect(d.crop.x * s, d.crop.y * s, d.crop.w * s, d.crop.h * s);
  }
}
function redrawStep(step) {
  document.querySelectorAll('.draw[data-step="' + step + '"]').forEach(wrap => drawCanvas(step, +wrap.dataset.shot));
}
function editable(step, shot) {
  const d = activeDraft(step, shot);
  return !!d && !d.busy;                          // frozen while an Apply is in flight
}
function markDirty(step, shot, field) {
  const d = activeDraft(step, shot);
  if (!d) return;
  d.dirty[field] = true;
  d.saved = false;
  redrawStep(step);
  renderDraftLine(step, stepMode(step) === 'new' ? 'new' : shot);
}

async function decide(step, shot, d) {
  const noteEl = document.querySelector('.note[data-step="' + step + '"]');
  const note = noteEl ? noteEl.value.trim() : '';
  if (note) d.note = note;
  const item = Object.assign({shot}, d);
  pushDecision(step, item);
  if (!SERVE) { status(step, shot, 'recorded'); decided(step); return; }
  showOverlay(step, shot);
  status(step, shot, 'saving…');
  const body = Object.assign({step}, item);
  try {
    const r = await fetch('/decide', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if (!j.ok) { hideOverlay(step, shot); status(step, shot, j.error || 'failed', 'err'); return; }
    hideOverlay(step, shot); status(step, shot, j.message || 'done', 'ok'); decided(step);
  } catch (e) {
    hideOverlay(step, shot);
    status(step, shot, 'server not reachable: ' + e, 'err');
  }
}

// One `edit` decision per Apply, carrying only what's dirty. On success the reply's
// at/boxes/crop are adopted back into the draft (a grown crop or a dropped anchor must
// show), on failure the draft — and its dirty flags — are left exactly as they were.
async function applyEdit(step, shot, item) {
  const record = Object.assign({shot}, item);
  pushDecision(step, record);
  const d = drafts[draftKey(step, shot)];
  if (!SERVE) {
    if (d) { d.dirty = blankDirty(); d.saved = true; renderDraftLine(step, shot); }
    status(step, shot, 'recorded'); decided(step);
    return;
  }
  showOverlay(step, shot);
  status(step, shot, 're-rendering…');
  const body = Object.assign({step}, record);
  if (d) d.busy = true;
  try {
    const r = await fetch('/decide', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if (!j.ok) { hideOverlay(step, shot); status(step, shot, j.error || 'failed', 'err'); return; }
    if (d) {
      if (j.at !== undefined) d.at = j.at;
      if (j.boxes !== undefined) d.boxes = j.boxes;
      if (j.crop !== undefined) d.crop = j.crop;
      d.dirty = blankDirty(); d.saved = true;
      redrawStep(step); renderDraftLine(step, shot);
    }
    const finish = () => { hideOverlay(step, shot); status(step, shot, j.message || 'done', 'ok'); decided(step); };
    if (j.image) {
      const img = document.getElementById('still-' + step + '-' + shot);
      if (img) {
        img.onload = finish;
        img.onerror = () => { hideOverlay(step, shot); status(step, shot, 'rendered, but the page could not load ' + j.image, 'err'); };
        img.src = j.image + '?v=' + Date.now();
      } else { finish(); }
    } else { finish(); }
  } catch (e) {
    hideOverlay(step, shot);
    status(step, shot, 'server not reachable: ' + e, 'err');
  } finally {
    if (d) d.busy = false;
  }
}

function swapFrame(img, src) {
  const wrap = img.closest('.draw');
  if (wrap) wrap.classList.add('loading');
  img.src = src;
}
function plainImgs(step) { return Array.from(document.querySelectorAll('.draw[data-step="' + step + '"] img')); }

document.querySelectorAll('button.mode').forEach(b => b.onclick = () => {
  const step = +b.dataset.step;
  modeState[step] = b.dataset.mode;
  document.querySelectorAll('button.mode[data-step="' + step + '"]').forEach(x => x.classList.toggle('active', x === b));
  const section = document.getElementById('step-' + step);
  if (section) section.classList.toggle('new-mode', b.dataset.mode === 'new');
  if (b.dataset.mode === 'edit') {
    // Back from staging: every canvas returns to its own shot's frame.
    plainImgs(step).forEach(img => { if (img.dataset.home && img.getAttribute('src') !== img.dataset.home) swapFrame(img, img.dataset.home); });
    document.querySelectorAll('button.moment[data-step="' + step + '"]').forEach(x => x.classList.remove('selected'));
  }
  redrawStep(step);
});

document.querySelectorAll('button.tool').forEach(b => b.onclick = () => {
  const step = +b.dataset.step, shot = +b.dataset.shot;
  toolState[toolKey(step, shot)] = b.dataset.tool;
  document.querySelectorAll('button.tool[data-step="' + step + '"][data-shot="' + shot + '"]')
    .forEach(x => x.classList.toggle('active', x === b));
});

document.querySelectorAll('button.moment').forEach(b => b.onclick = () => {
  const step = +b.dataset.step, shot = +b.dataset.shot, atStr = b.dataset.at, at = +atStr;
  if (!editable(step, shot)) return;
  // The drawing surface follows the click: the next drag is on this frame. In Edit mode
  // that is this shot's canvas (and becomes its home frame); in New-image mode the step's
  // canvases share one staged draft, so they all show the staged moment. The draft's
  // boxes/crop stay put — the user clears them with Undo/Clear if they no longer apply.
  const src = SERVE ? ('/frame?at=' + atStr) : ('thumb-' + atStr + '.jpg');
  const staging = stepMode(step) === 'new';
  document.querySelectorAll('button.moment[data-step="' + step + '"]' + (staging ? '' : '[data-shot="' + shot + '"]'))
    .forEach(x => x.classList.remove('selected'));
  b.classList.add('selected');
  const targets = staging ? plainImgs(step) : [document.getElementById('plain-' + step + '-' + shot)].filter(Boolean);
  targets.forEach(img => { if (!staging) img.dataset.home = src; swapFrame(img, src); });
  const d = activeDraft(step, shot);
  if (d) d.at = at;
  markDirty(step, shot, 'at');
});

function applyBox(step, shot, box) {
  if (!editable(step, shot)) return;
  const labelEl = document.querySelector('.label[data-step="' + step + '"][data-shot="' + shot + '"]');
  const label = labelEl ? labelEl.value.trim() : '';
  const d = activeDraft(step, shot);
  if (!d) return;
  const entry = Object.assign({}, box);
  if (label) entry.label = label;
  d.boxes.push(entry);
  markDirty(step, shot, 'boxes');
}
function applyCrop(step, shot, crop) {
  if (!editable(step, shot)) return;
  const d = activeDraft(step, shot);
  if (!d) return;
  d.crop = crop;
  markDirty(step, shot, 'crop');
}

document.querySelectorAll('.draw').forEach(wrap => {
  const step = +wrap.dataset.step, shot = +wrap.dataset.shot;
  const img = wrap.querySelector('img'), cv = wrap.querySelector('canvas');
  let start = null;
  const size = () => { cv.width = img.clientWidth; cv.height = img.clientHeight; drawCanvas(step, shot); };
  const loaded = () => { size(); wrap.classList.remove('loading'); };
  img.dataset.home = img.getAttribute('src');   // the frame this shot returns to after staging
  img.addEventListener('load', loaded); img.addEventListener('error', loaded); size(); window.addEventListener('resize', size);
  const pos = e => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  cv.onmousedown = e => { if (wrap.classList.contains('loading') || !editable(step, shot)) return; size(); start = pos(e); };
  cv.onmousemove = e => {
    if (!start) return;
    const [x, y] = pos(e);
    drawCanvas(step, shot);
    const c = cv.getContext('2d');
    c.lineWidth = 3;
    if (currentTool(step, shot) === 'crop') { c.strokeStyle = '#2b6cb0'; c.setLineDash([7, 5]); }
    else { c.strokeStyle = '#d1352b'; c.setLineDash([]); }
    c.strokeRect(Math.min(start[0], x), Math.min(start[1], y), Math.abs(x - start[0]), Math.abs(y - start[1]));
  };
  cv.onmouseup = e => {
    if (!start) return;
    const [x, y] = pos(e);
    const s = FRAME_W / cv.width;
    const rect = {
      x: Math.round(Math.min(start[0], x) * s), y: Math.round(Math.min(start[1], y) * s),
      w: Math.round(Math.abs(x - start[0]) * s), h: Math.round(Math.abs(y - start[1]) * s)
    };
    start = null;
    if (rect.w < 8 || rect.h < 8) { drawCanvas(step, shot); return; }
    if (currentTool(step, shot) === 'crop') applyCrop(step, shot, rect);
    else applyBox(step, shot, rect);
  };
});

document.querySelectorAll('button.nocrop').forEach(b => b.onclick = () => applyCrop(+b.dataset.step, +b.dataset.shot, null));
document.querySelectorAll('button.margincrop').forEach(b => b.onclick = () => applyCrop(+b.dataset.step, +b.dataset.shot, {margin: __MARGIN_CROP__}));
document.querySelectorAll('button.undobox').forEach(b => b.onclick = () => {
  const step = +b.dataset.step, shot = +b.dataset.shot;
  if (!editable(step, shot)) return;
  const d = activeDraft(step, shot);
  if (d && d.boxes.length) d.boxes.pop();
  markDirty(step, shot, 'boxes');
});
document.querySelectorAll('button.clearboxes').forEach(b => b.onclick = () => {
  const step = +b.dataset.step, shot = +b.dataset.shot;
  if (!editable(step, shot)) return;
  const d = activeDraft(step, shot);
  if (d) d.boxes = [];
  markDirty(step, shot, 'boxes');
});

document.querySelectorAll('button.keep').forEach(b => b.onclick = () => {
  const step = +b.dataset.step, shot = +b.dataset.shot;
  if (stepMode(step) === 'new') return;
  decide(step, shot, {action: 'keep'});
});

document.querySelectorAll('button.apply').forEach(b => b.onclick = () => {
  const step = +b.dataset.step, shot = +b.dataset.shot;
  const d = drafts[draftKey(step, shot)];
  if (!d) return;
  if (d.busy) { status(step, shot, 'still applying the last change…'); return; }
  if (!d.dirty.at && !d.dirty.boxes && !d.dirty.crop) { status(step, shot, 'nothing to apply'); return; }
  const noteEl = document.querySelector('.note[data-step="' + step + '"]');
  const note = noteEl ? noteEl.value.trim() : '';
  const item = {action: 'edit'};
  if (d.dirty.at) item.at = d.at;
  if (d.dirty.boxes) item.boxes = d.boxes;
  if (d.dirty.crop) item.crop = d.crop;
  if (note) item.note = note;
  applyEdit(step, shot, item);
});

async function postAdd(step, body) {
  const el = draftEl(step, 'new');
  if (el) { el.textContent = 'rendering the new image…'; el.className = 'draft'; }
  try {
    const r = await fetch('/decide', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if (!j.ok) { if (el) { el.textContent = j.error || 'failed'; el.className = 'draft err'; } return; }
    if (el) { el.textContent = (j.message || 'added') + (j.reload ? ' — reloading the page' : ''); el.className = 'draft ok'; }
    if (j.reload) { newDrafts[step] = blankNewDraft(); setTimeout(() => location.reload(), 600); }
  } catch (e) { if (el) { el.textContent = 'server not reachable: ' + e; el.className = 'draft err'; } }
}

document.querySelectorAll('button.addimage').forEach(b => b.onclick = () => {
  const step = +b.dataset.step;
  const d = newDraftFor(step);
  if (d.at === undefined) {
    const original = b.textContent;
    b.textContent = 'pick a moment first';
    setTimeout(() => { b.textContent = original; }, 2500);
    return;
  }
  const captionEl = document.querySelector('.caption[data-step="' + step + '"]');
  const noteEl = document.querySelector('.note[data-step="' + step + '"]');
  const caption = captionEl ? captionEl.value.trim() : '';
  const note = noteEl ? noteEl.value.trim() : '';
  const item = {action: 'add', at: d.at, boxes: d.boxes};
  if (d.dirty.crop) item.crop = d.crop;          // untouched: the server picks the default
  if (caption) item.caption = caption;
  if (note) item.note = note;
  pushDecision(step, item);
  const body = Object.assign({step}, item);
  if (SERVE) postAdd(step, body); else resetNewDraft(step);
});

if (SERVE) {
  fetch('/review.json')
    .then(r => { if (!r.ok) throw new Error('no log yet'); return r.json(); })
    .then(data => {
      Object.keys(data).forEach(step => {
        decisions[step] = data[step];
        if ((data[step] || []).some(d => !d.error)) decided(step);
      });
      render();
    })
    .catch(() => {});
}

render();
'''


def render_page(steps, out_dir, info, source, *, serve: bool) -> str:
    def rel(p) -> str:
        return os.path.relpath(p, out_dir).replace(os.sep, "/")

    sections = "".join(_step_html(s, rel) for s in steps)
    script = (_SCRIPT.replace("__SERVE__", "true" if serve else "false")
                      .replace("__FRAME_W__", str(info.width))
                      .replace("__FRAME_H__", str(info.height))
                      .replace("__MARGIN_CROP__", str(MARGIN_CROP)))
    lede = ("Decisions are applied as you make them and the still updates in place."
            if serve else
            "Decisions collect in the box at the bottom; save it as <code>review.json</code> "
            "beside this page and run <code>vedit review --apply</code>.")

    return f'''<!doctype html>
<meta charset="utf-8">
<title>Review: {html.escape(source.name)}</title>
<style>{_CSS}</style>
<main>
<h1>Review: {html.escape(source.name)}</h1>
<p class="lede">{len(steps)} screenshots in guide order. Click a candidate moment, drag boxes
or a crop, then press Apply to save the shot in one render. Switch a step to New image mode
to stage another screenshot the same way. {lede}</p>
{sections}
<section class="step"><h2>review.json</h2><textarea id="out" readonly></textarea>
<p class="hint">{"Also written to review.json beside this page after every decision." if serve else "Copy this into review.json beside this page."}</p></section>
</main>
<script>{script}</script>
'''
