"""Real-ESRGAN batch upscale for the placed sub-240-DPI photos.

Reads samples/upscale_worklist.json ({aid: {factor, w, h, src}}), upscales each
photo 4x with realesrgan-ncnn-vulkan (x4plus model), downsizes to the factor it
actually needs (+15% margin, capped 4x) with Lanczos, writes
staging/upscaled/<aid>.jpg, and updates the manifest: staged_original_path ->
the upscaled file, w/h -> new dims, tags += 'upscaled'.

The exporter's sha1 hash-sample skips 'upscaled'-tagged assets (an upscaled file
can't match the original-bytes asset_id by construction — that's expected, not
corruption).

Usage: python scripts/upscale_batch.py [--limit N] [--start K]
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
try:
    import pillow_heif; pillow_heif.register_heif_opener()
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan.exe"
OUT_DIR = ROOT / "staging" / "upscaled"
WORKLIST = ROOT / "samples" / "upscale_worklist.json"
MARGIN = 1.15


def main() -> int:
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    start = int(sys.argv[sys.argv.index("--start") + 1]) if "--start" in sys.argv else 0
    try:
        work = json.loads(WORKLIST.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"[upscale_batch] missing worklist: {WORKLIST}\n"
            '  Build it first — a JSON map {asset_id: {"src": path}} of the '
            "photos to upscale (see docs/UPSCALING.md)."
        ) from None
    items = list(work.items())[start:]
    if limit:
        items = items[:limit]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ROOT / "manifest.db")
    ok = fail = skip = 0
    for n, (aid, spec) in enumerate(items, 1):
        out = OUT_DIR / f"{aid}.jpg"
        if out.exists():                       # resumable
            skip += 1
            continue
        src = Path(spec["src"])
        if not src.is_absolute():
            src = ROOT / src
        if not src.is_file():
            print(f"[{n}] {aid[:8]} MISSING src"); fail += 1; continue
        try:
            with tempfile.TemporaryDirectory() as td:
                inp = Path(td) / "in.png"
                up = Path(td) / "up.png"
                with Image.open(src) as im:      # normalize (HEIC etc.) to PNG
                    im.convert("RGB").save(inp, "PNG")
                    ow, oh = im.size
                r = subprocess.run(
                    [str(EXE), "-i", str(inp), "-o", str(up), "-n", "realesrgan-x4plus"],
                    capture_output=True, timeout=600,
                )
                if r.returncode != 0 or not up.is_file():
                    print(f"[{n}] {aid[:8]} ESRGAN rc={r.returncode} {r.stderr[-120:]}")
                    fail += 1
                    continue
                factor = min(4.0, float(spec["factor"]) * MARGIN)
                tw, th = round(ow * factor), round(oh * factor)
                with Image.open(up) as u:
                    u = u.convert("RGB")
                    if (tw, th) != u.size:
                        u = u.resize((tw, th), Image.Resampling.LANCZOS)
                    u.save(out, "JPEG", quality=95)
            con.execute(
                "UPDATE assets SET staged_original_path=?, w=?, h=?, "
                "tags=COALESCE(tags,'') || ',upscaled' WHERE asset_id=?",
                (str(out), tw, th, aid),
            )
            con.commit()
            ok += 1
            if n % 10 == 0 or n == len(items):
                print(f"[{n}/{len(items)}] ok={ok} fail={fail} skip={skip}")
        except Exception as e:
            print(f"[{n}] {aid[:8]} EXC {str(e)[:100]}")
            fail += 1
    print(f"DONE ok={ok} fail={fail} skip={skip}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
