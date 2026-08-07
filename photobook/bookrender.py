"""Render spreads from book.json (Phase 1 preview). Reads the book-definition +
templates, positions each cell by its (x,y,w,h) inches on a 12in page, and
renders each SPREAD (left page 0-12in, right page 12-24in) to PNG/PDF via
Playwright. Preview uses derivatives; the production exporter (Phase 3) will
pull full-res."""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import db, paths
from .bookmodel import cell_asset, cell_caption, cell_crop

ROOT = Path(__file__).resolve().parents[1]
BOOK_JSON = ROOT / "book.json"
EDITED = ROOT / "edited"
OUT = ROOT / "samples"
PXIN = 100  # preview px per inch (2400x1200 per spread)

# Optional per-asset render-source overrides: {asset_id: image_path}. Use this to
# swap a specific photo's render source (a watermark-free re-crop, a rotation fix,
# a black-frame recovery, ...) WITHOUT editing the book. Empty by default and
# populated from the JSON sidecar below (fixes/overrides.json) so you never edit
# this literal.
CROPS: dict[str, Path] = {}

# Batch-remediation overrides (rotation corrections, black-frame recovery) merged
# from a JSON sidecar {asset_id: repo-relative-path}, so fixes don't edit this
# literal. Same override path as the wedding crops: it replaces the render source,
# and the exporter's exif_transpose no-ops on the EXIF-stripped corrected image.
_OVERRIDES_JSON = ROOT / "fixes" / "overrides.json"
if _OVERRIDES_JSON.is_file():
    for _aid, _rel in json.loads(_OVERRIDES_JSON.read_text(encoding="utf-8")).items():
        _p = Path(_rel)
        CROPS[_aid] = _p if _p.is_absolute() else (ROOT / _rel)


def _thumbs():
    with db.session() as c:
        rows = c.execute(
            "SELECT asset_id, derivative_path FROM assets WHERE derivative_path IS NOT NULL"
        ).fetchall()
    m = {}
    for r in rows:
        p = CROPS.get(r["asset_id"]) or Path(r["derivative_path"])
        m[r["asset_id"]] = p.resolve().as_uri()
    return m


def _page_html(page, x_off, templates, thumbs):
    tpl = templates[page["template"]]
    cells = tpl["cells"]
    fits = page.get("fits", [])
    bleed = tpl.get("bleed", False)
    scatter = tpl.get("scatter", False)
    page_bg = tpl.get("page_bg", "#F6F6F3")
    cell_bg = tpl.get("cell_bg", page_bg)
    parts = [
        f'<div class="page-bg" style="left:{x_off * PXIN}px;'
        f'background:{html.escape(page_bg)}"></div>'
    ]
    if tpl.get("title") and page.get("title"):
        parts.append(
            f'<div class="title" style="left:{(x_off + 0.18) * PXIN}px;top:{0.14 * PXIN}px;'
            f'width:{11.64 * PXIN}px">'
            f'<span class="ch">Chapter</span><span class="nm">{html.escape(page["title"])}</span></div>'
        )
    for i, cell in enumerate(cells):
        entry = page["cells"][i] if i < len(page["cells"]) else None
        aid = cell_asset(entry)
        if not aid:
            continue
        crop = cell_crop(entry)
        fit = fits[i] if i < len(fits) else tpl.get("default_fit", "contain")
        if fit != "contain" and not page.get("manual_crop"):
            raise ValueError(f"Automatic crop forbidden in cell {i}: {fit}")
        x, y, w, h = cell[0], cell[1], cell[2], cell[3]
        rot = cell[4] if len(cell) > 4 else 0
        src = thumbs.get(aid, "")
        caption = cell_caption(entry)
        caption_html = (
            f'<div class="caption">{html.escape(caption.strip())}</div>' if caption else ""
        )
        if crop:
            # Preview approximation of the cover-crop window; the exporter is the
            # pixel-exact source of truth. Uncropped path is left untouched.
            pos_x = (float(crop["x"]) + float(crop["w"]) / 2) * 100
            pos_y = (float(crop["y"]) + float(crop["h"]) / 2) * 100
            img_style = f"object-fit:cover;object-position:{pos_x:.2f}% {pos_y:.2f}%"
        else:
            img_style = f"object-fit:{fit}"
        if scatter:
            # scattered polaroid: a rotated wrapper holding a white-framed photo.
            wrap = (f"left:{(x_off + x) * PXIN}px;top:{y * PXIN}px;"
                    f"width:{w * PXIN}px;height:{h * PXIN}px;transform:rotate({rot}deg);")
            parts.append(
                f'<div class="polaroid" style="{wrap}">'
                f'<div class="pframe"><img src="{html.escape(src)}" style="{img_style}">{caption_html}</div>'
                f'</div>')
        else:
            style = (f"left:{(x_off + x) * PXIN}px;top:{y * PXIN}px;"
                     f"width:{w * PXIN}px;height:{h * PXIN}px;"
                     f"background:{html.escape(cell_bg)};")
            parts.append(f'<div class="cell" style="{style}"><img src="{html.escape(src)}" style="{img_style}">{caption_html}</div>')
    return "".join(parts)


def render(indices=None, tag="book"):
    book = json.loads(BOOK_JSON.read_text(encoding="utf-8"))
    templates = book["templates"]
    thumbs = _thumbs()
    spreads = book["spreads"]
    idx = indices if indices is not None else range(len(spreads))
    OUT.mkdir(exist_ok=True)

    blocks = []
    for i in idx:
        s = spreads[i]
        inner = _page_html(s["left"], 0, templates, thumbs) + _page_html(s["right"], 12, templates, thumbs)
        blocks.append(f'<div class="spread" data-i="{i}"><div class="gutter"></div>{inner}</div>')

    doc = f"""<!doctype html><meta charset="utf-8"><style>
* {{ box-sizing:border-box; margin:0; }}
body {{ background:#888; }}
.spread {{ position:relative; width:{24*PXIN}px; height:{12*PXIN}px; background:{book['meta']['bg']};
  page-break-after:always; overflow:hidden; }}
.page-bg {{ position:absolute; top:0; width:{12*PXIN}px; height:{12*PXIN}px; z-index:0; }}
.cell {{ position:absolute; overflow:hidden; z-index:1; }}
.cell img {{ width:100%; height:100%; object-fit:contain; object-position:center center; display:block; }}
.polaroid {{ position:absolute; z-index:1; }}
.pframe {{ position:absolute; inset:0; border:14px solid #fff; border-bottom-width:42px;
  box-shadow:0 12px 28px rgba(0,0,0,.34); overflow:hidden; background:#fff; }}
.pframe img {{ width:100%; height:100%; object-fit:cover; object-position:center center; display:block; }}
.cell .caption {{ position:absolute; left:0; right:0; bottom:0; z-index:2; padding:5px 10px;
  font:400 11pt Georgia,serif; line-height:1.25; color:#2b2a28; background:rgba(246,246,243,.82);
  text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.gutter {{ position:absolute; left:{12*PXIN-1}px; top:0; width:2px; height:100%;
  background:rgba(0,0,0,.05); z-index:5; }}
.title {{ position:absolute; z-index:2; }}
.title .ch {{ display:block; font:400 8pt Georgia,serif; letter-spacing:.24em;
  text-transform:uppercase; color:#9a948c; }}
.title .nm {{ display:block; font:400 22pt Georgia,serif; color:#2b2a28; margin-top:2px; }}
</style>{''.join(blocks)}"""

    hp = OUT / f"_{tag}.html"
    hp.write_text(doc, encoding="utf-8")

    from playwright.sync_api import sync_playwright
    shots = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 24 * PXIN, "height": 12 * PXIN}, device_scale_factor=1)
        pg.goto(hp.as_uri())
        pg.evaluate("async()=>{await document.fonts.ready;}")
        for i in idx:
            # Keep the page exactly one viewport tall while capturing. Element
            # screenshots on the former 28-spread document occasionally
            # stitched the browser's gray body between scroll positions.
            pg.evaluate(
                """index => {
                    document.querySelectorAll('.spread').forEach(el => {
                        el.style.display = el.dataset.i === String(index)
                            ? 'block'
                            : 'none';
                    });
                    window.scrollTo(0, 0);
                }""",
                i,
            )
            fn = OUT / f"{tag}_spread{i}.png"
            pg.screenshot(
                path=str(fn),
                clip={"x": 0, "y": 0, "width": 24 * PXIN, "height": 12 * PXIN},
            )
            shots.append(str(fn))
        b.close()
    return shots


if __name__ == "__main__":
    import sys
    ix = [int(x) for x in sys.argv[1:]] or [0, 1, 7, 8]
    print("\n".join(render(ix)))
