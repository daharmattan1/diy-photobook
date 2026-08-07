"""Reliable low-DPI fix — NO GPU (Lanczos only), so it cannot corrupt like the
Real-ESRGAN/Vulkan path did under load. For every placement whose real render
source is below target DPI, upscale the ACTUAL render file in place using the
largest clean available source (a prior clean ESRGAN derivative if bigger, else
Lanczos from the best source). These images are resolution-limited already, so
Lanczos is equivalent quality and 100% reliable."""
import json, tempfile
from pathlib import Path
from PIL import Image, ImageStat
Image.MAX_IMAGE_PIXELS = None
try:
    import pillow_heif; pillow_heif.register_heif_opener()
except Exception:
    pass
import photobook.export_book as eb

ROOT = Path(__file__).resolve().parents[1]   # repo root (scripts/ is one level down)
TARGET = 300
ACT_BELOW = 258          # act on render sources below this real DPI
try:
    book = json.loads((ROOT / "book.json").read_text(encoding="utf-8"))
except FileNotFoundError:
    raise SystemExit(
        "[fix_lowdpi] no book.json at the repo root — lay out a book first "
        "(photobook editor), or run scripts/make_demo.py for the demo book."
    ) from None
ids = eb._placed_ids(book)
assets = eb._load_assets(ids, None)
wd = Path(tempfile.mkdtemp())

def clean_open(p):
    """Return (Image, long_side) if openable and not black/garbage, else None."""
    try:
        im = Image.open(p).convert("RGB")
        st = ImageStat.Stat(im)
        if st.mean[0] < 14:            # near-black => corrupt
            return None
        if max(st.stddev) < 3:         # uniform => garbage
            return None
        return im, max(im.size)
    except Exception:
        return None

# needed long-side px per render file (max across its placements)
need = {}
render_of = {}
for i, s in enumerate(book["spreads"]):
    for side in ("left", "right"):
        pg = s[side]; tpl = book["templates"].get(pg["template"], {})
        for c, geo in zip(pg.get("cells", []), tpl.get("cells", [])):
            aid = c.get("asset_id")
            if not aid:
                continue
            try:
                rs = eb._full_res(aid, assets[aid], wd)
            except Exception:
                continue
            rp = Path(rs.render_path)
            if wd in rp.parents:        # skip zip work-dir extracts
                continue
            co = clean_open(rp)
            if co is None:
                W = H = 1               # force fix
            else:
                W, H = co[0].size
            cr = c.get("crop") or {"x": 0, "y": 0, "w": 1, "h": 1}
            dpi = min((cr["w"] * W) / geo[2], (cr["h"] * H) / geo[3])
            if dpi < ACT_BELOW:
                need_long = TARGET * max(geo[2] / cr["w"], geo[3] / cr["h"])
                need[rp] = max(need.get(rp, 0), need_long)
                render_of[rp] = aid

print(f"{len(need)} render files below {ACT_BELOW} DPI to fix (Lanczos)", flush=True)
fixed = failed = 0
for rp, need_long in sorted(need.items(), key=lambda kv: -kv[1]):
    aid = render_of[rp]
    a = assets[aid]
    # candidate clean sources: the render file itself + the (maybe ESRGAN) derivative
    cands = []
    for p in (rp, Path(a["derivative_path"]) if a.get("derivative_path") else None):
        if p and p.is_file():
            co = clean_open(p)
            if co:
                cands.append((co[1], p, co[0]))     # (long, path, image)
    if not cands:
        print(f"  NO CLEAN SOURCE for {rp.name} — leaving as-is", flush=True); failed += 1; continue
    long, srcpath, img = max(cands, key=lambda t: t[0])
    if long < need_long:                            # upscale via Lanczos
        sc = need_long / long
        img = img.resize((round(img.width * sc), round(img.height * sc)), Image.Resampling.LANCZOS)
    # cap long side at 6200 to avoid bloat
    if max(img.size) > 6200:
        sc = 6200 / max(img.size)
        img = img.resize((round(img.width * sc), round(img.height * sc)), Image.Resampling.LANCZOS)
    # save over the render file the exporter reads
    if rp.suffix.lower() in (".jpg", ".jpeg"):
        img.save(rp, "JPEG", quality=93)
    else:
        img.convert("RGB").save(rp, "JPEG")
    tag = "ESRGAN-deriv" if srcpath != rp else "lanczos-self"
    print(f"  {rp.name[:30]:30s} <- {srcpath.name[:20]:20s} [{tag}] -> {img.size[0]}x{img.size[1]}", flush=True)
    fixed += 1

print(f"\nDONE: {fixed} fixed, {failed} had no clean source", flush=True)
