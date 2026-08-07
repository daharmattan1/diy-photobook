"""Event-sweeper — group photos into named, dated EVENTS by eye.

Why: eras give a photo its chapter, but not its ORDER inside that chapter — and
for date-stripped sources there's no reliable timestamp at all. Events fix both:
you name an event with a rough date ("A Friend's Wedding, Oct 2023"), click all
its photos, submit — and every photo in it inherits that date (ordering) plus an
event label (narrative grouping / spreads).

Examples of good events: a family wedding · a milestone birthday · a big trip ·
a summer at the beach · a move to a new home. Anything the photos naturally
cluster around and you can date, even roughly.

Interaction mirrors the era-sorter: define/pick an event, click its photos,
Submit → they vanish + a sweep downloads. Photos can be left unassigned — not
every photo belongs to an event.

Import:  photobook eventsort --import <file-or-dir>
  -> writes `event` (label) and, when the event has a date, `best_datetime`
     + date_source='event' on those assets.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db, paths
from .erasort import GRID_CSS


def generate(db_path: Optional[Path] = None, page_size: int = 300,
             eras: Optional[list[str]] = None) -> dict:
    """Write review/eventsort_pN.html over accepted photos, grouped by era.

    eras: restrict to these chapter ids. Some eras are already coherently
      organised (e.g. a single trip), so event-grouping them is wasted clicks —
      restrict to the chapters where distinct named events actually cluster.
    """
    paths.REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    where = "review_status='accept'"
    params: list = []
    if eras:
        where += " AND chapter IN (" + ",".join("?" for _ in eras) + ")"
        params.extend(eras)

    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, derivative_path, orig_path, src, chapter, event "
            f"FROM assets WHERE {where} ORDER BY chapter, orig_path", params
        ).fetchall()

    for old in paths.REVIEW_DIR.glob("eventsort_*.html"):
        old.unlink()

    n_pages = max(1, (len(rows) + page_size - 1) // page_size)
    files = [f"eventsort_p{i+1}.html" for i in range(n_pages)]
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
        name = os.path.basename(r["orig_path"] or "")[:24]
        ch = (r["chapter"] or "")[3:].replace("_", " ")[:16]
        tiles.append(
            f'<div class="tile" data-id="{html.escape(r["asset_id"])}">'
            f'<img loading="lazy" src="{html.escape(_rel(Path(r["derivative_path"])))}" alt="">'
            f'<div class="cap">{html.escape(ch)} · {html.escape(name)}</div></div>'
        )
    nav = " ".join(
        (f"<b>{i+1}</b>" if i == page_idx else f'<a href="{f}" style="color:#6ea8fe">{i+1}</a>')
        for i, f in enumerate(files)
    )
    js = _JS.replace("__PAGE__", str(page_idx + 1))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Event sort — page {page_idx+1}/{len(files)}</title>
<style>{GRID_CSS}
 .newev {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px; }}
 .newev input {{ background:#161a22; border:1px solid #2a2f3a; border-radius:8px;
   color:#e6e9ef; padding:7px 10px; font-size:13px; }}
 .newev input#evName {{ min-width:230px; }}
 .newev input#evDate {{ width:150px; }}
</style></head><body>
<header>
  <h1>Group into events — page {page_idx+1}/{len(files)} &nbsp;<span class="count">{nav}</span></h1>
  <div class="newev">
    <input id="evName" placeholder="Event name (e.g. Jenny's Wedding)">
    <input id="evDate" placeholder="Date: 2023-10 or 2023-10-14">
    <button class="btn" id="addEv">+ Add event</button>
    <span class="count">then pick it below</span>
  </div>
  <div class="eras" id="evList" style="margin-top:10px"></div>
  <div class="bar">
    <button class="btn primary" id="submitBtn" disabled>Submit selected to event</button>
    <button class="btn" id="clearSel">Clear selection</button>
    <span class="count" id="status">Add an event, pick it, then click its photos.</span>
    <span class="count" id="remain"></span>
  </div>
</header>
<div class="hint">Not every photo needs an event — only group the ones that belong to a real moment (a wedding, a trip, a summer at the beach). Photos inherit the event's date, which fixes their order inside the chapter. Shift-click for ranges.</div>
<div class="grid" id="grid">
{''.join(tiles)}
</div>
<script>
{js}
</script>
</body></html>"""


_JS = r"""
const PAGE = __PAGE__;
const DONE_KEY = 'photobook.eventsort.done';   // asset_id -> event name
const EVS_KEY  = 'photobook.eventsort.events'; // [{name, date}]
let currentEv = null, lastIdx = -1;

function loadDone(){ try { return JSON.parse(localStorage.getItem(DONE_KEY)||'{}'); } catch(e){ return {}; } }
function saveDone(d){ localStorage.setItem(DONE_KEY, JSON.stringify(d)); }
function loadEvs(){ try { return JSON.parse(localStorage.getItem(EVS_KEY)||'[]'); } catch(e){ return []; } }
function saveEvs(v){ localStorage.setItem(EVS_KEY, JSON.stringify(v)); }

function renderEvs(){
  const evs = loadEvs();
  const box = document.getElementById('evList');
  box.innerHTML = evs.map(function(e){
    const lbl = e.name + (e.date ? ' · ' + e.date : '');
    return '<button class="era" data-ev="' + encodeURIComponent(e.name) + '">' + lbl + '</button>';
  }).join('') || '<span class="count">No events yet — add one above.</span>';
  box.querySelectorAll('.era').forEach(function(b){
    b.addEventListener('click', function(){
      box.querySelectorAll('.era').forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      currentEv = decodeURIComponent(b.dataset.ev);
      document.getElementById('status').textContent = 'Event: ' + currentEv + ' — click photos, then Submit.';
      updateRemain();
    });
  });
}
document.getElementById('addEv').addEventListener('click', function(){
  const n = document.getElementById('evName').value.trim();
  const d = document.getElementById('evDate').value.trim();
  if (!n) return;
  const evs = loadEvs();
  if (!evs.some(function(e){ return e.name === n; })) evs.push({ name: n, date: d });
  saveEvs(evs);
  document.getElementById('evName').value = ''; document.getElementById('evDate').value = '';
  renderEvs();
});
function hideDone(){
  const done = loadDone();
  document.querySelectorAll('.tile').forEach(function(t){ if (done[t.dataset.id]) t.style.display='none'; });
  updateRemain();
}
function updateRemain(){
  const vis = Array.from(document.querySelectorAll('.tile')).filter(function(t){ return t.style.display!=='none'; }).length;
  const sel = document.querySelectorAll('.tile.sel').length;
  document.getElementById('remain').textContent = vis + ' ungrouped on this page · ' + sel + ' selected';
  document.getElementById('submitBtn').disabled = !(currentEv && sel > 0);
}
const tiles = Array.from(document.querySelectorAll('.tile'));
tiles.forEach(function(t,i){
  t.addEventListener('click', function(e){
    if (e.shiftKey && lastIdx >= 0) {
      const a = Math.min(lastIdx,i), b = Math.max(lastIdx,i);
      for (let k=a;k<=b;k++) if (tiles[k].style.display!=='none') tiles[k].classList.add('sel');
    } else { t.classList.toggle('sel'); lastIdx = i; }
    updateRemain();
  });
});
document.getElementById('clearSel').addEventListener('click', function(){
  document.querySelectorAll('.tile.sel').forEach(function(t){ t.classList.remove('sel'); }); updateRemain();
});
document.getElementById('submitBtn').addEventListener('click', function(){
  const sel = Array.from(document.querySelectorAll('.tile.sel'));
  if (!currentEv || !sel.length) return;
  const ids = sel.map(function(t){ return t.dataset.id; });
  const done = loadDone(); ids.forEach(function(id){ done[id] = currentEv; }); saveDone(done);
  const ev = loadEvs().find(function(e){ return e.name === currentEv; }) || { name: currentEv, date: '' };
  const payload = { event: ev.name, date: ev.date || '', asset_ids: ids, page: PAGE, exported_at: new Date().toISOString() };
  const blob = new Blob([JSON.stringify(payload,null,2)], {type:'application/json'});
  const a = document.createElement('a');
  const slug = ev.name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');
  a.href = URL.createObjectURL(blob);
  a.download = 'event_' + slug + '_p' + PAGE + '_' + Date.now() + '.json';
  document.body.appendChild(a); a.click(); a.remove();
  sel.forEach(function(t){ t.classList.remove('sel'); t.style.display='none'; });
  document.getElementById('status').textContent = 'Submitted ' + ids.length + ' to "' + ev.name + '". Pick the next event.';
  updateRemain();
});
renderEvs(); hideDone();
"""


def import_events(path: Path, db_path: Optional[Path] = None) -> dict:
    """Import event_*.json sweeps: set `event` label and, if dated, best_datetime."""
    files = []
    if path.is_dir():
        files = sorted(path.glob("event_*.json"))
    elif path.is_file():
        files = [path]
    if not files:
        raise FileNotFoundError(f"no event_*.json found at {path}")

    applied = dated = 0
    per_event: dict[str, int] = {}
    with db.session(db_path) as conn:
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            name = data.get("event")
            ids = data.get("asset_ids", []) or []
            raw = (data.get("date") or "").strip()
            iso = _parse_event_date(raw)
            if not name:
                continue
            for aid in ids:
                if iso:
                    cur = conn.execute(
                        "UPDATE assets SET event=?, best_datetime=?, "
                        "date_source='event', date_confidence='medium' WHERE asset_id=?",
                        (name, iso, aid))
                    if cur.rowcount:
                        dated += 1
                else:
                    cur = conn.execute(
                        "UPDATE assets SET event=? WHERE asset_id=?", (name, aid))
                if cur.rowcount:
                    applied += 1
                    per_event[name] = per_event.get(name, 0) + 1
    return {"files": len(files), "applied": applied, "dated": dated,
            "per_event": per_event}


def _parse_event_date(raw: str) -> Optional[str]:
    """Accept 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD' -> ISO datetime (noon sentinel)."""
    raw = raw.strip()
    for fmt, iso in (("%Y-%m-%d", "%Y-%m-%dT12:00:00"),
                     ("%Y-%m", "%Y-%m-15T12:00:00"),
                     ("%Y", "%Y-07-01T12:00:00")):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime(iso)
        except ValueError:
            continue
    return None
