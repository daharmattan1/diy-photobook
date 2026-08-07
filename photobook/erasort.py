"""Era-sorter — assign every accepted photo to an era BY EYE, sweep by sweep.

Why this exists: date-based auto-placement is unreliable in practice. Cloud photo
services often strip EXIF dates, and filenames lie (iPhone IMG_NNNN counters reset
across devices, so an event can land in the wrong year's chapter). The only fully
reliable signal is the owner's eyes.

The interaction (the owner's design): pick ONE era, click every photo in the pool
that belongs to it, hit Submit. Those photos vanish from the pool. Pick the next
era, repeat — until the pool is empty. Far faster than a per-photo dropdown,
and it matches how people actually think ("these are all the wedding ones").

Output: a downloaded eras_<sweep>.json  { era: <id>, asset_ids: [...] }
Import with:  photobook erasort --import <file-or-dir>
which writes `chapter` directly on those assets (authoritative over dates).
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Optional

import yaml

from . import db, paths

GRID_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#0f1216; color:#e6e9ef;
       font-family: ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif; }
header { position:sticky; top:0; z-index:20; background:#0f1216ee; backdrop-filter:blur(8px);
         border-bottom:1px solid #232833; padding:12px 18px; }
h1 { margin:0 0 10px; font-size:17px; font-weight:650; }
.eras { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.era { padding:8px 14px; border:1px solid #2a2f3a; border-radius:999px; background:#161a22;
       color:#c8cfdb; cursor:pointer; font-size:13px; }
.era:hover { border-color:#3d6ea8; }
.era.active { background:#2f6f4f; border-color:#3c8a63; color:#fff; font-weight:600; }
.bar { display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap; }
.btn { padding:8px 16px; border:1px solid #2a2f3a; border-radius:8px; background:#1b2029;
       color:#e6e9ef; cursor:pointer; font-size:13px; }
.btn.primary { background:#2f6f4f; border-color:#3c8a63; font-weight:600; }
.btn.primary:disabled { background:#23303a; border-color:#2a2f3a; color:#667; cursor:not-allowed; }
.count { color:#9aa4b2; font-size:13px; }
.hint { color:#8a93a3; font-size:12px; padding:6px 18px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:8px; padding:14px 18px; }
.tile { position:relative; border:2px solid transparent; border-radius:8px; overflow:hidden;
        background:#161a22; cursor:pointer; }
.tile img { width:100%; height:150px; object-fit:cover; display:block; }
.tile.sel { border-color:#3c8a63; box-shadow:0 0 0 2px #3c8a6355; }
.tile.sel::after { content:"\\2713"; position:absolute; top:6px; right:6px; background:#2f6f4f;
        color:#fff; width:22px; height:22px; border-radius:50%; display:flex;
        align-items:center; justify-content:center; font-size:13px; font-weight:700; }
.tile .cap { font-size:10px; color:#7b8494; padding:3px 5px; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis; }
"""


def _load_eras() -> list[tuple[str, str]]:
    ch = yaml.safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    return [(c["id"], c.get("title", c["id"])) for c in (ch.get("chapters", []) or [])]


def _rel(p: Path) -> str:
    try:
        return os.path.relpath(p, paths.REVIEW_DIR).replace("\\", "/")
    except ValueError:
        return p.as_uri()


def generate(db_path: Optional[Path] = None, page_size: int = 300) -> dict:
    """Write review/erasort_pN.html — the sweep-based era assigner."""
    paths.REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    eras = _load_eras()

    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, derivative_path, orig_path, src, chapter, best_datetime "
            "FROM assets WHERE review_status='accept' ORDER BY src, orig_path"
        ).fetchall()

    for old in paths.REVIEW_DIR.glob("erasort_*.html"):
        old.unlink()

    n_pages = max(1, (len(rows) + page_size - 1) // page_size)
    files = [f"erasort_p{i+1}.html" for i in range(n_pages)]
    for p in range(n_pages):
        chunk = rows[p * page_size:(p + 1) * page_size]
        (paths.REVIEW_DIR / files[p]).write_text(
            _render(chunk, eras, files, p), encoding="utf-8")

    return {"pages": len(files), "photos": len(rows),
            "files": [f"review/{f}" for f in files]}


def _render(rows, eras, files, page_idx) -> str:
    tiles = []
    for r in rows:
        src_rel = _rel(Path(r["derivative_path"]))
        name = os.path.basename(r["orig_path"] or "")[:26]
        tiles.append(
            f'<div class="tile" data-id="{html.escape(r["asset_id"])}">'
            f'<img loading="lazy" src="{html.escape(src_rel)}" alt="">'
            f'<div class="cap">{html.escape(name)} · {html.escape(r["src"] or "")}</div></div>'
        )
    era_btns = "".join(
        f'<button class="era" data-era="{html.escape(eid)}">{html.escape(t)}</button>'
        for eid, t in eras
    )
    nav = " ".join(
        (f"<b>{i+1}</b>" if i == page_idx else f'<a href="{f}" style="color:#6ea8fe">{i+1}</a>')
        for i, f in enumerate(files)
    )
    # JS kept OUT of the f-string (JS braces collide with f-string syntax);
    # only PAGE is injected, via a simple placeholder swap.
    js = _PAGE_JS.replace("__PAGE__", str(page_idx + 1))
    head = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Era sort — page {page_idx+1}/{len(files)}</title>
<style>{GRID_CSS}</style></head><body>
<header>
  <h1>Sort into eras — page {page_idx+1}/{len(files)} &nbsp;<span class="count">{nav}</span></h1>
  <div class="eras">{era_btns}</div>
  <div class="bar">
    <button class="btn primary" id="submitBtn" disabled>Submit selected to era</button>
    <button class="btn" id="clearSel">Clear selection</button>
    <span class="count" id="status">Pick an era above, then click every photo from it.</span>
    <span class="count" id="remain"></span>
  </div>
</header>
<div class="hint">Photos you submit disappear from this pool. Sorted photos are remembered locally; each Submit downloads one sweep file. Shift-click selects a range.</div>
<div class="grid" id="grid">
{''.join(tiles)}
</div>
"""
    return head + "<script>\n" + js + "\n</script>\n</body></html>"


_PAGE_JS = r"""
const PAGE = __PAGE__;
const DONE_KEY = 'photobook.erasort.done';      // asset_id -> era (shared across pages)
let currentEra = null, lastIdx = -1;
function load() { try { return JSON.parse(localStorage.getItem(DONE_KEY) || '{}'); } catch(e) { return {}; } }
function save(d) { localStorage.setItem(DONE_KEY, JSON.stringify(d)); }

function hideDone() {
  const done = load();
  document.querySelectorAll('.tile').forEach(function(t) {
    if (done[t.dataset.id]) t.style.display = 'none';
  });
  updateRemain();
}
function updateRemain() {
  const vis = Array.from(document.querySelectorAll('.tile')).filter(function(t){ return t.style.display !== 'none'; }).length;
  const sel = document.querySelectorAll('.tile.sel').length;
  document.getElementById('remain').textContent = vis + ' left on this page · ' + sel + ' selected';
  document.getElementById('submitBtn').disabled = !(currentEra && sel > 0);
}
document.querySelectorAll('.era').forEach(function(b) {
  b.addEventListener('click', function() {
    document.querySelectorAll('.era').forEach(function(x){ x.classList.remove('active'); });
    b.classList.add('active');
    currentEra = b.dataset.era;
    document.getElementById('status').textContent = 'Era: ' + b.textContent + ' — click photos, then Submit.';
    updateRemain();
  });
});
const tiles = Array.from(document.querySelectorAll('.tile'));
tiles.forEach(function(t, i) {
  t.addEventListener('click', function(e) {
    if (e.shiftKey && lastIdx >= 0) {
      const a = Math.min(lastIdx, i), b = Math.max(lastIdx, i);
      for (let k = a; k <= b; k++) if (tiles[k].style.display !== 'none') tiles[k].classList.add('sel');
    } else {
      t.classList.toggle('sel');
      lastIdx = i;
    }
    updateRemain();
  });
});
document.getElementById('clearSel').addEventListener('click', function() {
  document.querySelectorAll('.tile.sel').forEach(function(t){ t.classList.remove('sel'); });
  updateRemain();
});
document.getElementById('submitBtn').addEventListener('click', function() {
  const sel = Array.from(document.querySelectorAll('.tile.sel'));
  if (!currentEra || !sel.length) return;
  const ids = sel.map(function(t){ return t.dataset.id; });
  const done = load();
  ids.forEach(function(id){ done[id] = currentEra; });
  save(done);
  const payload = { era: currentEra, asset_ids: ids, page: PAGE, exported_at: new Date().toISOString() };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'eras_' + currentEra + '_p' + PAGE + '_' + Date.now() + '.json';
  document.body.appendChild(a); a.click(); a.remove();
  sel.forEach(function(t){ t.classList.remove('sel'); t.style.display = 'none'; });
  document.getElementById('status').textContent = 'Submitted ' + ids.length + ' to ' + currentEra + '. Pick the next era.';
  updateRemain();
});
hideDone();
"""


def import_eras(path: Path, db_path: Optional[Path] = None) -> dict:
    """Import downloaded eras_*.json sweeps; write `chapter` directly."""
    files = []
    if path.is_dir():
        files = sorted(path.glob("eras_*.json"))
    elif path.is_file():
        files = [path]
    if not files:
        raise FileNotFoundError(f"no eras_*.json found at {path}")

    applied = 0
    per_era: dict[str, int] = {}
    with db.session(db_path) as conn:
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            era = data.get("era")
            ids = data.get("asset_ids", []) or []
            if not era:
                continue
            for aid in ids:
                cur = conn.execute(
                    "UPDATE assets SET chapter=? WHERE asset_id=?", (era, aid))
                if cur.rowcount:
                    applied += 1
                    per_era[era] = per_era.get(era, 0) + 1
    return {"files": len(files), "applied": applied, "per_era": per_era}
