"""Contact-sheet generator — the browser review surface (one HTML per chapter).

Each chapter gets a self-contained static HTML file at review/<chapter>.html:
  - responsive thumbnail grid over the 2560px DERIVATIVES (referenced by a
    relative file path so it works opened as file:// locally)
  - each tile shows date / source / score / flags + Accept / Reject / Hero
  - clicks persist to localStorage (keyed by chapter) so a reload is safe
  - keyboard navigation: Left/Right move focus, A/R/H set the focused tile
  - a PERSISTENT warning banner: "unsaved decisions — not yet exported" shows
    whenever localStorage holds decisions not yet downloaded since last change
    (a cross-model-review ask — localStorage alone is fragile to cache-clear)
  - "Export decisions" downloads decisions.json mapping asset_id -> accept|
    reject|hero to the browser's downloads folder (NOT written into the repo;
    the owner supplies the path to `photobook decisions --import`).

The HTML/CSS/JS is emitted inline (no build step, no external assets). An
index.html links all chapters.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Optional

from . import db, paths


def _rel_to_review(target: Path) -> str:
    """URL for a staging derivative, referenced from a review/*.html page.

    When staging lives under the repo (same drive), a relative path keeps the
    review/ dir portable. When staging is on an EXTERNAL drive (PHOTOBOOK_DATA_ROOT
    on E:/ while review/ is on C:/), a cross-drive relative path is impossible on
    Windows, so we fall back to an absolute file:// URI.
    """
    target = target.resolve()
    try:
        rel = os.path.relpath(target, start=paths.REVIEW_DIR.resolve())
        # os.path.relpath happily returns a path even across drives on POSIX,
        # but on Windows it raises ValueError for different drives. If it did
        # return something with a drive-hop (".."-only won't cross drives), the
        # browser still can't resolve it, so guard on the drive/anchor match.
        if Path(target).anchor == paths.REVIEW_DIR.resolve().anchor:
            return rel.replace("\\", "/")
    except ValueError:
        pass
    return target.as_uri()


PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 0; background: #14161a; color: #e8eaed; }
header { position: sticky; top: 0; z-index: 10; background: #1b1e24;
         padding: 12px 20px; border-bottom: 1px solid #2a2e36;
         display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
header h1 { font-size: 18px; margin: 0; font-weight: 600; }
header .counts { font-size: 13px; color: #9aa0ab; }
header .spacer { flex: 1; }
button.action { background: #2a2e36; color: #e8eaed; border: 1px solid #3a3f4a;
                border-radius: 6px; padding: 8px 14px; cursor: pointer; font-size: 13px; }
button.action:hover { background: #333842; }
button#exportBtn { background: #2f6f4f; border-color: #3c8a63; }
button#exportBtn:hover { background: #367d59; }
#banner { display: none; background: #7a4a12; color: #ffe8c9;
          padding: 8px 20px; font-size: 13px; font-weight: 600;
          border-bottom: 1px solid #9a5f1a; }
#banner.show { display: block; }
.nav { padding: 6px 20px; font-size: 12px; color: #7f8593; background:#16181d;}
.grid { display: grid; gap: 10px; padding: 16px 20px;
        grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }
.tile { background: #1b1e24; border: 2px solid #262a32; border-radius: 8px;
        overflow: hidden; position: relative; outline: none; }
.tile:focus { border-color: #6ea8fe; box-shadow: 0 0 0 2px rgba(110,168,254,.3); }
.tile.accept { border-color: #3c8a63; }
.tile.reject { border-color: #a3454a; opacity: .55; }
.tile.hero   { border-color: #d1a54a; box-shadow: 0 0 0 2px rgba(209,165,74,.35); }
.tile img { width: 100%; height: 190px; object-fit: cover; display: block; background:#0d0f12; }
.tile .meta { padding: 6px 8px; font-size: 11px; color: #9aa0ab; line-height: 1.35; }
.tile .flags { color: #d98b8b; }
.tile .btns { display: flex; border-top: 1px solid #262a32; }
.tile .btns button { flex: 1; background: transparent; color: #c7ccd4;
                     border: none; border-right: 1px solid #262a32;
                     padding: 7px 0; cursor: pointer; font-size: 12px; }
.tile .btns button:last-child { border-right: none; }
.tile .btns button.on-accept { background: #2f6f4f; color: #fff; }
.tile .btns button.on-reject { background: #8a3a3f; color: #fff; }
.tile .btns button.on-hero   { background: #b08a2f; color: #fff; }
.badge { position: absolute; top: 6px; left: 6px; background: rgba(0,0,0,.6);
         padding: 2px 6px; border-radius: 4px; font-size: 10px; }
"""


def _page_js(store_id: str) -> str:
    # localStorage key namespaced per STORE (a batch/page or a whole chapter);
    # a "dirty since export" flag per store. Each store exports its own file so
    # small batches are independent — a mistake in one can't touch another.
    return """
(function() {
  const CH = %s;
  const KEY = 'photobook.decisions.' + CH;
  const DIRTY = 'photobook.dirty.' + CH;

  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); }
                    catch(e){ return {}; } }
  function save(d) { localStorage.setItem(KEY, JSON.stringify(d)); }
  function setDirty(v){ localStorage.setItem(DIRTY, v ? '1':'0'); refreshBanner(); }
  function isDirty(){ return localStorage.getItem(DIRTY) === '1'; }

  function refreshBanner() {
    const b = document.getElementById('banner');
    const d = load();
    const n = Object.keys(d).length;
    if (n > 0 && isDirty()) { b.classList.add('show');
      b.textContent = '\\u26A0 ' + n + ' unsaved decision(s) in this chapter \\u2014 NOT yet exported. Click "Export decisions" and import the downloaded file.'; }
    else { b.classList.remove('show'); }
    refreshCounts();
  }

  function refreshCounts() {
    const d = load();
    let a=0,r=0,h=0;
    Object.values(d).forEach(v => { if(v==='accept')a++; else if(v==='reject')r++; else if(v==='hero'){h++;a++;} });
    const c = document.getElementById('counts');
    if (c) c.textContent = a + ' accept \\u00B7 ' + h + ' hero \\u00B7 ' + r + ' reject';
  }

  function applyTile(tile) {
    const d = load(); const id = tile.dataset.id; const st = d[id];
    tile.classList.remove('accept','reject','hero');
    tile.querySelectorAll('.btns button').forEach(b => b.classList.remove('on-accept','on-reject','on-hero'));
    if (st) {
      tile.classList.add(st);
      const btn = tile.querySelector('.btn-' + st);
      if (btn) btn.classList.add('on-' + st);
    }
  }

  function setState(tile, st) {
    const d = load(); const id = tile.dataset.id;
    if (d[id] === st) { delete d[id]; } else { d[id] = st; }
    save(d); setDirty(true); applyTile(tile);
  }

  window.addEventListener('DOMContentLoaded', function() {
    const tiles = Array.from(document.querySelectorAll('.tile'));
    tiles.forEach(t => {
      applyTile(t);
      t.querySelector('.btn-accept').addEventListener('click', () => setState(t,'accept'));
      t.querySelector('.btn-reject').addEventListener('click', () => setState(t,'reject'));
      t.querySelector('.btn-hero').addEventListener('click',   () => setState(t,'hero'));
    });

    let focusIdx = 0;
    if (tiles.length) tiles[0].focus();
    document.addEventListener('keydown', function(e) {
      const k = e.key.toLowerCase();
      if (k === 'arrowright') { focusIdx = Math.min(tiles.length-1, focusIdx+1); tiles[focusIdx].focus(); e.preventDefault(); }
      else if (k === 'arrowleft') { focusIdx = Math.max(0, focusIdx-1); tiles[focusIdx].focus(); e.preventDefault(); }
      else if (['a','r','h'].includes(k)) {
        const active = document.activeElement.closest ? document.activeElement.closest('.tile') : null;
        const tile = active || tiles[focusIdx];
        if (tile) setState(tile, k==='a'?'accept':k==='r'?'reject':'hero');
        e.preventDefault();
      }
    });
    tiles.forEach((t,i)=> t.addEventListener('focus', ()=> focusIdx=i));

    document.getElementById('exportBtn').addEventListener('click', function() {
      const d = load();
      const payload = { chapter: CH, decisions: d, exported_at: new Date().toISOString() };
      const blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'decisions_' + CH + '.json';
      document.body.appendChild(a); a.click(); a.remove();
      setDirty(false);   // exported → banner clears
    });

    document.getElementById('clearBtn').addEventListener('click', function() {
      if (confirm('Clear all decisions for this chapter?')) {
        localStorage.removeItem(KEY); setDirty(false);
        tiles.forEach(applyTile); refreshBanner();
      }
    });

    refreshBanner();
  });
})();
""" % json.dumps(store_id)


def _tile_html(row) -> str:
    deriv_rel = _rel_to_review(Path(row["derivative_path"]))
    name = os.path.basename(row["orig_path"]) if row["orig_path"] else row["asset_id"][:10]
    dt = (row["best_datetime"] or "")[:10]
    src = row["src"] or "?"
    score = f'{row["score"]:.2f}' if row["score"] is not None else "-"
    flags = []
    if row["is_screenshot"]:
        flags.append("screenshot")
    if row["is_document"]:
        flags.append("document")
    if row["quality_pass"] == 0:
        flags.append("low-q")
    flags_html = (f'<span class="flags">{html.escape(" ".join(flags))}</span>'
                  if flags else "")
    return f"""
    <div class="tile" tabindex="0" data-id="{html.escape(row['asset_id'])}">
      <span class="badge">{html.escape(dt)}</span>
      <img loading="lazy" src="{html.escape(deriv_rel)}" alt="{html.escape(name)}">
      <div class="meta">{html.escape(name)}<br>{html.escape(src)} · score {score} {flags_html}</div>
      <div class="btns">
        <button class="btn-accept">Accept</button>
        <button class="btn-reject">Reject</button>
        <button class="btn-hero">Hero</button>
      </div>
    </div>"""


def _render_chapter(chapter_id: str, title: str, rows: list, extra_nav: str = "",
                    store_id: Optional[str] = None) -> str:
    # store_id: the localStorage/export namespace. For a single-page chapter it's
    # the chapter id; for a paginated batch it's the per-batch id (so each batch
    # exports its own file and is fully independent).
    store = store_id or chapter_id
    tiles = "\n".join(_tile_html(r) for r in rows)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review — {html.escape(title)}</title>
<style>{PAGE_CSS}</style>
</head><body>
<header>
  <h1>{html.escape(title)}</h1>
  <span class="counts" id="counts"></span>
  <span class="spacer"></span>
  <a class="action" href="index.html" style="text-decoration:none">&#8962; All batches</a>
  <button class="action" id="clearBtn">Clear</button>
  <button class="action" id="exportBtn">Export THIS batch</button>
</header>
<div id="banner"></div>
<div class="nav">Keys: ←/→ move · A accept · R reject · H hero. This batch saves independently; click <b>Export THIS batch</b> when done, then go back for the next.</div>
{extra_nav}
<div class="grid">
{tiles}
</div>
{extra_nav}
<script>{_page_js(store)}</script>
</body></html>"""


def _render_index(chapters: list[tuple[str, str, int]]) -> str:
    # each entry is (href, title, count); href already includes ".html".
    # The store_id is the href minus ".html" (matches _page_js's CH namespace),
    # so JS can read per-batch localStorage to show progress + exported status.
    rows_json = json.dumps([
        {"href": href, "title": title, "n": n, "store": href[:-5]}
        for href, title, n in chapters
    ])
    total = sum(n for _, _, n in chapters)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Book — Review</title>
<style>{PAGE_CSS}
 .grid{{display:block}}
 .batchlist{{padding:16px 24px;max-width:820px;margin:0 auto}}
 .batch{{display:flex;align-items:center;gap:14px;padding:14px 18px;margin:8px 0;
   border:1px solid #2a2f3a;border-radius:10px;background:#161a22;text-decoration:none;color:inherit}}
 .batch:hover{{border-color:#3d6ea8;background:#1a2029}}
 .batch .num{{font-size:15px;font-weight:600;min-width:92px}}
 .batch .meta{{color:#8a93a3;font-size:13px;flex:1}}
 .batch .stat{{font-size:13px;text-align:right;min-width:150px}}
 .batch .done{{color:#3c8a63;font-weight:600}}
 .batch .pending{{color:#c9a227}}
 .batch .untouched{{color:#667}}
 .summary{{padding:8px 24px;color:#9aa4b2;max-width:820px;margin:0 auto;font-size:14px}}
 h1{{padding:0 24px}}
</style>
</head><body>
<header><h1>Photo Book — Review</h1></header>
<div class="nav">Each row below is an <b>independent batch of ~400</b>. Open one, Accept/Reject/Hero, click <b>Export THIS batch</b>, come back for the next. Batches are separate — finish them in any order, over multiple sittings. Your progress is tracked here.</div>
<div class="summary" id="summary"></div>
<div class="batchlist" id="batchlist"></div>
<script>
const BATCHES = {rows_json};
const TOTAL = {total};
function stateFor(store) {{
  let d = {{}};
  try {{ d = JSON.parse(localStorage.getItem('photobook.decisions.'+store) || '{{}}'); }} catch(e){{}}
  const dirty = localStorage.getItem('photobook.dirty.'+store);
  let a=0,r=0,h=0;
  Object.values(d).forEach(v => {{ if(v==='accept')a++; else if(v==='reject')r++; else if(v==='hero'){{h++;a++;}} }});
  const touched = Object.keys(d).length;
  // exported = has decisions AND dirty flag is '0' (was cleared on export)
  const exported = touched>0 && dirty==='0';
  return {{a,r,h,touched,exported,dirty}};
}}
function render() {{
  const list = document.getElementById('batchlist');
  let doneCount=0, touchedCount=0, totalAcc=0;
  list.innerHTML = BATCHES.map(b => {{
    const s = stateFor(b.store);
    totalAcc += s.a;
    let stat, cls;
    if (s.exported) {{ stat='\\u2713 exported \\u00B7 '+s.a+' kept'; cls='done'; doneCount++; touchedCount++; }}
    else if (s.touched>0) {{ stat='in progress \\u00B7 '+s.a+' kept, '+s.touched+' marked (NOT exported)'; cls='pending'; touchedCount++; }}
    else {{ stat='not started'; cls='untouched'; }}
    return '<a class="batch" href="'+b.href+'">'
      + '<span class="num">Batch '+(BATCHES.indexOf(b)+1)+'/'+BATCHES.length+'</span>'
      + '<span class="meta">'+b.n+' photos</span>'
      + '<span class="stat '+cls+'">'+stat+'</span></a>';
  }}).join('');
  document.getElementById('summary').textContent =
    TOTAL+' photos in '+BATCHES.length+' batches \\u00B7 '+doneCount+' exported, '
    +(touchedCount-doneCount)+' in progress, '+(BATCHES.length-touchedCount)+' not started \\u00B7 '
    +totalAcc+' kept so far';
}}
render();
window.addEventListener('focus', render);   // refresh when you come back from a batch
document.addEventListener('visibilitychange', ()=>{{ if(!document.hidden) render(); }});
</script>
</body></html>"""


def generate(
    db_path: Optional[Path] = None,
    only_shortlist: bool = True,
    include_accepted: bool = True,
    batch: Optional[str] = None,
    batch_all: bool = False,
) -> dict:
    """Write review/<chapter>.html for every chapter + review/index.html.

    only_shortlist: restrict to the scored shortlist (review_status='pending').
    include_accepted: when only_shortlist is True, ALSO include already-accepted
      photos (review_status='accept') so a re-generated sheet still shows — and,
      via localStorage, re-marks — everything the owner already picked. Prevents a
      re-score/re-review from dropping prior accepts off the page.
    batch: NET-NEW review. When set, show ONLY photos whose ingest_batch == this
      label (i.e. the ones a specific ingest run introduced). This is the "only
      show me the new photos" path — the owner never re-scrolls parts he already
      reviewed. include_accepted is ignored in batch mode (a fresh batch has no
      accepts yet, and prior accepts live in other batches).
    """
    paths.REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    ch_cfg = __import__("yaml").safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    chapter_defs = ch_cfg.get("chapters", []) or []
    title_by_id = {c["id"]: c.get("title", c["id"]) for c in chapter_defs}

    where = "is_dup_keep=1"
    params_extra: list = []
    if batch is not None and batch_all:
        # Whole-batch: every quality-passing keeper in the batch that isn't already
        # rejected — bypasses the per-chapter shortlist cap so a curated source
        # (e.g. blog photos competing against a huge same-period Google pile) is
        # shown in full. Accepted photos stay marked (localStorage).
        where += (" AND ingest_batch=? AND quality_pass=1 "
                  "AND (review_status IS NULL OR review_status IN ('pending','accept'))")
        params_extra.append(batch)
    elif batch is not None:
        # Net-new: only this batch's still-undecided SHORTLISTED photos.
        where += " AND ingest_batch=? AND review_status='pending'"
        params_extra.append(batch)
    elif only_shortlist:
        if include_accepted:
            where += " AND review_status IN ('pending','accept')"
        else:
            where += " AND review_status='pending'"

    # Clean slate: remove any existing per-chapter sheets so a run never leaves
    # STALE sheets from a prior (e.g. different-batch) generation on disk. Without
    # this, `review --batch B2` would leave B1's sheets for chapters B2 didn't
    # touch, and the index/opened pages would mix old photos into a "net-new" view.
    for old in paths.REVIEW_DIR.glob("[0-9][0-9]_*.html"):
        old.unlink()

    # Pagination: a single HTML sheet with thousands of image tiles will crawl or
    # crash a browser. Split any chapter over `page_size` into <cid>_pN.html pages,
    # each cross-linked. The localStorage decision KEY is per-chapter (not per-page),
    # so decisions carry across a chapter's pages correctly.
    PAGE_SIZE = 400
    written: list[tuple[str, str, int]] = []      # (href, title, count) for the index
    with db.session(db_path) as conn:
        for c in chapter_defs:
            cid = c["id"]
            rows = conn.execute(
                f"SELECT * FROM assets WHERE chapter=? AND {where} "
                "ORDER BY best_datetime, score DESC",
                (cid, *params_extra),
            ).fetchall()
            if not rows:
                continue
            title = title_by_id.get(cid, cid)
            n_pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
            if n_pages <= 1:
                out = paths.REVIEW_DIR / f"{cid}.html"
                out.write_text(_render_chapter(cid, title, rows), encoding="utf-8")
                written.append((f"{cid}.html", title, len(rows)))
            else:
                # Each batch is INDEPENDENT: its own file, its own store_id (so its
                # own localStorage decisions + its own export file). A mistake in
                # one batch can't touch another, and you export/import batch-by-batch.
                page_files = [f"{cid}_b{p+1}.html" for p in range(n_pages)]
                for p in range(n_pages):
                    chunk = rows[p * PAGE_SIZE:(p + 1) * PAGE_SIZE]
                    store = f"{cid}_b{p+1}"
                    nav = _page_nav(page_files, p)
                    btitle = f"{title} — batch {p+1} of {n_pages}"
                    (paths.REVIEW_DIR / page_files[p]).write_text(
                        _render_chapter(cid, btitle, chunk, extra_nav=nav, store_id=store),
                        encoding="utf-8")
                    written.append((page_files[p], btitle, len(chunk)))

    (paths.REVIEW_DIR / "index.html").write_text(_render_index(written), encoding="utf-8")
    return {
        "chapters_written": len(written),
        "files": [f"review/{href}" for href, _, _ in written],
        "index": "review/index.html",
    }


def _page_nav(page_files: list[str], current: int) -> str:
    """Prev/next + batch-number links across the batches of one chapter."""
    links = []
    if current > 0:
        links.append(f'<a href="{page_files[current-1]}">&larr; prev batch</a>')
    for i, pf in enumerate(page_files):
        if i == current:
            links.append(f'<b>{i+1}</b>')
        else:
            links.append(f'<a href="{pf}">{i+1}</a>')
    if current < len(page_files) - 1:
        links.append(f'<a href="{page_files[current+1]}">next batch &rarr;</a>')
    return ('<div class="pagenav" style="padding:8px 16px;font-size:14px">batch: '
            + ' &nbsp; '.join(links) + '</div>')
