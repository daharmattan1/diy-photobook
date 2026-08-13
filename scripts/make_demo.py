"""Build a small, fully-synthetic DEMO book so the whole toolkit runs out of the
box — no personal photos, no cloud, nothing to download.

It:
  1. generates a set of synthetic "photos" under demo/_work/originals/ (gitignored).
     Six scene archetypes (ridgelines, beach, night bokeh, figures, city skyline,
     misty forest) so the demo reads like a real, varied family album rather than a
     test pattern. Each photo is rendered at the ASPECT RATIO of the cell it lands
     in (the editor shows the whole frame with object-fit:contain), so placed pages
     fill cleanly instead of letterboxing — the way a finished book looks,
  2. builds a demo manifest.db at the repo root with one row per image
     (asset_id = sha1 of the file's bytes, matching the pipeline's contract), and
     with era / event / theme-tag / favorited metadata so the editor's tray
     filters (the two-axis "time vs. theme" idea) actually have something to sort,
  3. writes a book.json at the repo root: 8 spreads across 4 chapters, using a
     spread of layout templates (chapter openers, full bleeds, duos, trios, a 2x2
     quad, a hero+strip, a 3x2 grid) so the layout looks like a finished book.

Then you can:
    python -m photobook export_book        # -> export_book/book.pdf + cover.pdf + pages
    python -m photobook editor             # -> http://127.0.0.1:8765/

Everything it writes is regenerable and gitignored. Deterministic (fixed seed).

Usage:  python scripts/make_demo.py
"""
from __future__ import annotations

import hashlib
import json
import random
import sys
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from photobook import db  # noqa: E402  (after sys.path insert)

ORIGINALS = ROOT / "demo" / "_work" / "originals"
MANIFEST = ROOT / "manifest.db"
BOOK_JSON = ROOT / "book.json"
N_IMAGES = 40
DPI = 300                      # render each cell's photo at ~300 DPI of its printed size

# Four synthetic "chapters" (eras) — the time axis. Neutral, obviously-a-demo.
ERAS = ["The Early Years", "Adventures", "The Big Move", "Family Grows"]

# Theme tags — the axis that cuts ACROSS time. Derived from the scene so the
# imagery and the tag agree (travel photos read as travel, etc.).
_SCENES = ["ridges", "figures", "beach", "bokeh", "forest", "skyline"]
# Curate the four chapter-opener heroes (full-page) to the strongest scenes.
_OPENER_SCENE = {0: "ridges", 10: "beach", 20: "skyline", 30: "figures"}
_SCENE_TAGS = {
    "ridges": ["travel", "outdoors"],
    "beach": ["travel", "outdoors"],
    "forest": ["outdoors"],
    "skyline": ["travel", "city"],
    "bokeh": ["celebration"],
    "figures": ["family", "kids"],
}


def _era(i: int) -> str:
    return ERAS[min(i // 10, len(ERAS) - 1)]


def _scene(i: int) -> str:
    # step 5 (coprime to 6) cycles all six with no adjacent repeats within a page;
    # chapter openers are overridden to a curated strong scene.
    return _OPENER_SCENE.get(i, _SCENES[(i * 5) % len(_SCENES)])


def _event(i: int) -> str | None:
    if 3 <= i <= 7:
        return "Gap Year"
    if 12 <= i <= 15:
        return "A Wedding"
    if 22 <= i <= 25:
        return "First House"
    if 32 <= i <= 35:
        return "A Birthday"
    return None


def _datetime(i: int) -> str:
    year = 2016 + i // 5           # 2016 .. 2023 across the 40 images
    month = (i * 7) % 12 + 1
    return f"{year:04d}-{month:02d}-15T12:00:00+00:00"


# --------------------------------------------------------------- templates ----
# Cells are [x, y, w, h] in inches on a 12in page (0.12in margin, 0.24in gutter).
# The editor and exporter both position cells straight from these rectangles.
TEMPLATES = {
    "chapter_full": {"title": True, "cells": [[0.12, 0.95, 11.76, 10.93]]},
    "full": {"cells": [[0.12, 0.12, 11.76, 11.76]]},
    "duo_v": {"cells": [[0.12, 0.12, 11.76, 5.76], [0.12, 6.12, 11.76, 5.76]]},
    "duo_h": {"cells": [[0.12, 0.12, 5.76, 11.76], [6.12, 0.12, 5.76, 11.76]]},
    "trio_l": {"cells": [[0.12, 0.12, 7.40, 11.76],
                         [7.76, 0.12, 4.12, 5.76], [7.76, 6.12, 4.12, 5.76]]},
    "trio_top": {"cells": [[0.12, 0.12, 11.76, 7.40],
                           [0.12, 7.76, 5.76, 4.12], [6.12, 7.76, 5.76, 4.12]]},
    "quad": {"cells": [[0.12, 0.12, 5.76, 5.76], [6.12, 0.12, 5.76, 5.76],
                       [0.12, 6.12, 5.76, 5.76], [6.12, 6.12, 5.76, 5.76]]},
    "five_hero": {"cells": [[0.12, 0.12, 7.40, 11.76],
                            [7.76, 0.12, 1.94, 5.76], [9.94, 0.12, 1.94, 5.76],
                            [7.76, 6.12, 1.94, 5.76], [9.94, 6.12, 1.94, 5.76]]},
    "six_grid": {"cells": [[0.12, 0.12, 3.76, 5.76], [4.12, 0.12, 3.76, 5.76],
                           [8.12, 0.12, 3.76, 5.76], [0.12, 6.12, 3.76, 5.76],
                           [4.12, 6.12, 3.76, 5.76], [8.12, 6.12, 3.76, 5.76]]},
}

# The book: 8 spreads across 4 chapters. Each page is (template, [image indices]).
# Placing 36 of the 40 photos leaves a few in the tray so the "unused" filter
# has something to show.
LAYOUT = [
    (ERAS[0], ("chapter_full", [0]),                   ("duo_v", [1, 2])),
    (ERAS[0], ("trio_l", [3, 4, 5]),                   ("full", [6])),
    (ERAS[1], ("chapter_full", [10]),                  ("quad", [11, 12, 13, 14])),
    (ERAS[1], ("duo_h", [15, 16]),                     ("trio_top", [17, 18, 19])),
    (ERAS[2], ("chapter_full", [20]),                  ("five_hero", [21, 22, 23, 24, 25])),
    (ERAS[2], ("full", [26]),                          ("duo_v", [27, 28])),
    (ERAS[3], ("chapter_full", [30]),                  ("duo_h", [31, 32])),
    (ERAS[3], ("six_grid", [33, 34, 35, 36, 37, 38]),  ("full", [39])),
]


def _cell_sizes() -> dict[int, tuple[float, float]]:
    """image index -> (cell_w_in, cell_h_in) for every PLACED image."""
    sizes: dict[int, tuple[float, float]] = {}
    for _chapter, left, right in LAYOUT:
        for template, idxs in (left, right):
            cells = TEMPLATES[template]["cells"]
            for pos, img_idx in enumerate(idxs):
                sizes[img_idx] = (cells[pos][2], cells[pos][3])
    return sizes


# --------------------------------------------------------------- imagery ------
# Each scene builder takes a seeded RNG and a (W, H) canvas and returns a soft,
# photo-like RGB image. Shapes are cast to int coords (Pillow wants integers) and
# the result gets a light final blur so it reads as a photograph, not vector art.

def _ipts(pts):
    return [(int(x), int(y)) for x, y in pts]


def _to_img(arr: np.ndarray) -> Image.Image:
    # Pillow infers "RGB" from an (H, W, 3) uint8 array; passing mode= is deprecated.
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))


def _vgrad(top, bot, h: int, w: int) -> np.ndarray:
    """A vertical top->bottom colour gradient as an (h, w, 3) float32 array."""
    t = np.linspace(0.0, 1.0, h, dtype=np.float32).reshape(h, 1, 1)
    top = np.array(top, np.float32).reshape(1, 1, 3)
    bot = np.array(bot, np.float32).reshape(1, 1, 3)
    band = top * (1.0 - t) + bot * t                      # (h, 1, 3)
    return np.broadcast_to(band, (h, w, 3)).astype(np.float32).copy()


def _scene_ridges(rnd: random.Random, W: int, H: int) -> Image.Image:
    k, u = max(W, H), min(W, H)
    skies = [((60, 92, 150), (232, 176, 120)), ((40, 64, 120), (206, 150, 170)),
             ((70, 120, 150), (222, 224, 208)), ((36, 52, 96), (240, 198, 132))]
    top, glow = rnd.choice(skies)
    top = tuple(c + rnd.randint(-8, 8) for c in top)
    horizon = int(H * rnd.uniform(0.60, 0.70))
    arr = np.empty((H, W, 3), np.float32)
    arr[:horizon] = _vgrad(top, glow, horizon, W)
    arr[horizon:] = np.array(glow, np.float32) * 0.5
    im = _to_img(arr)
    d = ImageDraw.Draw(im, "RGBA")
    if rnd.random() < 0.7:
        sr = int(u * rnd.uniform(0.06, 0.11))
        sx = int(W * rnd.uniform(0.20, 0.80)); sy = int(horizon * rnd.uniform(0.40, 0.75))
        d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 240, 210, 235))
    layers = rnd.randint(3, 4)
    base = np.array(top, np.float32)
    for layer in range(layers):
        frac = (layer + 1) / (layers + 1)
        base_y = int(horizon - (layers - 1 - layer) * H * 0.08 + H * 0.02)
        amp = H * (0.02 + 0.05 * frac)
        freq = rnd.uniform(1.2, 2.6); phase = rnd.uniform(0, 6.28)
        xs = np.linspace(0, W, 40)
        ys = (base_y + amp * np.sin(freq * xs / W * 6.28 + phase)
              + amp * 0.4 * np.sin(freq * 2.1 * xs / W * 6.28 + phase * 1.7))
        pts = _ipts(list(zip(xs, ys))) + [(W, H), (0, H)]
        shade = 0.55 - 0.32 * frac
        col = tuple(int(max(0, c * shade)) for c in base)
        d.polygon(pts, fill=col + (255,))
    return im.filter(ImageFilter.GaussianBlur(k / 1100))


def _scene_beach(rnd: random.Random, W: int, H: int) -> Image.Image:
    k, u = max(W, H), min(W, H)
    skies = [((120, 170, 210), (238, 224, 196)), ((150, 150, 200), (244, 208, 190)),
             ((96, 150, 190), (224, 230, 220))]
    top, glow = rnd.choice(skies)
    horizon = int(H * rnd.uniform(0.48, 0.56))
    sand_y = int(H * rnd.uniform(0.80, 0.88))
    arr = np.empty((H, W, 3), np.float32)
    arr[:horizon] = _vgrad(top, glow, horizon, W)
    sea_top = tuple(int(c * 0.7) for c in glow)
    arr[horizon:sand_y] = _vgrad(sea_top, (40, 78, 96), sand_y - horizon, W)
    arr[sand_y:] = _vgrad((214, 196, 158), (188, 168, 132), H - sand_y, W)
    im = _to_img(arr)
    d = ImageDraw.Draw(im, "RGBA")
    sr = int(u * rnd.uniform(0.06, 0.10))
    sx = int(W * rnd.uniform(0.30, 0.70)); sy = int(horizon * rnd.uniform(0.55, 0.80))
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 238, 206, 240))
    rh = max(2, int(H * 0.0016))
    for j in range(26):                                    # shimmering reflection
        yy = horizon + (sand_y - horizon) * j / 26
        ww = sr * (0.5 + 0.9 * j / 26)
        a = int(150 * (1 - j / 26))
        d.ellipse([int(sx - ww), int(yy - rh), int(sx + ww), int(yy + rh)],
                  fill=(255, 236, 200, a))
    return im.filter(ImageFilter.GaussianBlur(k / 1000))


def _scene_bokeh(rnd: random.Random, W: int, H: int) -> Image.Image:
    k, u = max(W, H), min(W, H)
    bases = [((28, 20, 40), (12, 8, 20)), ((40, 22, 26), (14, 10, 12)),
             ((20, 26, 44), (8, 10, 18))]
    top, bot = rnd.choice(bases)
    im = _to_img(_vgrad(top, bot, H, W))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    warm = [(255, 214, 150), (255, 180, 120), (255, 236, 190), (210, 200, 255), (255, 160, 150)]
    for _ in range(rnd.randint(34, 48)):
        r = int(u * rnd.uniform(0.03, 0.14))
        cx = int(rnd.uniform(0, W)); cy = int(rnd.uniform(0, H * 0.9))
        col = rnd.choice(warm); a = rnd.randint(40, 130)
        od.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (a,))
    overlay = overlay.filter(ImageFilter.GaussianBlur(k / 220))
    im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
    return im.filter(ImageFilter.GaussianBlur(k / 1600))


def _scene_figures(rnd: random.Random, W: int, H: int) -> Image.Image:
    k, u = max(W, H), min(W, H)
    skies = [((44, 60, 120), (244, 168, 110)), ((70, 60, 120), (246, 150, 140)),
             ((40, 72, 110), (236, 206, 150))]
    top, glow = rnd.choice(skies)
    horizon = int(H * rnd.uniform(0.68, 0.78))
    arr = np.empty((H, W, 3), np.float32)
    arr[:horizon] = _vgrad(top, glow, horizon, W)
    arr[horizon:] = np.array(tuple(int(c * 0.32) for c in glow), np.float32)
    im = _to_img(arr)
    d = ImageDraw.Draw(im, "RGBA")
    if rnd.random() < 0.7:
        sr = int(u * rnd.uniform(0.07, 0.12))
        sx = int(W * rnd.uniform(0.25, 0.75)); sy = int(horizon * rnd.uniform(0.55, 0.85))
        d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 226, 180, 235))
    n = rnd.randint(1, 3)
    for i in range(n):
        h = int(H * rnd.uniform(0.20, 0.30)); w = int(h * rnd.uniform(0.26, 0.34))
        cx = int(W * ((i + 1) / (n + 1)) + rnd.randint(-W // 20, W // 20))
        top_y = horizon - h
        d.polygon(_ipts([(cx - w * 0.5, horizon), (cx - w * 0.35, top_y + h * 0.28),
                         (cx + w * 0.35, top_y + h * 0.28), (cx + w * 0.5, horizon)]),
                  fill=(18, 16, 24, 255))
        hr = max(2, int(w * 0.34))
        d.ellipse([cx - hr, top_y, cx + hr, top_y + 2 * hr], fill=(18, 16, 24, 255))
    return im.filter(ImageFilter.GaussianBlur(k / 1100))


def _scene_skyline(rnd: random.Random, W: int, H: int) -> Image.Image:
    k, u = max(W, H), min(W, H)
    skies = [((30, 40, 78), (232, 150, 110)), ((44, 40, 84), (214, 150, 170)),
             ((26, 44, 72), (232, 196, 150))]
    top, glow = rnd.choice(skies)
    horizon = int(H * rnd.uniform(0.55, 0.66))
    arr = np.empty((H, W, 3), np.float32)
    arr[:horizon] = _vgrad(top, glow, horizon, W)
    arr[horizon:] = np.array(tuple(int(c * 0.4) for c in top), np.float32)
    im = _to_img(arr)
    d = ImageDraw.Draw(im, "RGBA")
    if rnd.random() < 0.6:
        sr = int(u * rnd.uniform(0.05, 0.09))
        sx = int(W * rnd.uniform(0.20, 0.80)); sy = int(horizon * rnd.uniform(0.35, 0.60))
        d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 232, 198, 220))
    x = 0
    ws = max(2, int(u * 0.008))
    while x < W:
        bw = int(W * rnd.uniform(0.04, 0.10)); bh = int(H * rnd.uniform(0.20, 0.55))
        by = horizon - bh
        d.rectangle([x, by, x + bw, int(horizon + H * 0.02)], fill=(16, 18, 30, 255))
        for _ in range(rnd.randint(3, 10)):                # lit windows
            wx = x + int(rnd.uniform(0.15, 0.85) * bw); wy = by + int(rnd.uniform(0.10, 0.90) * bh)
            d.rectangle([wx, wy, wx + ws, wy + ws], fill=(255, 224, 150, 235))
        x += bw + int(W * rnd.uniform(0.005, 0.02))
    return im.filter(ImageFilter.GaussianBlur(k / 1300))


def _scene_forest(rnd: random.Random, W: int, H: int) -> Image.Image:
    k = max(W, H)
    tops = [((196, 206, 196), (150, 166, 150)), ((186, 200, 210), (140, 158, 166)),
            ((206, 200, 188), (158, 158, 150))]
    top, bot = rnd.choice(tops)
    im = _to_img(_vgrad(top, bot, H, W))
    d = ImageDraw.Draw(im, "RGBA")
    n = rnd.randint(5, 8)
    for i in range(n):
        frac = i / n
        cx = int(W * rnd.uniform(0.05, 0.95)); tw = max(2, int(W * rnd.uniform(0.02, 0.06)))
        shade = int(40 + 120 * frac)
        d.polygon(_ipts([(cx - tw, H), (cx - tw * 0.5, H * 0.10),
                         (cx + tw * 0.5, H * 0.10), (cx + tw, H)]),
                  fill=(shade, shade + 8, shade, int(150 + 80 * frac)))
    return im.filter(ImageFilter.GaussianBlur(k / 700))


_SCENE_FN = {
    "ridges": _scene_ridges, "beach": _scene_beach, "bokeh": _scene_bokeh,
    "figures": _scene_figures, "skyline": _scene_skyline, "forest": _scene_forest,
}


def _make_image(seed: int, scene: str, w_in: float, h_in: float) -> Image.Image:
    W, H = max(64, round(w_in * DPI)), max(64, round(h_in * DPI))
    return _SCENE_FN[scene](random.Random(seed), W, H)


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cell(asset_id: str) -> dict:
    return {"id": uuid.uuid4().hex, "asset_id": asset_id,
            "crop": None, "dpi_override": None, "caption": None}


def main() -> None:
    ORIGINALS.mkdir(parents=True, exist_ok=True)
    sizes = _cell_sizes()

    # 1) generate each photo at its cell's aspect ratio (tray-only photos default
    #    to a 6x4 landscape frame), and collect content-addressed ids + dimensions.
    ids: list[str] = []
    dims: list[tuple[int, int]] = []
    for i in range(N_IMAGES):
        w_in, h_in = sizes.get(i, (6.0, 4.0))
        img = _make_image(1000 + i, _scene(i), w_in, h_in)
        p = ORIGINALS / f"demo_{i:02d}.jpg"
        img.save(p, "JPEG", quality=90)
        ids.append(_sha1(p)); dims.append(img.size)   # (w, h)
    print(f"generated {N_IMAGES} synthetic photos in {ORIGINALS}")

    # 2) build the demo manifest.db (fresh) with tray-filter metadata
    if MANIFEST.exists():
        MANIFEST.unlink()
    for suffix in ("-wal", "-shm"):
        wal = MANIFEST.with_name(MANIFEST.name + suffix)
        if wal.exists():
            wal.unlink()
    db.init_db(MANIFEST)
    with db.session(MANIFEST) as conn:
        for i, aid in enumerate(ids):
            p = ORIGINALS / f"demo_{i:02d}.jpg"
            w, h = dims[i]
            scene = _scene(i)
            tags = _SCENE_TAGS[scene]
            people = 3 if scene == "figures" else (2 if scene == "bokeh" else (1 if i % 2 else 0))
            conn.execute(
                "INSERT OR REPLACE INTO assets "
                "(asset_id, src, orig_path, staged_original_path, derivative_path, "
                " w, h, bytes, best_datetime, date_source, date_confidence, chapter, "
                " event, review_status, is_hero, favorited, people_count, tags) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                # derivative_path = the generated JPEG itself: the editor serves
                # thumbnails from the derivative, so it must be non-NULL.
                (aid, "demo", str(p), str(p), str(p), w, h, p.stat().st_size,
                 _datetime(i), "exif", "high", _era(i), _event(i),
                 "accept", 1 if i == 0 else 0, 1 if i % 3 == 0 else 0,
                 people, ",".join(tags)),
            )
    print(f"wrote demo manifest -> {MANIFEST}  ({N_IMAGES} accepted assets)")

    # 3) write the 8-spread book from the same LAYOUT.
    def page(template, idxs, chapter, opener):
        pg = {"template": template, "cells": [_cell(ids[k]) for k in idxs]}
        if opener:
            pg["title"] = chapter
        return pg

    spreads = []
    for chapter, (lt, li), (rt, ri) in LAYOUT:
        spreads.append({
            "id": uuid.uuid4().hex, "kind": "content", "chapter": chapter,
            "left": page(lt, li, chapter, lt == "chapter_full"),
            "right": page(rt, ri, chapter, rt == "chapter_full"),
        })

    book = {
        "meta": {
            "schema_version": "2.0",
            # INT per the book.json contract (bookmodel defaults it to 0; the
            # editor increments it on every save). A non-int here breaks the
            # editor's revision check.
            "book_revision": 1,
            "bg": "#F6F6F3",
            "bleed_in": 0.125,
            "page_in": 12.0,
            "print_ready": True,
            "geometry": {"page_in": 12.0, "margin_in": 0.12,
                         "gutter_in": 0.05, "title_in": 0.65},
            "cover": {
                "title": "A Family Album",
                "dates": "2016 – 2023",
                "spine_in": 0.75,
            },
        },
        "templates": TEMPLATES,
        "spreads": spreads,
    }
    BOOK_JSON.write_text(json.dumps(book, indent=2), encoding="utf-8")
    print(f"wrote demo book -> {BOOK_JSON}  ({len(spreads)} spreads / {len(spreads) * 2} pages)")
    print("\nNext:")
    print("  python -m photobook export_book     # -> export_book/book.pdf + cover.pdf")
    print("  python -m photobook editor          # -> http://127.0.0.1:8765/")


if __name__ == "__main__":
    main()
