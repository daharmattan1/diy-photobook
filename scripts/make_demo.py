"""Build a tiny, fully-synthetic DEMO book so the whole toolkit runs out of the
box — no personal photos, no cloud, nothing to download.

It:
  1. generates a handful of synthetic 3600x3600 images (well above the print-DPI
     floor) under demo/_work/originals/   (gitignored),
  2. builds a demo manifest.db at the repo root with one row per image
     (asset_id = sha1 of the file's bytes, matching the pipeline's contract),
  3. writes a book.json at the repo root with two spreads that place those images.

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
import sqlite3
import sys
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from photobook import db  # noqa: E402  (after sys.path insert)

ORIGINALS = ROOT / "demo" / "_work" / "originals"
MANIFEST = ROOT / "manifest.db"
BOOK_JSON = ROOT / "book.json"
PX = 3600            # square; a full 12in page cell renders at ~306 DPI (>= 240 floor)
N_IMAGES = 6


def _make_image(seed: int) -> Image.Image:
    """A non-uniform, colorful synthetic image (distinct per seed)."""
    rnd = random.Random(seed)
    base = (rnd.randint(30, 90), rnd.randint(30, 90), rnd.randint(40, 110))
    img = Image.new("RGB", (PX, PX), base)
    d = ImageDraw.Draw(img)
    for _ in range(40):
        x0, y0 = rnd.randint(0, PX - 1), rnd.randint(0, PX - 1)
        x1, y1 = rnd.randint(0, PX - 1), rnd.randint(0, PX - 1)
        color = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                    outline=color, width=rnd.randint(8, 40))
    # a big soft circle so the frame reads as a "subject"
    cx, cy, r = PX // 2, PX // 2, PX // 3
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              fill=(rnd.randint(120, 230), rnd.randint(120, 230), rnd.randint(120, 230)))
    return img


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

    # 1) generate images + collect content-addressed ids
    ids: list[str] = []
    for i in range(N_IMAGES):
        img = _make_image(1000 + i)
        p = ORIGINALS / f"demo_{i:02d}.jpg"
        img.save(p, "JPEG", quality=90)
        ids.append(_sha1(p))
    print(f"generated {N_IMAGES} synthetic images in {ORIGINALS}")

    # 2) build the demo manifest.db (fresh)
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
            conn.execute(
                "INSERT OR REPLACE INTO assets "
                "(asset_id, src, orig_path, staged_original_path, derivative_path, "
                " w, h, bytes, best_datetime, date_source, date_confidence, chapter, "
                " review_status, is_hero, favorited, people_count, tags) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                # derivative_path = the generated JPEG itself: the editor serves
                # thumbnails from the derivative, so it must be non-NULL.
                (aid, "demo", str(p), str(p), str(p), PX, PX, p.stat().st_size,
                 f"200{i}-06-15T12:00:00+00:00", "filename", "high",
                 "01_early_years", "accept", 1 if i == 0 else 0, 0, 1, ""),
            )
    print(f"wrote demo manifest -> {MANIFEST}  ({N_IMAGES} accepted assets)")

    # 3) write a two-spread book.json referencing those assets
    templates = {
        "chapter_full": {"title": True, "cells": [[0.12, 0.95, 11.76, 10.93]]},
        "full": {"cells": [[0.12, 0.12, 11.76, 11.76]]},
        "duo_v": {"cells": [[0.12, 0.12, 11.76, 5.82], [0.12, 6.06, 11.76, 5.82]]},
    }
    spreads = [
        {"id": uuid.uuid4().hex,
         "left": {"template": "chapter_full", "title": "The Early Years",
                  "cells": [_cell(ids[0])]},
         "right": {"template": "duo_v", "cells": [_cell(ids[1]), _cell(ids[2])]}},
        {"id": uuid.uuid4().hex,
         "left": {"template": "full", "cells": [_cell(ids[3])]},
         "right": {"template": "duo_v", "cells": [_cell(ids[4]), _cell(ids[5])]}},
    ]
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
                "title": "A Demo Book",
                "dates": "2001 – 2012",
                "spine_in": 0.75,
            },
        },
        "templates": templates,
        "spreads": spreads,
    }
    BOOK_JSON.write_text(json.dumps(book, indent=2), encoding="utf-8")
    print(f"wrote demo book -> {BOOK_JSON}  (2 spreads / 4 pages)")
    print("\nNext:")
    print("  python -m photobook export_book     # -> export_book/book.pdf + cover.pdf")
    print("  python -m photobook editor          # -> http://127.0.0.1:8765/")


if __name__ == "__main__":
    main()
