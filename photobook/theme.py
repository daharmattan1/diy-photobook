"""Theme-aggregator — group photos by theme, then NARROW within each theme.

The one mechanism that genuinely narrows a big set: scattered across the
timeline, 18 beach photos look like 18 moments; grouped under 'beach' you see
them together and know you need 5. So this does two things in one surface:

  1. TAG: define a theme (beach, births, holidays, 'just us two'), select
     photos, "Add to theme" — they get a badge and STAY (a photo can be in
     several themes). This writes the multi-value `tags` column.
  2. NARROW: filter to one theme -> only its photos show, together -> click the
     weak/redundant to CUT (reject from the book). The survivors are the theme's
     keepers.

Two downloads, each importable by an existing command:
  - tag_<theme>_*.json   -> photobook tagsort --import   (the theme memberships)
  - cut_theme_*.json     -> photobook cull    --import   (the book-level cuts)
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Optional

from . import db, paths
from .erasort import GRID_CSS


def _split(t: Optional[str]) -> list[str]:
    return [x.strip() for x in (t or "").split(",") if x.strip()]


def _rel(p: Path) -> str:
    try:
        return os.path.relpath(p, paths.REVIEW_DIR).replace("\\", "/")
    except ValueError:
        return p.as_uri()


def generate(db_path: Optional[Path] = None, page_size: int = 100000) -> dict:
    # single page by default: the theme FILTER must aggregate ALL photos of a
    # theme in one view, which is impossible if they're split across pages.
    paths.REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, derivative_path, orig_path, chapter, tags, is_hero, event "
            "FROM assets WHERE review_status='accept' ORDER BY chapter, orig_path"
        ).fetchall()
    for old in paths.REVIEW_DIR.glob("theme_*.html"):
        old.unlink()
    n = max(1, (len(rows) + page_size - 1) // page_size)
    files = [f"theme_p{i+1}.html" for i in range(n)]
    for p in range(n):
        (paths.REVIEW_DIR / files[p]).write_text(
            _render(rows[p*page_size:(p+1)*page_size], files, p), encoding="utf-8")
    return {"pages": n, "photos": len(rows), "files": [f"review/{f}" for f in files]}


def _render(rows, files, page_idx) -> str:
    tiles = []
    for r in rows:
        existing = ",".join(_split(r["tags"]))
        hero = "★" if r["is_hero"] else ""
        tiles.append(
            f'<div class="tile" data-id="{html.escape(r["asset_id"])}" '
            f'data-tags="{html.escape(existing)}">'
            f'<img loading="lazy" src="{html.escape(_rel(Path(r["derivative_path"])))}" alt="">'
            f'<div class="badges"></div><div class="cut-x">✕ cut</div>'
            f'<div class="cap">{hero} {html.escape(os.path.basename(r["orig_path"] or "")[:18])}</div>'
            f'</div>')
    nav = " ".join((f"<b>{i+1}</b>" if i == page_idx else f'<a href="{f}" style="color:#6ea8fe">{i+1}</a>')
                   for i, f in enumerate(files))
    js = _JS.replace("__PAGE__", str(page_idx + 1))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Themes &amp; narrow — page {page_idx+1}/{len(files)}</title>
<style>{GRID_CSS}
 .newev {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px; }}
 .newev input {{ background:#161a22; border:1px solid #2a2f3a; border-radius:8px;
   color:#e6e9ef; padding:7px 10px; font-size:13px; min-width:220px; }}
 .filters {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }}
 .filt {{ padding:5px 11px; border:1px solid #2a2f3a; border-radius:999px;
   background:#12161d; color:#9aa4b2; cursor:pointer; font-size:12px; }}
 .filt.on {{ background:#26313f; color:#e6e9ef; border-color:#3d6ea8; }}
 .badges {{ position:absolute; top:4px; left:4px; right:26px; display:flex; flex-wrap:wrap; gap:3px; }}
 .badges span {{ background:#2f6f4fdd; color:#fff; font-size:9px; padding:1px 5px; border-radius:6px; }}
 .cut-x {{ position:absolute; top:4px; right:4px; background:#0009; color:#f88;
   font-size:10px; padding:2px 6px; border-radius:6px; opacity:0; cursor:pointer; }}
 .tile:hover .cut-x {{ opacity:1; }}
 .tile.cut {{ opacity:.28; filter:grayscale(1); border-color:#a33; }}
 .tile.cut .cut-x {{ opacity:1; background:#a33; color:#fff; }}
 .tile.sel {{ box-shadow:0 0 0 2px #3d6ea8; border-color:#3d6ea8; }}
 .mode {{ font-weight:600; }}
</style></head><body>
<header>
  <h1>Themes &amp; narrow — page {page_idx+1}/{len(files)} &nbsp;<span class="count">{nav}</span></h1>
  <div class="newev">
    <input id="tagName" placeholder="Theme (beach, births, holidays, just us two, grandparents)">
    <button class="btn" id="addTag">+ Add theme</button>
  </div>
  <div class="eras" id="tagList" style="margin-top:8px"></div>
  <div class="filters" id="filterRow"></div>
  <div class="bar">
    <button class="btn primary" id="tagBtn" disabled>Add selected to theme</button>
    <button class="btn" id="clearSel">Clear selection</button>
    <button class="btn" id="exportTags">Export themes</button>
    <button class="btn" id="exportCuts">Export cuts</button>
    <span class="count" id="status">1) tag photos into themes. 2) filter to a theme and cut the extras.</span>
    <span class="count" id="tally"></span>
  </div>
</header>
<div class="hint">TAG: pick a theme, click photos, "Add selected to theme" (badge appears, photo stays — can be in several). NARROW: click a filter chip to see ONLY that theme together, then click the weak/extra ones (or the ✕) to CUT them from the book. Export themes AND cuts separately.</div>
<div class="grid">
{''.join(tiles)}
</div>
<script>
{js}
</script>
</body></html>"""


_JS = r"""
const PAGE = __PAGE__;
const TAGS_KEY = 'photobook.theme.tags';       // defined theme names
const APPLY_KEY = 'photobook.theme.applied';   // asset_id -> [themes]
const CUT_KEY = 'photobook.theme.cut';         // asset_id -> 1
let activeTag = null, filter = '__untagged', lastIdx = -1;   // default: show the to-do (untagged) pile

function j(k,d){ try { return JSON.parse(localStorage.getItem(k)||d); } catch(e){ return JSON.parse(d); } }
function loadTags(){ return j(TAGS_KEY,'[]'); }
function saveTags(v){ localStorage.setItem(TAGS_KEY, JSON.stringify(v)); }
function loadApplied(){ return j(APPLY_KEY,'{}'); }
function saveApplied(v){ localStorage.setItem(APPLY_KEY, JSON.stringify(v)); }
function loadCut(){ return j(CUT_KEY,'{}'); }
function saveCut(v){ localStorage.setItem(CUT_KEY, JSON.stringify(v)); }

function tileThemes(t){
  const base=(t.dataset.tags||'').split(',').map(s=>s.trim()).filter(Boolean);
  return Array.from(new Set(base.concat(loadApplied()[t.dataset.id]||[])));
}
function renderBadges(t){ t.querySelector('.badges').innerHTML = tileThemes(t).map(x=>'<span>'+x+'</span>').join(''); }
function applyCut(t){ if (loadCut()[t.dataset.id]) t.classList.add('cut'); else t.classList.remove('cut'); }

function renderTags(){
  const tags=loadTags(), box=document.getElementById('tagList');
  box.innerHTML = tags.map(n=>'<button class="era" data-t="'+encodeURIComponent(n)+'">'+n+'</button>').join('')
    || '<span class="count">No themes yet.</span>';
  box.querySelectorAll('.era').forEach(b=>b.addEventListener('click',function(){
    box.querySelectorAll('.era').forEach(x=>x.classList.remove('active')); b.classList.add('active');
    activeTag=decodeURIComponent(b.dataset.t);
    document.getElementById('status').textContent='Theme: '+activeTag+' — select photos then "Add selected to theme".';
    updateTally();
  }));
  const fr=document.getElementById('filterRow');
  const total=document.querySelectorAll('.tile').length;
  let untag=0; document.querySelectorAll('.tile').forEach(function(t){ if(tileThemes(t).length===0) untag++; });
  let chips='<span class="filt'+(filter==='__untagged'?' on':'')+'" data-f="__untagged">Untagged ('+untag+')</span>';
  chips+='<span class="filt'+(filter===null?' on':'')+'" data-f="__all">All ('+total+')</span>';
  tags.forEach(n=>{ chips+='<span class="filt'+(filter===n?' on':'')+'" data-f="'+encodeURIComponent(n)+'">'+n+'</span>'; });
  fr.innerHTML=chips;
  fr.querySelectorAll('.filt').forEach(c=>c.addEventListener('click',function(){
    const f=c.dataset.f;
    filter=(f==='__all')?null:(f==='__untagged')?'__untagged':decodeURIComponent(f);
    doFilter(); renderTags();
  }));
}
function doFilter(){
  document.querySelectorAll('.tile').forEach(function(t){
    const th=tileThemes(t);
    let show;
    if (filter===null) show=true;                       // All
    else if (filter==='__untagged') show=th.length===0; // to-do pile
    else show=th.indexOf(filter)>=0;                    // one theme
    t.style.display = show ? '' : 'none';
  });
  updateTally();
}
function updateTally(){
  const vis=Array.from(document.querySelectorAll('.tile')).filter(t=>t.style.display!=='none');
  const sel=document.querySelectorAll('.tile.sel').length;
  const cut=vis.filter(t=>t.classList.contains('cut')).length;
  const lbl = (filter==='__untagged') ? 'untagged: ' : (filter ? ('theme "'+filter+'": ') : '');
  document.getElementById('tally').textContent =
    lbl + vis.length+' shown · '+(vis.length-cut)+' keep · '+cut+' cut · '+sel+' selected';
  document.getElementById('tagBtn').disabled = !(activeTag && sel>0);
}
document.getElementById('addTag').addEventListener('click',function(){
  const n=document.getElementById('tagName').value.trim(); if(!n) return;
  const tags=loadTags(); if(tags.indexOf(n)<0) tags.push(n); saveTags(tags);
  document.getElementById('tagName').value=''; renderTags();
});
const tiles=Array.from(document.querySelectorAll('.tile'));
tiles.forEach(function(t,i){
  // click on ✕ = cut; click on body = select (for tagging)
  t.querySelector('.cut-x').addEventListener('click',function(e){
    e.stopPropagation();
    const c=loadCut(); const id=t.dataset.id;
    if (c[id]) delete c[id]; else c[id]=1; saveCut(c); applyCut(t); updateTally();
  });
  t.addEventListener('click',function(e){
    if (e.shiftKey && lastIdx>=0){
      const a=Math.min(lastIdx,i), b=Math.max(lastIdx,i);
      for(let k=a;k<=b;k++) if(tiles[k].style.display!=='none') tiles[k].classList.add('sel');
    } else { t.classList.toggle('sel'); lastIdx=i; }
    updateTally();
  });
});
document.getElementById('clearSel').addEventListener('click',function(){
  document.querySelectorAll('.tile.sel').forEach(t=>t.classList.remove('sel')); updateTally();
});
document.getElementById('tagBtn').addEventListener('click',function(){
  const sel=Array.from(document.querySelectorAll('.tile.sel')); if(!activeTag||!sel.length) return;
  const ap=loadApplied();
  sel.forEach(function(t){
    const arr=ap[t.dataset.id]||[]; if(arr.indexOf(activeTag)<0) arr.push(activeTag);
    ap[t.dataset.id]=arr; renderBadges(t); t.classList.remove('sel');
  });
  saveApplied(ap);
  document.getElementById('status').textContent='Added "'+activeTag+'" to '+sel.length+' photos.';
  doFilter(); renderTags();   // tagged photos vanish from the Untagged pile; counts refresh
});
document.getElementById('exportTags').addEventListener('click',function(){
  // ONE combined file — firing one download per theme trips the browser's
  // multi-download block (only the first lands). All themes in a single json.
  const ap=loadApplied(); const bytheme={};
  Object.keys(ap).forEach(function(id){ ap[id].forEach(function(tag){ (bytheme[tag]=bytheme[tag]||[]).push(id); }); });
  const payload={themes:bytheme, page:PAGE, exported_at:new Date().toISOString()};
  const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='tags_all_p'+PAGE+'_'+Date.now()+'.json';
  document.body.appendChild(a); a.click(); a.remove();
  document.getElementById('status').textContent='Exported all '+Object.keys(bytheme).length+' themes in one file.';
});
document.getElementById('exportCuts').addEventListener('click',function(){
  const ids=Object.keys(loadCut());
  const payload={era:'theme_p'+PAGE, cut:ids, exported_at:new Date().toISOString()};
  const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='cut_theme_p'+PAGE+'_'+Date.now()+'.json';
  document.body.appendChild(a); a.click(); a.remove();
  document.getElementById('status').textContent='Exported '+ids.length+' cuts.';
});
renderTags(); tiles.forEach(function(t){ renderBadges(t); applyCut(t); }); doFilter();
"""
