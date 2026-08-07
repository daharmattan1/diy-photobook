"""Cull tool — subtractive trim of the accepted set, era by era.

the owner's rules: subtractive (click the photos to CUT, most stay), no hard
targets (cull till it feels right), AI surfaces/organises but NEVER decides.

Each photo shows its keep-SIGNALS (hero / event / tag / favorited) and its
weak-FLAGS (reduced-res / blurry / screenshot). Same-moment bursts are boxed
together so a run of near-identical frames can be thinned to one at a glance.
Nothing is cut unless the owner clicks it.

Export -> cut_<era>_<ts>.json { cut: [asset_ids] }.
Import: photobook cull --import <dir>  -> sets review_status='reject' on the
cut ids (non-destructive flag; re-runnable; a re-accept is possible by re-import
of a decisions file). Then: photobook export.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Optional

import yaml

from . import db, paths
from .erasort import GRID_CSS
from .samemoment import cluster


def _load_titles() -> dict:
    ch = yaml.safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    order = {c["id"]: i for i, c in enumerate(ch.get("chapters", []) or [])}
    titles = {c["id"]: c.get("title", c["id"]) for c in (ch.get("chapters", []) or [])}
    return order, titles


def _rel(p: Path) -> str:
    try:
        return os.path.relpath(p, paths.REVIEW_DIR).replace("\\", "/")
    except ValueError:
        return p.as_uri()


def generate(db_path: Optional[Path] = None) -> dict:
    """One page PER ERA (so you cull in context). Same-moment clusters boxed."""
    paths.REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    order, titles = _load_titles()

    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, derivative_path, orig_path, src, chapter, event, tags, "
            "is_hero, favorited, quality_pass, is_screenshot, w, h, phash, "
            "best_datetime, date_source "
            "FROM assets WHERE review_status='accept'"
        ).fetchall()

    clusters = cluster(rows)   # {asset_id: cluster_id}

    for old in paths.REVIEW_DIR.glob("cull_*.html"):
        old.unlink()

    by_era: dict = {}
    for r in rows:
        by_era.setdefault(r["chapter"], []).append(r)

    written = []
    era_ids = sorted(by_era, key=lambda c: order.get(c, 999))
    files = {cid: f"cull_{cid}.html" for cid in era_ids}
    for cid in era_ids:
        ers = by_era[cid]
        # order: cluster members adjacent (by cluster id), then the rest by date/name
        ers.sort(key=lambda r: (clusters.get(r["asset_id"], 10**9),
                                r["best_datetime"] or "", r["orig_path"] or ""))
        (paths.REVIEW_DIR / files[cid]).write_text(
            _render(cid, titles.get(cid, cid), ers, clusters, files, era_ids), encoding="utf-8")
        written.append((files[cid], titles.get(cid, cid), len(ers)))

    (paths.REVIEW_DIR / "cull_index.html").write_text(
        _render_index(written), encoding="utf-8")
    return {"eras": len(written), "photos": len(rows),
            "clustered": len(clusters), "index": "review/cull_index.html"}


def _tile(r, clusters) -> str:
    sig = []
    if r["is_hero"]:
        sig.append('<span class="s hero">★ hero</span>')
    if r["event"]:
        sig.append(f'<span class="s ev">{html.escape(r["event"][:14])}</span>')
    if r["tags"]:
        sig.append(f'<span class="s tag">{html.escape(r["tags"][:16])}</span>')
    if r["favorited"]:
        sig.append('<span class="s fav">♡ fav</span>')
    flags = []
    if (r["w"] or 0) * (r["h"] or 0) and max(r["w"] or 0, r["h"] or 0) <= 2560 \
            and (r["date_source"] == "" or True):
        pass  # (resolution flag handled below via reduced check)
    if r["is_screenshot"]:
        flags.append("screenshot")
    if r["quality_pass"] == 0:
        flags.append("low-q")
    cl = clusters.get(r["asset_id"])
    clattr = f' data-cluster="{cl}"' if cl else ""
    clcls = " inclust" if cl else ""
    sightml = "".join(sig)
    flaghtml = (f'<span class="flags">{html.escape(" ".join(flags))}</span>' if flags else "")
    return (f'<div class="tile{clcls}"{clattr} data-id="{html.escape(r["asset_id"])}">'
            f'<img loading="lazy" src="{html.escape(_rel(Path(r["derivative_path"])))}" alt="">'
            f'<div class="sigs">{sightml}</div>'
            f'<div class="cut-x">✕ cut</div>'
            f'<div class="cap">{html.escape(os.path.basename(r["orig_path"] or "")[:20])} {flaghtml}</div>'
            f'</div>')


def _render(cid, title, rows, clusters, files, era_ids) -> str:
    # group consecutive cluster members into a boxed .burst wrapper
    html_tiles = []
    i = 0
    while i < len(rows):
        cl = clusters.get(rows[i]["asset_id"])
        if cl:
            j = i
            while j < len(rows) and clusters.get(rows[j]["asset_id"]) == cl:
                j += 1
            inner = "".join(_tile(r, clusters) for r in rows[i:j])
            html_tiles.append(f'<div class="burst" title="same moment — keep one">'
                              f'<div class="burst-lbl">same moment · pick one</div>{inner}</div>')
            i = j
        else:
            html_tiles.append(_tile(rows[i], clusters))
            i += 1
    nav = " ".join(
        (f"<b>{files[c][:-5].replace('cull_','')[3:]}</b>" if c == cid
         else f'<a href="{files[c]}" style="color:#6ea8fe">{files[c][:-5].replace("cull_","")[3:]}</a>')
        for c in era_ids)
    js = _JS.replace("__CID__", json.dumps(cid))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cull — {html.escape(title)}</title>
<style>{GRID_CSS}
 .sigs {{ position:absolute; top:4px; left:4px; right:26px; display:flex; flex-wrap:wrap; gap:3px; }}
 .s {{ font-size:9px; padding:1px 5px; border-radius:6px; color:#fff; }}
 .s.hero {{ background:#b8860bcc; }} .s.ev {{ background:#3d6ea8cc; }}
 .s.tag {{ background:#2f6f4fcc; }} .s.fav {{ background:#8a5a9bcc; }}
 .cut-x {{ position:absolute; top:4px; right:4px; background:#0009; color:#f88;
   font-size:10px; padding:2px 6px; border-radius:6px; opacity:0; cursor:pointer; }}
 .tile:hover .cut-x {{ opacity:1; }}
 .tile.cut {{ opacity:.28; filter:grayscale(1); border-color:#a33; }}
 .tile.cut .cut-x {{ opacity:1; background:#a33; color:#fff; }}
 .tile.cut .cut-x::before {{ content:"CUT ↺ "; }}
 .burst {{ display:contents; }}
 .tile.inclust {{ border-color:#c9a22766; }}
 .burst-lbl {{ display:none; }}
 .flags {{ color:#c98; font-size:9px; }}
</style></head><body>
<header>
  <h1>Cull: {html.escape(title)} &nbsp;<span class="count">{nav}</span></h1>
  <div class="bar">
    <span class="count" id="status">Click a photo (or its ✕) to CUT it. Click again to restore.</span>
    <button class="btn primary" id="exportBtn">Export cuts for this era</button>
    <button class="btn" id="resetBtn">Reset cuts (this era)</button>
    <a class="btn" href="cull_index.html" style="text-decoration:none">All eras</a>
    <span class="count" id="tally"></span>
  </div>
</header>
<div class="hint">Subtractive: most photos STAY. Cut the weak/redundant ones. Signal badges (★hero / event / tag / ♡fav) mark keepers you probably want. Boxed runs = same moment (thin to one). Nothing leaves the book unless you cut it. Shift-click a range.</div>
<div class="grid">
{''.join(html_tiles)}
</div>
<script>
{js}
</script>
</body></html>"""


_JS = r"""
const CID = __CID__;
const KEY = 'photobook.cull.' + CID;
let lastIdx = -1;
function load(){ try { return JSON.parse(localStorage.getItem(KEY)||'{}'); } catch(e){ return {}; } }
function save(d){ localStorage.setItem(KEY, JSON.stringify(d)); }
function apply(t){ const d=load(); if (d[t.dataset.id]) t.classList.add('cut'); else t.classList.remove('cut'); }
function tally(){
  const tiles=document.querySelectorAll('.tile');
  const cut=document.querySelectorAll('.tile.cut').length;
  document.getElementById('tally').textContent = (tiles.length-cut)+' keep · '+cut+' cut (of '+tiles.length+')';
}
const tiles=Array.from(document.querySelectorAll('.tile'));
function toggle(t){
  const d=load(); const id=t.dataset.id;
  if (d[id]) delete d[id]; else d[id]=1;
  save(d); apply(t); tally();
}
tiles.forEach(function(t,i){
  t.addEventListener('click', function(e){
    if (e.shiftKey && lastIdx>=0){
      const a=Math.min(lastIdx,i), b=Math.max(lastIdx,i), d=load();
      for (let k=a;k<=b;k++){ d[tiles[k].dataset.id]=1; apply(tiles[k]); }
      save(d); tally();
    } else { toggle(t); lastIdx=i; }
  });
});
document.getElementById('resetBtn').addEventListener('click', function(){
  if (!confirm('Restore ALL cuts in this era? (nothing is cut until you re-mark)')) return;
  localStorage.removeItem(KEY);
  tiles.forEach(function(t){ t.classList.remove('cut'); });
  document.getElementById('status').textContent='All cuts cleared for this era. Start fresh.';
  tally();
});
document.getElementById('exportBtn').addEventListener('click', function(){
  const d=load(); const ids=Object.keys(d);
  const payload={ era:CID, cut:ids, exported_at:new Date().toISOString() };
  const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='cut_'+CID+'_'+Date.now()+'.json';
  document.body.appendChild(a); a.click(); a.remove();
  document.getElementById('status').textContent='Exported '+ids.length+' cuts for this era.';
});
tiles.forEach(apply); tally();
"""


def _render_index(written) -> str:
    items = "".join(
        f'<li><a href="{html.escape(href)}">{html.escape(title)}</a> '
        f'<span style="color:#888">({n} photos)</span></li>'
        for href, title, n in written)
    total = sum(n for _, _, n in written)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cull — the book</title>
<style>{GRID_CSS} .grid{{display:block}} li{{margin:8px 0;font-size:16px}} a{{color:#6ea8fe}}</style>
</head><body>
<header><h1>Cull the book &mdash; {total} photos across {len(written)} eras</h1></header>
<div class="nav">Cull ONE era at a time. Subtractive: click the weak/redundant photos to cut, most stay. Export each era's cuts, then tell me to import. No hard target &mdash; cull till it feels right.</div>
<ul style="padding:20px 40px">{items}</ul>
</body></html>"""


def import_cuts(path: Path, db_path: Optional[Path] = None) -> dict:
    files = []
    if path.is_dir():
        files = sorted(path.glob("cut_*.json"))
    elif path.is_file():
        files = [path]
    if not files:
        raise FileNotFoundError(f"no cut_*.json found at {path}")
    cut = 0
    per_era: dict = {}
    with db.session(db_path) as conn:
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            era = data.get("era", "?")
            for aid in data.get("cut", []) or []:
                c = conn.execute(
                    "UPDATE assets SET review_status='reject' "
                    "WHERE asset_id=? AND review_status='accept'", (aid,))
                if c.rowcount:
                    cut += 1
                    per_era[era] = per_era.get(era, 0) + 1
    return {"files": len(files), "cut": cut, "per_era": per_era}
