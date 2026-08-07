"""Generate synthetic photo fixtures for smoke-testing the pipeline.

Creates a directory of small images across the formats the pipeline handles
(JPEG, PNG, HEIC), a Takeout-style `name.jpg` + `name.jpg.json` sidecar pair,
a `(N)`-suffixed sidecar variant, a couple of near-duplicates, a synthetic
"screenshot", and a low-light frame — enough to exercise ingest, date-repair,
dedup, quality, chaptering, scoring, and export without any real photos.

Usage:
    python scripts/make_fixtures.py <out_dir> [--n 40]

Deterministic (fixed seed) so runs are reproducible.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIC_OK = True
except Exception:  # pragma: no cover
    _HEIC_OK = False


def _noise_image(w: int, h: int, seed: int, base=(120, 120, 120)) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(img)
    # A handful of colored rectangles → non-trivial pHash, distinct per seed.
    for _ in range(12):
        x0, y0 = rnd.randint(0, w - 1), rnd.randint(0, h - 1)
        x1, y1 = rnd.randint(0, w - 1), rnd.randint(0, h - 1)
        color = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        d.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], fill=color)
    return img


def _write_sidecar(img_path: Path, taken: datetime, sidecar_name: str | None = None) -> Path:
    payload = {
        "title": img_path.name,
        "photoTakenTime": {
            "timestamp": str(int(taken.timestamp())),
            "formatted": taken.strftime("%b %d, %Y, %I:%M:%S %p UTC"),
        },
    }
    sc = img_path.parent / (sidecar_name or f"{img_path.name}.json")
    sc.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--n", type=int, default=40, help="approx number of base images")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    made = []

    # 1) A spread of plain JPEGs across dates 2017..2026 (filename-dated).
    for i in range(args.n):
        year = 2017 + (i % 10)
        month = (i % 12) + 1
        day = (i % 27) + 1
        seed = 1000 + i
        img = _noise_image(1600, 1200, seed)
        name = f"IMG_{year}{month:02d}{day:02d}_{i:03d}.jpg"
        p = out / name
        img.save(p, "JPEG", quality=88)
        made.append(p)

    # 2) A Takeout pair: photo + photo.jpg.json (sidecar-dated).
    tk = out / "takeout_photo.jpg"
    _noise_image(2000, 1500, seed=55).save(tk, "JPEG", quality=90)
    _write_sidecar(tk, datetime(2019, 6, 1, 14, 30, tzinfo=timezone.utc))
    made.append(tk)

    # 3) A "(N)"-suffixed Takeout pair using the image.jpg(N).json quirk.
    tkn = out / "trip(1).jpg"
    _noise_image(2000, 1500, seed=56).save(tkn, "JPEG", quality=90)
    _write_sidecar(tkn, datetime(2020, 9, 15, 9, 0, tzinfo=timezone.utc),
                   sidecar_name="trip.jpg(1).json")
    made.append(tkn)

    # 4) Near-duplicates of one base image (dedup should collapse these).
    base = _noise_image(1800, 1350, seed=77)
    for k in range(3):
        dup = base.copy()
        # tiny perturbation so bytes differ but pHash stays within threshold
        ImageDraw.Draw(dup).point([(k, k)], fill=(0, 0, 0))
        dp = out / f"PXL_20220101_00000{k}.jpg"
        dup.save(dp, "JPEG", quality=92)
        made.append(dp)

    # 5) A HEIC (iPhone default) if pillow-heif is available.
    if _HEIC_OK:
        hp = out / "IMG_20230704_iphone.heic"
        _noise_image(1500, 1500, seed=88).save(hp, format="HEIF", quality=80)
        made.append(hp)

    # 6) A synthetic "screenshot": tall device aspect, PNG, Screenshot_ name, flat UI.
    ss = Image.new("RGB", (1080, 2340), (245, 245, 247))
    d = ImageDraw.Draw(ss)
    d.rectangle([0, 0, 1080, 120], fill=(255, 255, 255))
    d.rectangle([40, 200, 1040, 400], fill=(230, 230, 235))
    sp = out / "Screenshot_20240115_101010.png"
    ss.save(sp, "PNG")
    made.append(sp)

    # 7) A dark/low-light frame (quality gate should flag as dark).
    dark = Image.new("RGB", (1600, 1200), (8, 8, 10))
    dp2 = out / "IMG_20250201_dark.jpg"
    dark.save(dp2, "JPEG", quality=85)
    made.append(dp2)

    # 8) A non-image file (should be skipped by ingest).
    (out / "notes.txt").write_text("not a photo", encoding="utf-8")

    print(f"wrote {len(made)} image fixtures + sidecars + 1 non-image to {out}")
    print(f"  HEIC included: {_HEIC_OK}")


if __name__ == "__main__":
    main()
