"""Theme-tagger — apply OVERLAPPING multi-tags to photos (e.g. holidays, kids,
sports, a favorite place, grandparents...).

Unlike the event-sweeper (one exclusive label per photo), a photo can carry
SEVERAL tags — a beach photo can also be 'kids' and 'summer'. So photos do
NOT vanish when tagged: they get a badge and stay, and you can add more tags on
another sweep. Tags are dateless themes, not dated moments.

Interaction: define a tag (name only), pick it, click photos, "Add tag to
selected" → the tag is added to those photos (badge appears), selection clears,
photos stay. Filter to "untagged" or a specific tag to focus. Each submit
downloads a sweep; import APPENDS tags (multi-value, deduped).

Import:  photobook tagsort --import <file-or-dir>
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Optional

from . import db, paths
from .erasort import GRID_CSS


def _split(tags: Optional[str]) -> list[str]:
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def generate(db_path: Optional[Path] = None, page_size: int = 300) -> dict:
    paths.REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, derivative_path, orig_path, chapter, tags "
            "FROM assets WHERE review_status='accept' ORDER BY chapter, orig_path"
        ).fetchall()
    for old in paths.REVIEW_DIR.glob("tagsort_*.html"):
        old.unlink()
    n_pages = max(1, (len(rows) + page_size - 1) // page_size)
    files = [f"tagsort_p{i+1}.html" for i in range(n_pages)]
    for p in range(n_pages):
        chunk = rows[p * page_size:(p + 1) * page_size]
        (paths.REVIEW_DIR / files[p]).write_text(
            _render(chunk, files, p), encoding="utf-8")
    return {"pages": len(files), "photos": len(rows),
            "files": [f"review/{f}" for f in files]}


def _rel(p: Path) -> str:
    try:
        return os.path.relpath(p, paths.REVIEW_DIR).replace("\\", "/")
    except ValueError:
        return p.as_uri()


def _render(rows, files, page_idx) -> str:
    tiles = []
    for r in rows:
        name = os.path.basename(r["orig_path"] or "")[:22]
        existing = ",".join(_split(r["tags"]))
        tiles.append(
            f'<div class="tile" data-id="{html.escape(r["asset_id"])}" '
            f'data-tags="{html.escape(existing)}">'
            f'<img loading="lazy" src="{html.escape(_rel(Path(r["derivative_path"])))}" alt="">'
            f'<div class="badges"></div>'
            f'<div class="cap">{html.escape(name)}</div></div>'
        )
    nav = " ".join(
        (f"<b>{i+1}</b>" if i == page_idx else f'<a href="{f}" style="color:#6ea8fe">{i+1}</a>')
        for i, f in enumerate(files)
    )
    js = _JS.replace("__PAGE__", str(page_idx + 1))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Theme tags — page {page_idx+1}/{len(files)}</title>
<style>{GRID_CSS}
 .newev {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px; }}
 .newev input {{ background:#161a22; border:1px solid #2a2f3a; border-radius:8px;
   color:#e6e9ef; padding:7px 10px; font-size:13px; min-width:200px; }}
 .filters {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }}
 .filt {{ padding:5px 11px; border:1px solid #2a2f3a; border-radius:999px;
   background:#12161d; color:#9aa4b2; cursor:pointer; font-size:12px; }}
 .filt.on {{ background:#26313f; color:#e6e9ef; border-color:#3d6ea8; }}
 .badges {{ position:absolute; top:4px; left:4px; right:4px; display:flex; flex-wrap:wrap; gap:3px; }}
 .badges span {{ background:#2f6f4fdd; color:#fff; font-size:9px; padding:1px 5px;
   border-radius:6px; }}
 .tile.sel {{ border-color:#3d6ea8; box-shadow:0 0 0 2px #3d6ea855; }}
</style></head><body>
<header>
  <h1>Theme tags — page {page_idx+1}/{len(files)} &nbsp;<span class="count">{nav}</span></h1>
  <div class="newev">
    <input id="tagName" placeholder="Tag (e.g. holidays, kids, sports, a place, grandparents)">
    <button class="btn" id="addTag">+ Add tag</button>
    <span class="count">pick a tag below, click photos, add</span>
  </div>
  <div class="eras" id="tagList" style="margin-top:10px"></div>
  <div class="filters" id="filterRow"></div>
  <div class="bar">
    <button class="btn primary" id="applyBtn" disabled>Add tag to selected</button>
    <button class="btn" id="clearSel">Clear selection</button>
    <span class="count" id="status">Add a tag, pick it, click photos, then Add.</span>
    <span class="count" id="remain"></span>
  </div>
</header>
<div class="hint">Photos can have SEVERAL tags — they don't disappear when tagged, a badge appears. Add one tag, then re-select and add another. Use the filter chips to show only untagged or a specific tag. Shift-click for ranges.</div>
<div class="grid" id="grid">
{''.join(tiles)}
</div>
<script>
{js}
</script>
</body></html>"""


_JS = r"""
const PAGE = __PAGE__;
const TAGS_KEY  = 'photobook.tagsort.tags';      // list of defined tag names
const APPLY_KEY = 'photobook.tagsort.applied';   // asset_id -> [tags] added this session
let currentTag = null, lastIdx = -1, filter = null;

function loadTags(){ try { return JSON.parse(localStorage.getItem(TAGS_KEY)||'[]'); } catch(e){ return []; } }
function saveTags(v){ localStorage.setItem(TAGS_KEY, JSON.stringify(v)); }
function loadApplied(){ try { return JSON.parse(localStorage.getItem(APPLY_KEY)||'{}'); } catch(e){ return {}; } }
function saveApplied(v){ localStorage.setItem(APPLY_KEY, JSON.stringify(v)); }

// a tile's current tags = its data-tags (from DB) UNION applied-this-session
function tileTags(t){
  const base = (t.dataset.tags||'').split(',').map(s=>s.trim()).filter(Boolean);
  const app = (loadApplied()[t.dataset.id]||[]);
  return Array.from(new Set(base.concat(app)));
}
function renderBadges(t){
  const b = t.querySelector('.badges');
  b.innerHTML = tileTags(t).map(function(x){ return '<span>'+x+'</span>'; }).join('');
}
function renderAllBadges(){ document.querySelectorAll('.tile').forEach(renderBadges); }

function renderTags(){
  const tags = loadTags();
  const box = document.getElementById('tagList');
  box.innerHTML = tags.map(function(n){ return '<button class="era" data-tag="'+encodeURIComponent(n)+'">'+n+'</button>'; }).join('')
    || '<span class="count">No tags yet — add one above.</span>';
  box.querySelectorAll('.era').forEach(function(b){
    b.addEventListener('click', function(){
      box.querySelectorAll('.era').forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      currentTag = decodeURIComponent(b.dataset.tag);
      document.getElementById('status').textContent = 'Tag: ' + currentTag + ' — click photos, then Add.';
      updateRemain();
    });
  });
  // filter chips: All / Untagged / each tag
  const fr = document.getElementById('filterRow');
  let chips = '<span class="filt' + (filter===null?' on':'') + '" data-f="__all">All</span>'
            + '<span class="filt' + (filter==='__untagged'?' on':'') + '" data-f="__untagged">Untagged</span>';
  tags.forEach(function(n){ chips += '<span class="filt' + (filter===n?' on':'') + '" data-f="'+encodeURIComponent(n)+'">'+n+'</span>'; });
  fr.innerHTML = chips;
  fr.querySelectorAll('.filt').forEach(function(c){
    c.addEventListener('click', function(){
      const f = c.dataset.f;
      filter = (f==='__all') ? null : (f==='__untagged' ? '__untagged' : decodeURIComponent(f));
      applyFilter(); renderTags();
    });
  });
}
function applyFilter(){
  document.querySelectorAll('.tile').forEach(function(t){
    const tags = tileTags(t);
    let show = true;
    if (filter === '__untagged') show = tags.length === 0;
    else if (filter) show = tags.indexOf(filter) >= 0;
    t.style.display = show ? '' : 'none';
  });
  updateRemain();
}
function updateRemain(){
  const vis = Array.from(document.querySelectorAll('.tile')).filter(function(t){ return t.style.display!=='none'; }).length;
  const sel = document.querySelectorAll('.tile.sel').length;
  document.getElementById('remain').textContent = vis + ' shown · ' + sel + ' selected';
  document.getElementById('applyBtn').disabled = !(currentTag && sel>0);
}
document.getElementById('addTag').addEventListener('click', function(){
  const n = document.getElementById('tagName').value.trim();
  if (!n) return;
  const tags = loadTags();
  if (tags.indexOf(n) < 0) tags.push(n);
  saveTags(tags);
  document.getElementById('tagName').value = '';
  renderTags();
});
const tiles = Array.from(document.querySelectorAll('.tile'));
tiles.forEach(function(t,i){
  t.addEventListener('click', function(e){
    if (e.target.closest('.badges')) return;
    if (e.shiftKey && lastIdx>=0){
      const a=Math.min(lastIdx,i), b=Math.max(lastIdx,i);
      for (let k=a;k<=b;k++) if (tiles[k].style.display!=='none') tiles[k].classList.add('sel');
    } else { t.classList.toggle('sel'); lastIdx=i; }
    updateRemain();
  });
});
document.getElementById('clearSel').addEventListener('click', function(){
  document.querySelectorAll('.tile.sel').forEach(function(t){ t.classList.remove('sel'); }); updateRemain();
});
document.getElementById('applyBtn').addEventListener('click', function(){
  const sel = Array.from(document.querySelectorAll('.tile.sel'));
  if (!currentTag || !sel.length) return;
  const applied = loadApplied();
  const ids = [];
  sel.forEach(function(t){
    const id = t.dataset.id;
    const arr = applied[id] || [];
    if (arr.indexOf(currentTag) < 0) arr.push(currentTag);
    applied[id] = arr;
    ids.push(id);
    renderBadges(t);
    t.classList.remove('sel');
  });
  saveApplied(applied);
  const payload = { tag: currentTag, asset_ids: ids, page: PAGE, exported_at: new Date().toISOString() };
  const blob = new Blob([JSON.stringify(payload,null,2)], {type:'application/json'});
  const a = document.createElement('a');
  const slug = currentTag.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');
  a.href = URL.createObjectURL(blob);
  a.download = 'tag_' + slug + '_p' + PAGE + '_' + Date.now() + '.json';
  document.body.appendChild(a); a.click(); a.remove();
  document.getElementById('status').textContent = 'Added "' + currentTag + '" to ' + ids.length + ' photos. Keep going or pick another tag.';
  if (filter) applyFilter();
  updateRemain();
});
renderTags(); renderAllBadges(); updateRemain();
"""


def import_tags(path: Path, db_path: Optional[Path] = None) -> dict:
    """Import tag files; APPEND each tag to the photo's tag set (deduped).

    Accepts BOTH formats:
      - combined  : {"themes": {tag: [ids], ...}}   (tags_all_*.json — one file)
      - per-theme : {"tag": name, "asset_ids": [ids]} (tag_*.json — legacy)
    """
    files = []
    if path.is_dir():
        files = sorted(path.glob("tag*.json"))   # matches tag_ AND tags_all_
    elif path.is_file():
        files = [path]
    if not files:
        raise FileNotFoundError(f"no tag*.json found at {path}")

    def _pairs(data):
        """Yield (tag, [asset_ids]) from either format."""
        if isinstance(data.get("themes"), dict):
            for tag, ids in data["themes"].items():
                yield (tag or "").strip(), ids or []
        else:
            yield (data.get("tag") or "").strip(), data.get("asset_ids", []) or []

    applied = 0
    per_tag: dict[str, int] = {}
    with db.session(db_path) as conn:
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            for tag, ids in _pairs(data):
                if not tag:
                    continue
                for aid in ids:
                    row = conn.execute(
                        "SELECT tags FROM assets WHERE asset_id=?", (aid,)).fetchone()
                    if not row:
                        continue
                    cur = set(_split(row["tags"]))
                    if tag not in cur:
                        cur.add(tag)
                        conn.execute("UPDATE assets SET tags=? WHERE asset_id=?",
                                     (",".join(sorted(cur)), aid))
                        applied += 1
                        per_tag[tag] = per_tag.get(tag, 0) + 1
    return {"files": len(files), "applied": applied, "per_tag": per_tag}
