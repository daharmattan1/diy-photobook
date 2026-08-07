"""Smart-fill layout pass — a REVERSIBLE post-process that removes the ugly
white margins the contain-cap composer leaves, while PROTECTING SHARPNESS and
NEVER cutting a face.

The composer sizes each cell to its photo's aspect and caps the scale at
`min(1.0, usable/natural)` (contain), so two portraits on a 12in page become a
narrow centered column with wide white sides. This pass rewrites each content
page's geometry to a JUSTIFIED-ROWS fill (rows span the full content width with
zero crop), then adds a LIGHT face-safe cover-crop per cell to also fill height.

The three guardrails:
  1. Crop cap        — never remove more than the cap fraction of a photo's
                       area; where a full fill would need more, leave modest
                       EVEN margin.
  2. Faces           — a cover-crop window must contain every detected face box
                       (Haar, via photobook.faces). Faces clipped => the crop is
                       refused and the cell falls back to contain. 0 clips or we
                       refuse to save.
  3. Sharpness (DPI) — a photo that would print below the soft floor at its fill
                       size is put in a SMALLER, contain-fit cell (all its
                       pixels kept) rather than blown up softly. Low-res photos
                       stay small.

Reversibility: this only mutates the in-memory book fetched from the running
editor (GET /api/book) and saves it once (POST /api/book). It does NOT touch
compose()/_row_grid/_column_grid — a recompose rebuilds the original geometry
from the manifest, discarding this pass entirely.

Usage:
  python scripts/smart_fill.py            # measure + save (editor must be running)
  python scripts/smart_fill.py --dry-run  # measure only, no save
  python scripts/smart_fill.py --file     # operate on book.json directly

The editor server also imports ``fill_page`` directly for its per-page
relayout endpoint, so keep this module import-safe (no side effects).
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from photobook import bookmodel as bm
from photobook import db, faces
from photobook.bookmodel import cell_asset, cell_crop, effective_dpi, make_cell
from photobook.bookrender import CROPS as OVERRIDE_CROPS

BASE = os.environ.get("PHOTOBOOK_EDITOR_URL", "http://127.0.0.1:8765")

# Geometry (single source of truth: bookmodel).
PAGE_IN = bm.PAGE_IN
M = bm.M
G = bm.G
TITLE_H = bm.TITLE_H
PAGE_AREA = PAGE_IN * PAGE_IN

SOFT_DPI = 110.0           # soft sharpness floor for the fill. Deliberately below
                            # the 240 hard print floor: current low resolution
                            # should not cap the LAYOUT when sharper originals may
                            # swap in later — design the sizes you want. Only
                            # truly tiny photos stay contained. The exporter still
                            # reports every sub-floor placement.
MAX_CROP_FRAC = 0.45        # a cover-crop may remove up to ~45% to FILL its cell —
                            # face safety is enforced separately (a crop that clips a
                            # detected face is refused and falls back to contain), so
                            # this lets scenery/wide shots fill instead of leaving
                            # white bands, without butchering people.
CAUTIOUS_CROP_FRAC = 0.12   # tighter cap when we suspect undetected faces
BLIND_CROP_FRAC = 0.15      # 0 detected faces: cap the cover-crop hard so an
                            # UNDETECTED subject (profile/distant/dark) can't be
                            # cut out of frame — the "0 faces clipped" gate can
                            # only protect a face it saw.
FILL_CROP_FRAC = 0.55       # a whole row-stack may grow (crop) up to this to FILL
                            # page height; each cell's own crop is still face-
                            # checked in resolve_cell, so this fills more pages
                            # without loosening per-cell subject safety.
EPS = 1e-6

# Assets with hand-made override crops (bookrender.CROPS, populated from
# fixes/overrides.json). Never overlay a cover-crop on those — leave them
# contain so the curated crop is preserved.
NO_COVER = set(OVERRIDE_CROPS)


# --- editor API ---------------------------------------------------------------
def api_get(path: str):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def api_post(path: str, body: dict):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())


# --- face-safe crop geometry --------------------------------------------------
def cover_crop(pw: float, ph: float, target_aspect: float, facelist,
               center_on_faces: bool = True) -> dict:
    """Normalized crop window {x,y,w,h} of aspect `target_aspect` that cover-fills
    a cell; centered on the detected faces' centroid (or the image center), then
    nudged so every face stays inside the window where geometrically possible.
    The window aspect exactly equals `target_aspect`, so the exporter's residual
    cover-crop is a no-op and the persisted window is what prints."""
    photo_aspect = pw / max(EPS, ph)
    if photo_aspect >= target_aspect:      # wider than the cell -> crop width
        cw = target_aspect / photo_aspect
        ch = 1.0
    else:                                   # taller than the cell -> crop height
        cw = 1.0
        ch = photo_aspect / target_aspect
    cw = min(1.0, cw)
    ch = min(1.0, ch)
    if facelist and center_on_faces:
        fx = sum((f[0] + f[2]) / 2 for f in facelist) / len(facelist)
        fy = sum((f[1] + f[3]) / 2 for f in facelist) / len(facelist)
    else:
        fx = fy = 0.5
    x = min(max(fx - cw / 2, 0.0), 1.0 - cw)
    y = min(max(fy - ch / 2, 0.0), 1.0 - ch)
    for fx0, fy0, fx1, fy1 in facelist:
        if fx0 < x:
            x = max(0.0, min(fx0, 1.0 - cw))
        if fx1 > x + cw:
            x = min(1.0 - cw, max(x, fx1 - cw))
        if fy0 < y:
            y = max(0.0, min(fy0, 1.0 - ch))
        if fy1 > y + ch:
            y = min(1.0 - ch, max(y, fy1 - ch))
    return {"x": round(x, 4), "y": round(y, 4), "w": round(cw, 4), "h": round(ch, 4)}


def clips_any_face(crop: dict, facelist) -> bool:
    """True if the crop window cuts through any face box (a face not fully inside)."""
    if not crop:
        return False
    x0, y0 = crop["x"], crop["y"]
    x1, y1 = x0 + crop["w"], y0 + crop["h"]
    for fx0, fy0, fx1, fy1 in facelist:
        if fx0 < x0 - EPS or fy0 < y0 - EPS or fx1 > x1 + EPS or fy1 > y1 + EPS:
            return True
    return False


# --- per-cell resolution: cover (fill) vs contain (protect) -------------------
def _contain_cell(ph: dict, box, floor: float = SOFT_DPI):
    """Return (crop=None, fit, geometry): the largest photo-aspect rect inside
    `box`, centered, then shrunk if needed so a CONTAIN fit still prints >= floor
    DPI. Keeps every source pixel (no crop) — the right home for a low-res photo.

    HERO WAIVER: a hero-marked photo is prominent REGARDLESS of resolution (it
    may print a touch soft — that is the owner's call, and upscaling can fix the
    ones that go full page; see docs/UPSCALING.md). No DPI shrink for heroes —
    a hard floor maroons low-res heroes as small photos adrift in white."""
    x, y, w, h = box
    if ph.get("is_hero"):
        floor = 0.0
    a = (ph.get("w") or 1) / max(1, ph.get("h") or 1)
    if w / max(EPS, h) >= a:               # box wider than photo -> limit by height
        bh = h
        bw = h * a
    else:                                   # box taller than photo -> limit by width
        bw = w
        bh = w / a
    dpi = min((ph.get("w") or 0) / max(EPS, bw), (ph.get("h") or 0) / max(EPS, bh))
    if floor and dpi and dpi < floor:
        s = dpi / floor
        bw *= s
        bh *= s
    nx = x + (w - bw) / 2
    ny = y + (h - bh) / 2
    return None, "contain", [round(nx, 4), round(ny, 4), round(bw, 4), round(bh, 4)]


def resolve_cell(aid: str, ph: dict, box, facelist):
    """Decide how one photo occupies its allotted `box`.

    Returns (crop|None, fit, geometry). Cover-fill only when it is BOTH face-safe
    and sharp (>= SOFT_DPI) and within the crop cap; otherwise contain (protect
    sharpness / faces), which turns the unused area into modest even margin.
    """
    x, y, w, h = box
    target_aspect = w / max(EPS, h)

    people = ph.get("people_count")
    detected = len(facelist)
    cautious = people is not None and people > detected

    # Override-cropped assets + pre-assembled collages/galleries: never overlay
    # a cover-crop (it would slice the grid or defeat the curated crop). Show
    # them whole.
    _name = os.path.basename(ph.get("orig_path") or "")
    if aid in NO_COVER or "COLLAGE" in _name.upper() or "_all_" in _name.lower():
        return _contain_cell(ph, box)

    center_on_faces = not cautious              # suspect undetected faces -> center
    if cautious:
        cap = CAUTIOUS_CROP_FRAC
    elif detected == 0:
        cap = BLIND_CROP_FRAC                    # no detected face to protect
    else:
        cap = MAX_CROP_FRAC
    crop = cover_crop(ph.get("w") or 1, ph.get("h") or 1, target_aspect, facelist,
                      center_on_faces=center_on_faces)
    removed = 1.0 - crop["w"] * crop["h"]
    dpi = effective_dpi(ph, [x, y, w, h], crop)
    # Heroes are prominent regardless of resolution — the sharpness veto is
    # waived (face safety + crop caps still hold; export still flags low DPI).
    sharp_ok = dpi >= SOFT_DPI or bool(ph.get("is_hero"))
    if (removed <= cap + EPS
            and not clips_any_face(crop, facelist)
            and sharp_ok):
        return crop, "cover", [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]
    return _contain_cell(ph, box)


# --- candidate row groupings --------------------------------------------------
def _candidate_rowings(n: int) -> list[list[int]]:
    """Sequence-preserving row groupings (each entry = photos in that row).

    Reuses the composer's curated TRACE_PATTERNS partitions for the photo count
    when available (they are the composer's layout recipes read as rows), and
    always includes a generated fallback set (1-3 rows, <=4 per row) so any count
    still has candidates.
    """
    from photobook.bookdef import TRACE_PATTERNS, _layout_style

    candidates: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()

    def add(rowsizes):
        key = tuple(rowsizes)
        # Cap any row at 3 photos: a 4-across row on a 12in page is 3in cells = tiny.
        if key and sum(rowsizes) == n and key not in seen and max(rowsizes) <= 3:
            seen.add(key)
            candidates.append(list(rowsizes))

    if 1 <= n <= 7:
        try:
            for part in TRACE_PATTERNS.get(_layout_style(n, side="left", opener=False), []):
                add(part)
        except ValueError:
            pass

    def gen(remaining: int, parts: list[int]):
        if remaining == 0:
            add(parts)
            return
        if len(parts) >= 3:
            return
        for size in range(1, min(3, remaining) + 1):   # rows of at most 3
            gen(remaining - size, parts + [size])

    gen(n, [])
    return candidates or [[n]]


def _rows_of(seq, rowsizes):
    out, cur = [], 0
    for k in rowsizes:
        out.append(list(seq[cur:cur + k]))
        cur += k
    return out


def build_justified(assets, meta, faces_by_aid, rowsizes, has_title):
    """Build one page's fill geometry for a given row grouping.

    Justified rows fill WIDTH at natural aspect (zero crop); a vertical scale
    factor f stretches/(shrinks) the stack toward filling HEIGHT via per-cell
    cover-crops; per-cell resolve then protects faces + sharpness.

    PARTIAL-FILL LADDER: trying ONLY the exact-fill f is a trap — when that
    demands a crop beyond the face/crop caps, every cell falls back to contain,
    the worst of both worlds. A ladder of f values between natural (1.0) and
    exact fill is tried and the best face-safe coverage wins; the residual stays
    centered (even matting). Returns (cells, crops, fits, coverage_fraction).
    """
    # Full-bleed: image cells span the WHOLE page, edge to edge; only the chapter
    # title band is reserved from the top. The exporter extends every edge-touching
    # cell past the trim by the bleed amount, so there is no baked-in white frame.
    # Gutters (G) remain only BETWEEN cells, never at the page edge.
    y_start = (TITLE_H if has_title else 0.0)
    usable_w = PAGE_IN
    usable_h = PAGE_IN - y_start

    aspects = [(meta[a].get("w") or 1) / max(1, meta[a].get("h") or 1) for a in assets]
    rows = _rows_of(list(range(len(assets))), rowsizes)

    row_natural_h: list[float] = []
    row_widths: list[list[float]] = []
    for r in rows:
        k = len(r)
        s = sum(aspects[i] for i in r)
        row_h = (usable_w - G * (k - 1)) / max(EPS, s)
        row_natural_h.append(row_h)
        row_widths.append([aspects[i] * row_h for i in r])

    photo_h = sum(row_natural_h)
    target_photo_h = usable_h - G * (len(rows) - 1)
    f_fill = target_photo_h / max(EPS, photo_h)
    f_hi = 1.0 / (1.0 - FILL_CROP_FRAC)
    # NEVER exceed the exact fill (f <= f_fill) — otherwise a tall portrait stack
    # overflows off the bottom of the page. A stack too tall to fit shrinks to
    # exactly f_fill (no choice); an undersized stack tries the ladder from exact
    # fill down to natural height and keeps the best face-safe coverage.
    f_max = min(f_fill, f_hi)
    if f_max <= 1.0 + EPS:
        f_candidates = [f_max]
    else:
        # Ascending: with strict-improvement replacement below, the LOWEST f
        # (least crop) wins coverage ties.
        f_candidates = sorted({round(1.0 + t * (f_max - 1.0), 4)
                               for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)})

    best = None
    for f in f_candidates:
        row_h = [h * f for h in row_natural_h]
        stack_h = sum(row_h) + G * (len(rows) - 1)
        y = y_start + max(0.0, (usable_h - stack_h) / 2)   # center residual -> even margin

        cells: list[list[float]] = []
        crops: list[dict | None] = []
        fits: list[str] = []
        for ri, r in enumerate(rows):
            h = row_h[ri]
            widths = row_widths[ri]
            x = 0.0
            for j, i in enumerate(r):
                w = widths[j]
                aid = assets[i]
                crop, fit, geom = resolve_cell(aid, meta[aid], [x, y, w, h],
                                               faces_by_aid.get(aid, []))
                cells.append(geom)
                crops.append(crop)
                fits.append(fit)
                x += w + G
            y += h + G

        coverage = sum(c[2] * c[3] for c in cells) / PAGE_AREA
        # Prefer coverage; tie-break toward the LOWER f (less crop, calmer).
        if best is None or coverage > best[0] + 1e-4:
            best = (coverage, cells, crops, fits)

    coverage, cells, crops, fits = best
    return cells, crops, fits, coverage


def fill_page(assets, meta, faces_by_aid, has_title):
    """Pick the best-covering row grouping for a page."""
    best = None
    for rowsizes in _candidate_rowings(len(assets)):
        result = build_justified(assets, meta, faces_by_aid, rowsizes, has_title)
        coverage = result[3]
        # Prefer coverage; tie-break toward fewer rows (calmer) then less crop.
        crop_area = sum((c["w"] * c["h"]) for c in result[2 - 1] if c)  # crops removed frac proxy
        key = (round(coverage, 5), -len(rowsizes), crop_area)
        if best is None or key > best[0]:
            best = (key, rowsizes, result)
    return best[1], best[2]


# --- metrics ------------------------------------------------------------------
def page_has_title(book, page) -> bool:
    tpl = book["templates"].get(page["template"], {})
    return bool(tpl.get("title") and page.get("title"))


def page_coverage(book, page) -> float:
    tpl = book["templates"].get(page["template"], {})
    geom = tpl.get("cells", [])
    entries = page.get("cells", [])
    total = 0.0
    for i, cell in enumerate(geom):
        if i < len(entries) and cell_asset(entries[i]):
            total += cell[2] * cell[3]
    return total / PAGE_AREA


def cell_dpis(book, page, meta) -> list[float]:
    tpl = book["templates"].get(page["template"], {})
    geom = tpl.get("cells", [])
    entries = page.get("cells", [])
    out = []
    for i, cell in enumerate(geom):
        if i >= len(entries):
            continue
        aid = cell_asset(entries[i])
        if not aid or aid not in meta:
            continue
        out.append(effective_dpi(meta[aid], cell, cell_crop(entries[i])))
    return out


def iter_content_pages(book):
    for si, spread in enumerate(book["spreads"]):
        for side in ("left", "right"):
            page = spread[side]
            assets = [cell_asset(c) for c in page.get("cells", []) if cell_asset(c)]
            if assets:
                yield si, side, page, assets


# --- main ---------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    use_file = "--file" in sys.argv   # operate on book.json directly (batch pipeline)
    from photobook.bookrender import BOOK_JSON
    if use_file:
        book = json.loads(BOOK_JSON.read_text(encoding="utf-8"))
        revision = book.get("meta", {}).get("book_revision", 0)
        print(f"loaded book.json (rev {revision}): {len(book['spreads'])} spreads")
    else:
        payload = api_get("/api/book")
        book = payload["book"]
        revision = payload["book_revision"]
        print(f"loaded book rev {revision}: {len(book['spreads'])} spreads")

    # photo metadata for every placed asset
    placed = sorted({a for _, _, _, assets in iter_content_pages(book) for a in assets})
    with db.session() as conn:
        qmarks = ",".join("?" for _ in placed)
        meta = {
            r["asset_id"]: dict(r)
            for r in conn.execute(
                f"SELECT asset_id,w,h,derivative_path,people_count,orig_path,is_hero "
                f"FROM assets WHERE asset_id IN ({qmarks})", placed)
        }
    missing = [a for a in placed if a not in meta]
    if missing:
        print(f"  WARN: {len(missing)} placed assets missing from manifest (left untouched)")

    # face boxes for every placed asset (cached)
    faces_by_aid: dict[str, list] = {}
    for aid in placed:
        m = meta.get(aid)
        if m and m.get("derivative_path"):
            faces_by_aid[aid] = faces.detect_faces(m["derivative_path"], aid)
        else:
            faces_by_aid[aid] = []
    faces.flush_cache()
    n_faces_total = sum(len(v) for v in faces_by_aid.values())
    n_with_faces = sum(1 for v in faces_by_aid.values() if v)
    print(f"  faces: {n_faces_total} boxes across {n_with_faces}/{len(placed)} photos")

    # template-sharing guard (clone before rewrite so a shared template can't leak)
    tid_use: dict[str, int] = {}
    for _, _, page, _ in iter_content_pages(book):
        tid_use[page["template"]] = tid_use.get(page["template"], 0) + 1

    # --- BEFORE metrics ---
    before_cov = [page_coverage(book, page) for _, _, page, _ in iter_content_pages(book)]
    before_dpi = [d for _, _, page, _ in iter_content_pages(book) for d in cell_dpis(book, page, meta)]

    # --- rewrite each content page ---
    changed_pages = []
    for si, side, page, assets in iter_content_pages(book):
        usable = [a for a in assets if a in meta]
        if len(usable) != len(assets):
            # a placed asset is missing from the manifest — skip this page rather
            # than silently drop the photo (a keep-forever book must never lose one)
            continue
        has_title = page_has_title(book, page)
        rowsizes, (cells, crops, fits, _cov) = fill_page(usable, meta, faces_by_aid, has_title)

        tid = page["template"]
        if tid_use.get(tid, 0) > 1:                       # clone a shared template
            new_tid = f"{tid}__smartfill_{si}_{side}"
            book["templates"][new_tid] = dict(book["templates"][tid])
            page["template"] = tid = new_tid
            tid_use[tid] = 1
        tpl = book["templates"].setdefault(tid, {})
        tpl["cells"] = cells
        tpl["layout_style"] = "smart_fill"
        tpl["smart_fill"] = True
        tpl["bleed"] = True                     # full-bleed: exporter extends edge
        tpl["smart_fill_rows"] = rowsizes       # cells past the trim by bleed_in
        page["cells"] = [make_cell(aid, crop=crop) for aid, crop in zip(usable, crops)]
        page["fits"] = fits
        page["manual_crop"] = any(c is not None for c in crops)
        changed_pages.append((si, side))

    # --- AFTER metrics ---
    after_cov = [page_coverage(book, page) for _, _, page, _ in iter_content_pages(book)]
    after_dpi = [d for _, _, page, _ in iter_content_pages(book) for d in cell_dpis(book, page, meta)]

    # --- faces-clipped verification (hard gate) ---
    clipped = 0
    clip_detail = []
    for si, side, page, assets in iter_content_pages(book):
        entries = page.get("cells", [])
        for i, aid in enumerate([cell_asset(c) for c in entries]):
            if not aid:
                continue
            crop = cell_crop(entries[i])
            if crop and clips_any_face(crop, faces_by_aid.get(aid, [])):
                clipped += 1
                clip_detail.append((si + 1, side, aid[:8]))

    # --- spreads that changed the most (by coverage gain) ---
    per_spread_gain: dict[int, float] = {}
    order = [(si, side) for si, side, _, _ in iter_content_pages(book)]
    for (si, side), b, a in zip(order, before_cov, after_cov):
        per_spread_gain[si] = per_spread_gain.get(si, 0.0) + (a - b)
    top_spreads = sorted(per_spread_gain.items(), key=lambda kv: kv[1], reverse=True)[:6]

    def dist(xs):
        xs = [x for x in xs if x > 0]
        if not xs:
            return "n/a"
        xs = sorted(xs)
        return (f"min {min(xs):.0f} / p10 {xs[len(xs)//10]:.0f} / "
                f"median {statistics.median(xs):.0f} / max {max(xs):.0f}")

    print("\n=== COVERAGE (image area / page area) ===")
    print(f"  BEFORE: median {statistics.median(before_cov):.3f}  min {min(before_cov):.3f}")
    print(f"  AFTER : median {statistics.median(after_cov):.3f}  min {min(after_cov):.3f}")
    print(f"  pages < 0.90 coverage: before {sum(c<0.90 for c in before_cov)}  "
          f"after {sum(c<0.90 for c in after_cov)}  (of {len(after_cov)})")

    print("\n=== DPI (effective print DPI per cell) ===")
    print(f"  BEFORE: {dist(before_dpi)}")
    print(f"  AFTER : {dist(after_dpi)}")
    print(f"  cells < {SOFT_DPI:.0f} DPI: before {sum(d<SOFT_DPI for d in before_dpi)}  "
          f"after {sum(d<SOFT_DPI for d in after_dpi)}")
    print(f"  cells < {bm.DPI_OK} DPI (hard floor): before "
          f"{sum(d<bm.DPI_OK for d in before_dpi)}  after {sum(d<bm.DPI_OK for d in after_dpi)}")

    print("\n=== FACES CLIPPED (must be 0) ===")
    print(f"  clipped face boxes: {clipped}")
    if clip_detail:
        for sp, side, aid in clip_detail[:20]:
            print(f"    sp{sp:02d} {side} {aid}")

    print("\n=== SPREADS THAT CHANGED MOST (0-based index, coverage gain) ===")
    for si, gain in top_spreads:
        print(f"  spread {si} (sp{si+1:02d}, chapter {book['spreads'][si].get('chapter')}): +{gain:.3f}")

    if clipped > 0:
        print("\nREFUSING TO SAVE: face boxes would be clipped. No changes written.")
        return 1

    if dry_run:
        print("\n--dry-run: not saving.")
        return 0

    if use_file:
        book.setdefault("meta", {})["book_revision"] = int(revision or 0) + 1
        BOOK_JSON.write_text(json.dumps(book, indent=2), encoding="utf-8")
        print(f"\nsaved book.json ({len(changed_pages)} pages rewritten)")
        return 0
    st, res = api_post("/api/book", {"base_revision": revision, "book": book})
    print(f"\nsaved -> HTTP {st}, book_revision {res.get('book_revision')} "
          f"({len(changed_pages)} pages rewritten)")
    return 0 if st == 200 else 2


if __name__ == "__main__":
    raise SystemExit(main())
