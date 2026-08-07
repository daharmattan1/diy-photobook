"""Blurb/lab exporter: full-res sources -> 3600px RGB pages -> PDF."""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import shutil
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps

# These are the owner's own local photos (trusted). Some originals (stitched
# panoramas / scans) exceed PIL's default ~178 MP decompression-bomb guard, which
# otherwise aborts the export mid-render. Disable the guard for this trusted pipeline.
Image.MAX_IMAGE_PIXELS = None

from . import db
from .bookdef import DPI_OK
from .bookmodel import cell_asset, cell_caption, cell_crop, derive_print_ready, effective_dpi
from .bookrender import CROPS

ROOT = Path(__file__).resolve().parents[1]
BOOK_JSON = ROOT / "book.json"
DEFAULT_OUTPUT = ROOT / "export_book"
PAGE_IN = 12.0
CSS_PX_PER_IN = 100
DEVICE_SCALE_FACTOR = 3
PAGE_PX = 3600
# --- Blurb full-bleed print geometry (verified against Blurb PDF-to-Book spec) ---
BLEED_IN = 0.125                    # image extends this far PAST the trim on every edge
SAFE_IN = 0.25                      # text/faces stay at least this far INSIDE the trim
DOC_IN = PAGE_IN + 2 * BLEED_IN     # 12.25in full-bleed document page (trim is 12in)
DOC_PX = round(DOC_IN * 300)        # 3675 — the rendered page canvas
BLEED_PX = round(BLEED_IN * 300)    # 38  — trim offset: page-coord 0 maps to this px
SAFE_PX = round(SAFE_IN * 300)      # 75
OFF_WHITE = "#F6F6F3"


@dataclass(frozen=True)
class ResolvedSource:
    asset_id: str
    render_path: Path
    original_path: Path
    kind: str
    byte_verified: bool


def _repo_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _load_assets(asset_ids: Iterable[str], db_path: Path | None = None) -> dict[str, dict]:
    ids = sorted(set(asset_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id,tags,w,h,staged_original_path,zip_source,zip_entry,"
            "derivative_path FROM assets WHERE asset_id IN (" + placeholders + ")",
            ids,
        ).fetchall()
    assets = {row["asset_id"]: dict(row) for row in rows}
    missing = sorted(set(ids) - set(assets))
    if missing:
        raise KeyError(f"{len(missing)} placed asset(s) absent from manifest.db: {missing[:5]}")
    return assets


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _full_res(asset_id: str, asset: dict, work_dir: Path) -> ResolvedSource:
    """Resolve staged original, verified ZIP entry, or flagged derivative fallback."""
    staged = _repo_path(asset.get("staged_original_path"))
    if staged and staged.is_file():
        original, kind, verified = staged, "staged", False
    else:
        zip_source = _repo_path(asset.get("zip_source"))
        zip_entry = asset.get("zip_entry")
        if zip_source and zip_source.is_file() and zip_entry:
            with zipfile.ZipFile(zip_source) as archive:
                try:
                    payload = archive.read(zip_entry)
                except KeyError as exc:
                    raise FileNotFoundError(f"{asset_id}: ZIP entry missing: {zip_entry}") from exc
            actual = hashlib.sha1(payload).hexdigest()
            if actual != asset_id:
                raise ValueError(f"{asset_id}: ZIP sha1 mismatch ({actual})")
            suffix = Path(zip_entry).suffix or ".jpg"
            original = work_dir / "sources" / f"{asset_id}{suffix.lower()}"
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(payload)
            kind, verified = "zip", True
        else:
            derivative = _repo_path(asset.get("derivative_path"))
            if not derivative or not derivative.is_file():
                raise FileNotFoundError(f"{asset_id}: no staged, ZIP, or derivative source")
            original, kind, verified = derivative, "derivative-2560", False

    crop = CROPS.get(asset_id)
    render_path = crop if crop and crop.is_file() else original
    if render_path != original:
        kind += "+watermark-crop"
    return ResolvedSource(asset_id, render_path, original, kind, verified)


def _placed_ids(book: dict, spread_indices: Sequence[int] | None = None) -> list[str]:
    selected = set(spread_indices) if spread_indices is not None else None
    result: list[str] = []
    for index, spread in enumerate(book["spreads"]):
        if selected is not None and index not in selected:
            continue
        for side in ("left", "right"):
            result.extend(
                aid for aid in (cell_asset(entry) for entry in spread[side].get("cells", []))
                if aid
            )
    return result


def _cell_box(cell: Sequence[float], bleed: bool, bleed_in: float):
    x, y, width, height = map(float, cell[:4])
    if bleed:
        if x <= 0:
            x -= bleed_in
            width += bleed_in
        if y <= 0:
            y -= bleed_in
            height += bleed_in
        if x + width >= PAGE_IN:
            width += bleed_in
        if y + height >= PAGE_IN:
            height += bleed_in
    return x, y, width, height


def _page_html(page: dict, book: dict, sources: dict, assets: dict) -> str:
    template = book["templates"][page["template"]]
    bleed_in = float(book.get("meta", {}).get("bleed_in", 0.125))
    parts: list[str] = []
    if template.get("title") and page.get("title"):
        parts.append(
            '<div class="title"><span class="chapter">Chapter</span>'
            f'<span class="name">{html.escape(page["title"])}</span></div>'
        )
    page_cells = page.get("cells", [])
    fits = page.get("fits", [])
    for index, cell in enumerate(template["cells"]):
        entry = page_cells[index] if index < len(page_cells) else None
        asset_id = cell_asset(entry)
        if not asset_id:
            continue
        x, y, width, height = _cell_box(cell, bool(template.get("bleed")), bleed_in)
        fit = fits[index] if index < len(fits) else template.get("default_fit", "contain")
        if fit != "contain" and not page.get("manual_crop"):
            raise ValueError(
                f"Automatic crop forbidden for {asset_id} in cell {index}: {fit}"
            )
        rotation = float(cell[4]) if len(cell) > 4 else 0
        decoration = ""
        if template.get("scatter"):
            decoration = (
                f"transform:rotate({rotation}deg);border:10px solid #fff;"
                "box-shadow:0 6px 20px rgba(0,0,0,.28);"
            )
        source = sources[asset_id].render_path.resolve().as_uri()
        caption = cell_caption(entry)
        caption_html = (
            f'<div class="caption">{html.escape(caption.strip())}</div>' if caption else ""
        )
        parts.append(
            f'<div class="cell" data-asset="{asset_id}" style="left:{x*100}px;top:{y*100}px;'
            f'width:{width*100}px;height:{height*100}px;{decoration}">'
            f'<img src="{html.escape(source)}" style="object-fit:{fit}" alt="">{caption_html}</div>'
        )
    background = book.get("meta", {}).get("bg", OFF_WHITE)
    return f'''<!doctype html><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0}}html,body{{width:1200px;height:1200px;overflow:hidden;background:{background}}}
.page{{position:relative;width:100%;height:100%;overflow:hidden;background:{background}}}
.cell{{position:absolute;overflow:hidden;background:{background}}}.cell img{{width:100%;height:100%;display:block}}
.cell .caption{{position:absolute;left:0;right:0;bottom:0;padding:5px 10px;font:400 11pt Georgia,serif;
line-height:1.25;color:#2b2a28;background:rgba(246,246,243,.82);text-align:center;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}}
.title{{position:absolute;left:18px;top:14px;width:1164px}}.chapter{{display:block;font:400 8pt Georgia,serif;
letter-spacing:.24em;text-transform:uppercase;color:#9a948c}}.name{{display:block;font:400 22pt Georgia,serif;
color:#2b2a28;margin-top:2px}}</style><main class="page">{"".join(parts)}</main>'''


# Chapter-opener eyebrow ordinals ("CHAPTER ONE"). By default the ordinal is
# derived from each chapter's ORDER of appearance in the book, so no per-book
# configuration is required. A book may override the label for a given chapter
# title via meta.chapter_ordinals: {"<chapter title>": "Three", ...}.
_ORDINAL_WORDS = [
    "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
    "Eighteen", "Nineteen", "Twenty",
]


# Cross-platform font resolution. The book's typography wants a serif (chapter
# titles/captions) and a sans (rare). We look, in order, at: an explicit env
# override, then a set of common serif/sans face filenames across the standard
# Windows / macOS / Linux font directories. If nothing is found we fall back to
# PIL's bundled default so the exporter still runs (just with plainer type).
_FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path.home() / ".local/share/fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path.home() / "Library/Fonts",
]
_FONT_NAMES = {
    ("serif", False): ["georgia.ttf", "Georgia.ttf", "DejaVuSerif.ttf",
                       "LiberationSerif-Regular.ttf", "times.ttf", "Times New Roman.ttf",
                       "NotoSerif-Regular.ttf", "FreeSerif.ttf"],
    ("serif", True):  ["georgiai.ttf", "Georgia-Italic.ttf", "DejaVuSerif-Italic.ttf",
                       "LiberationSerif-Italic.ttf", "timesi.ttf",
                       "NotoSerif-Italic.ttf", "FreeSerifItalic.ttf"],
    ("sans", False):  ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf",
                       "LiberationSans-Regular.ttf", "Helvetica.ttf",
                       "NotoSans-Regular.ttf", "FreeSans.ttf"],
}
_FONT_CACHE: dict[tuple[str, bool], str | None] = {}


def _find_font_file(names: list[str]) -> str | None:
    for base in _FONT_DIRS:
        if not base.is_dir():
            continue
        for name in names:
            direct = base / name
            if direct.is_file():
                return str(direct)
        # Linux nests faces one or more levels deep (e.g. dejavu/, truetype/).
        try:
            for child in base.rglob("*.ttf"):
                if child.name in names:
                    return str(child)
        except (OSError, PermissionError):
            continue
    return None


def _font(size: int, serif: bool = True, italic: bool = False):
    family = "serif" if serif else "sans"
    env = os.environ.get("PHOTOBOOK_FONT_SERIF" if serif else "PHOTOBOOK_FONT_SANS")
    if env and Path(env).is_file():
        return ImageFont.truetype(env, size=size)
    key = (family, bool(italic))
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = _find_font_file(_FONT_NAMES[key])
        if _FONT_CACHE[key] is None and italic:
            _FONT_CACHE[key] = _find_font_file(_FONT_NAMES[(family, False)])
    resolved = _FONT_CACHE[key]
    if resolved:
        return ImageFont.truetype(resolved, size=size)
    return ImageFont.load_default(size=size)


def _draw_caption(canvas: Image.Image, text: str, box: tuple[int, int, int, int]) -> None:
    """Draw an optional caption as a subtle Georgia line on a translucent off-white
    band at the bottom edge of a cell — dark ink on the off-white, matching the
    title-band typography. Positioned at the bottom so it stays clear of faces.
    """
    x, y, width, height = box
    pad = 14
    font = _font(34)  # ~8pt at 300 px/in — smaller than the 22pt chapter title
    probe = ImageDraw.Draw(canvas)

    def text_width(value: str) -> int:
        left, _, right, _ = probe.textbbox((0, 0), value, font=font)
        return right - left

    inner = max(1, width - 2 * pad)
    shown = text
    if text_width(shown) > inner:
        while shown and text_width(shown + "…") > inner:
            shown = shown[:-1]
        shown = (shown.rstrip() + "…") if shown else "…"

    left, top, right, bottom = probe.textbbox((0, 0), shown, font=font)
    text_w, text_h = right - left, bottom - top
    band_h = text_h + 2 * pad
    # Hold the caption band inside the safe zone (>= SAFE_IN from the trim), so a
    # bottom-edge full-bleed cell's caption never rides the cut line.
    lo_x, hi_x = BLEED_PX + SAFE_PX, canvas.width - BLEED_PX - SAFE_PX
    hi_y = canvas.height - BLEED_PX - SAFE_PX
    bx0, bx1 = max(lo_x, x), min(hi_x, x + width)
    by1 = min(hi_y, y + height)
    by0 = by1 - band_h
    if bx1 <= bx0 or by1 <= by0:
        return
    overlay = Image.new("RGBA", (bx1 - bx0, by1 - by0), (246, 246, 243, 210))
    region = canvas.crop((bx0, by0, bx1, by1)).convert("RGBA")
    canvas.paste(Image.alpha_composite(region, overlay).convert("RGB"), (bx0, by0))

    draw = ImageDraw.Draw(canvas)
    text_x = bx0 + ((bx1 - bx0) - text_w) // 2 - left
    text_y = by0 + pad - top
    draw.text((text_x, text_y), shown, font=font, fill="#2b2a28")


def _apply_crop(image: Image.Image, crop: dict | None) -> Image.Image:
    """Crop to a normalized cover-window {x,y,w,h} (0..1) of the source."""
    if not crop:
        return image
    w, h = image.width, image.height
    x0 = max(0, min(w - 1, round(float(crop.get("x", 0.0)) * w)))
    y0 = max(0, min(h - 1, round(float(crop.get("y", 0.0)) * h)))
    x1 = max(x0 + 1, min(w, round((float(crop.get("x", 0.0)) + float(crop.get("w", 1.0))) * w)))
    y1 = max(y0 + 1, min(h, round((float(crop.get("y", 0.0)) + float(crop.get("h", 1.0))) * h)))
    return image.crop((x0, y0, x1, y1))


def _fit_image(source: Path, width: int, height: int, contain: bool,
               crop: dict | None = None) -> Image.Image:
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        # Color management: honor an embedded ICC profile (iPhone HEICs are
        # Display-P3) by converting to sRGB — a bare convert("RGB") would
        # reinterpret P3 numbers as sRGB and shift saturated tones in print.
        icc = image.info.get("icc_profile")
        if icc:
            try:
                import io as _io
                from PIL import ImageCms
                src_prof = ImageCms.ImageCmsProfile(_io.BytesIO(icc))
                dst_prof = ImageCms.createProfile("sRGB")
                image = ImageCms.profileToProfile(
                    image.convert("RGB"), src_prof, dst_prof, outputMode="RGB")
            except Exception:
                image = image.convert("RGB")   # bad/exotic profile: fall back
        else:
            image = image.convert("RGB")
    # A crop window selects a sub-region that then COVER-fills the cell.
    if crop:
        image = _apply_crop(image, crop)
        scale = max(width / image.width, height / image.height)
        resized = image.resize(
            (max(width, round(image.width * scale)), max(height, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        left = (resized.width - width) // 2
        top = (resized.height - height) // 2
        return resized.crop((left, top, left + width, top + height))
    if contain:
        # Fit the whole image inside the box, filling one dimension exactly and
        # centering the other as an EVEN margin. Unlike thumbnail() this UPSCALES a
        # low-res source to fill its box, matching the CSS-preview (object-fit:
        # contain) so export and preview agree — a low-res source prints soft but
        # full (the DPI gate still blocks production until a sharp swap lands),
        # never marooned small in white.
        scale = min(width / image.width, height / image.height)
        new_w, new_h = max(1, round(image.width * scale)), max(1, round(image.height * scale))
        fitted = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        result = Image.new("RGB", (width, height), OFF_WHITE)
        result.paste(fitted, ((width - new_w) // 2, (height - new_h) // 2))
        return result
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(width, round(image.width * scale)), max(height, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _render_pages(page_specs: list, book: dict, sources: dict, assets: dict,
                  work_dir: Path, pages_dir: Path) -> list[Path]:
    """Compose pages natively at 300 px/in; no browser downsampling or OOM risk."""
    del work_dir  # retained in the signature because ZIP extraction uses the same work scope
    background = book.get("meta", {}).get("bg", OFF_WHITE)
    if background.upper() != OFF_WHITE:
        raise ValueError(f"HARD constraint: background {background} != {OFF_WHITE}")
    bleed_in = float(book.get("meta", {}).get("bleed_in", 0.125))
    ordinal_overrides = book.get("meta", {}).get("chapter_ordinals", {}) or {}
    chapter_seen = 0
    outputs: list[Path] = []
    for page_number, (_, _, page_def) in enumerate(page_specs, 1):
        # RESUME: a page rendered by a prior (killed) run is kept if it passes the
        # same size/mode assertion — long exports survive being stopped mid-run.
        existing = pages_dir / f"page-{page_number:03d}.png"
        if existing.is_file():
            try:
                with Image.open(existing) as prior:
                    if prior.size == (DOC_PX, DOC_PX) and prior.mode == "RGB":
                        outputs.append(existing)
                        continue
            except Exception:
                pass  # unreadable/partial file: fall through and re-render
        canvas = Image.new("RGB", (DOC_PX, DOC_PX), background)
        template = book["templates"][page_def["template"]]
        cells = page_def.get("cells", [])
        fits = page_def.get("fits", [])
        for index, cell in enumerate(template["cells"]):
            entry = cells[index] if index < len(cells) else None
            asset_id = cell_asset(entry)
            if not asset_id:
                continue
            crop = cell_crop(entry)
            x, y, width, height = _cell_box(cell, bool(template.get("bleed")), bleed_in)
            box_width, box_height = max(1, round(width * 300)), max(1, round(height * 300))
            fit = fits[index] if index < len(fits) else template.get("default_fit", "contain")
            if fit != "contain" and not page_def.get("manual_crop"):
                raise ValueError(
                    f"Automatic crop forbidden for {asset_id} in cell {index}: {fit}"
                )
            fitted = _fit_image(
                sources[asset_id].render_path,
                box_width,
                box_height,
                contain=fit == "contain",
                crop=crop,
            )
            paste_x, paste_y = round((x + BLEED_IN) * 300), round((y + BLEED_IN) * 300)
            if template.get("scatter"):
                border = 30
                framed = ImageOps.expand(fitted, border=border, fill="white")
                rotation = float(cell[4]) if len(cell) > 4 else 0
                framed = framed.rotate(-rotation, resample=Image.Resampling.BICUBIC,
                                       expand=True, fillcolor=background)
                paste_x += (box_width - framed.width) // 2
                paste_y += (box_height - framed.height) // 2
                canvas.paste(framed, (paste_x, paste_y))
            else:
                canvas.paste(fitted, (paste_x, paste_y))
            caption = cell_caption(entry)
            if caption:
                _draw_caption(
                    canvas, caption.strip(),
                    (round((x + BLEED_IN) * 300), round((y + BLEED_IN) * 300),
                     box_width, box_height),
                )
        if template.get("title") and page_def.get("title"):
            draw = ImageDraw.Draw(canvas)
            # Chapter opener: eyebrow + rule + italic title, CENTERED on the trim.
            # Centering is deliberate — a left-anchored title lands in the GUTTER on a
            # recto (right-hand) page and gets swallowed by the perfect binding.
            title = page_def["title"]
            cx = BLEED_PX + round(6.0 * 300)          # horizontal center of the 12in trim
            ordinal = ordinal_overrides.get(title) or (
                _ORDINAL_WORDS[chapter_seen] if chapter_seen < len(_ORDINAL_WORDS) else "")
            chapter_seen += 1
            eyebrow = " ".join(("CHAPTER " + ordinal).strip().upper())
            draw.text((cx, BLEED_PX + round(0.42 * 300)), eyebrow,
                      font=_font(34), fill="#9a948c", anchor="mt")
            ry = BLEED_PX + round(0.74 * 300)
            draw.line([(cx - round(0.85 * 300), ry), (cx + round(0.85 * 300), ry)],
                      fill="#b0967a", width=3)
            draw.text((cx, BLEED_PX + round(0.84 * 300)), title,
                      font=_font(92, italic=True), fill="#2b2a28", anchor="mt")
        output = pages_dir / f"page-{page_number:03d}.png"
        canvas.save(output, format="PNG", dpi=(300, 300), optimize=True)
        with Image.open(output) as rendered:
            if rendered.size != (DOC_PX, DOC_PX) or rendered.mode != "RGB":
                raise AssertionError(f"{output.name}: expected {DOC_PX}x{DOC_PX} RGB, got {rendered.size} {rendered.mode}")
        outputs.append(output)
    return outputs


def _extend_to_edges(img: Image.Image, cx0: int, cy0: int, cx1: int, cy1: int) -> Image.Image:
    """Replicate the covered rect's border out to the canvas edges, filling the
    ≤2px bleed gaps a trim-aligned paste leaves. The fill lands entirely inside the
    bleed (trimmed off at the cut), so it is never visible on the finished page."""
    W, H = img.size
    if cy0 > 0:
        img.paste(img.crop((cx0, cy0, cx1, cy0 + 1)).resize((cx1 - cx0, cy0)), (cx0, 0))
    if cy1 < H:
        img.paste(img.crop((cx0, cy1 - 1, cx1, cy1)).resize((cx1 - cx0, H - cy1)), (cx0, cy1))
    if cx0 > 0:
        img.paste(img.crop((cx0, 0, cx0 + 1, H)).resize((cx0, H)), (0, 0))
    if cx1 < W:
        img.paste(img.crop((cx1 - 1, 0, cx1, H)).resize((W - cx1, H)), (cx1, 0))
    return img


def _blurb_page_transform(canvas: Image.Image, recto: bool) -> Image.Image:
    """Map a full-bleed 12.25in page (12.0in trim, 0.125 bleed all sides, 3675px)
    onto Blurb's asymmetric interior page (11.875 x 12.0in): scale the 12.0 design
    down to Blurb's 11.75 trim, then DROP the binding-side bleed (recto = binding on
    the LEFT, verso = binding on the RIGHT) while keeping the outside + top/bottom
    bleed. Only bleed is discarded — no trim content is lost, and there is no
    aspect distortion (uniform scale). This is Fable's verified Option A."""
    S = 11.75 / 12.0                                  # 12.0 trim -> 11.75 Blurb trim
    sw = round(canvas.width * S)                      # ~3599 (square)
    scaled = canvas.resize((sw, sw), Image.Resampling.LANCZOS)
    TW, TH = round(11.875 * 300), round(12.0 * 300)   # 3563 x 3600
    bleed_s = round(BLEED_PX * S)                     # scaled bleed ~37px
    tgt = Image.new("RGB", (TW, TH), (255, 255, 255))
    paste_y = BLEED_PX - bleed_s                      # align top trim edges (~1px)
    if recto:                                         # binding LEFT: design trim x0 -> 0
        paste_x = -bleed_s
    else:                                             # binding RIGHT: design trim x1 -> TW
        paste_x = TW - (scaled.width - bleed_s)
    tgt.paste(scaled, (paste_x, paste_y))
    cx0, cy0 = max(0, paste_x), max(0, paste_y)
    cx1, cy1 = min(TW, paste_x + scaled.width), min(TH, paste_y + scaled.height)
    return _extend_to_edges(tgt, cx0, cy0, cx1, cy1)


def _write_image_pdf(image_paths: Sequence[Path], output: Path,
                     page_sizes_in: Sequence[tuple[float, float]] | None = None,
                     trim_bleed_in: float = 0.0, lossless: bool = False,
                     trim_boxes_pt: Sequence[tuple[float, float, float, float]] | None = None) -> None:
    """Write a bounded-memory PDF whose image XObjects are explicitly DeviceRGB.

    ``trim_bleed_in`` insets the TrimBox from the MediaBox on every edge, so a
    full-bleed page declares MediaBox=BleedBox (the printed sheet) and a smaller
    TrimBox (the finished cut) — what a print shop's imposition needs.
    """
    if not image_paths:
        raise ValueError("Cannot write an empty PDF")
    sizes = list(page_sizes_in or [(PAGE_IN, PAGE_IN)] * len(image_paths))
    tb = trim_bleed_in * 72
    object_count = 2 + len(image_paths) * 3
    offsets = [0] * (object_count + 1)
    with output.open("wb") as pdf:
        pdf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        def obj(object_id: int, payload: bytes) -> None:
            offsets[object_id] = pdf.tell()
            pdf.write(f"{object_id} 0 obj\n".encode())
            pdf.write(payload)
            pdf.write(b"\nendobj\n")
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        page_ids = [3 + index * 3 for index in range(len(image_paths))]
        kids = " ".join(f"{item} 0 R" for item in page_ids)
        obj(2, f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode())
        for index, (image_path, (width_in, height_in)) in enumerate(zip(image_paths, sizes)):
            page_id, image_id, content_id = 3 + index * 3, 4 + index * 3, 5 + index * 3
            width_pt, height_pt = width_in * 72, height_in * 72
            with Image.open(image_path) as opened:
                rgb = opened.convert("RGB")
                pixel_width, pixel_height = rgb.size
                if lossless:
                    # Lossless FlateDecode for crisp text pages (the cover) — no JPEG
                    # ringing around foil/print-quality serif type.
                    stream = zlib.compress(rgb.tobytes(), 9)
                    img_filter = "/FlateDecode"
                else:
                    buffer = io.BytesIO()
                    rgb.save(buffer, format="JPEG", quality=96, subsampling=0, optimize=True)
                    stream = buffer.getvalue()
                    img_filter = "/DCTDecode"
            if trim_boxes_pt is not None:
                tx0, ty0, tx1, ty1 = trim_boxes_pt[index]
            else:
                tx0, ty0, tx1, ty1 = tb, tb, width_pt - tb, height_pt - tb
            obj(page_id, (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:.3f} {height_pt:.3f}] "
                f"/TrimBox [{tx0:.3f} {ty0:.3f} {tx1:.3f} {ty1:.3f}] "
                f"/BleedBox [0 0 {width_pt:.3f} {height_pt:.3f}] "
                f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode())
            obj(image_id, (
                f"<< /Type /XObject /Subtype /Image /Width {pixel_width} /Height {pixel_height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter {img_filter} /Length {len(stream)} >>\nstream\n"
            ).encode() + stream + b"\nendstream")
            drawing = f"q {width_pt:.3f} 0 0 {height_pt:.3f} 0 0 cm /Im0 Do Q".encode()
            obj(content_id, f"<< /Length {len(drawing)} >>\nstream\n".encode() + drawing + b"\nendstream")
        xref = pdf.tell()
        pdf.write(f"xref\n0 {object_count+1}\n0000000000 65535 f \n".encode())
        for object_id in range(1, object_count + 1):
            pdf.write(f"{offsets[object_id]:010d} 00000 n \n".encode())
        pdf.write(f"trailer\n<< /Size {object_count+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())


def _write_cover(page_paths: Sequence[Path], page_count: int, output_dir: Path,
                 cover: dict | None = None) -> dict:
    """Full-wrap dust jacket for the Blurb Large-Square hardcover: the cover art
    panorama runs across back+spine+front; the title sits in the darkest band of the
    FRONT panel; the spine carries a scrimmed title; and the flaps are filled by
    edge-extending the panorama past each fold so a shifted fold never shows a seam.
    The spine width and page count come from the book's cover config (see
    docs/PRINT_RUNBOOK.md — always take the spine width from your print shop's live
    calculator, never a per-page estimate). Cover is JPEG, not lossless, to stay
    under the shop's cover-file size cap. `page_paths`/`page_count` are unused (cover
    no longer reuses a page).

    cover config (all optional, from book meta.cover):
      title   -> front + spine title text  (default: "Your Book Title")
      dates   -> subtitle under the title  (default: "")
      art     -> path to a wide panorama image for the wrap (default: a generated
                 neutral gradient placeholder, so the exporter runs with no assets)
      spine_in-> spine width in inches      (default: 0.75; USE YOUR SHOP'S NUMBER)
    """
    import numpy as np
    from PIL import ImageFilter
    cover = cover or {}
    DPI = 300
    SPINE_IN, FLAP_IN, PANEL_IN, BLEED = float(cover.get("spine_in", 0.75)), 3.792, 12.208, 0.125
    TRIM_W = FLAP_IN * 2 + PANEL_IN * 2 + SPINE_IN            # 32.75
    width_in, height_in = TRIM_W + 2 * BLEED, 12.0 + 2 * BLEED  # 33.0 x 12.25
    W, H = round(width_in * DPI), round(height_in * DPI)       # 9900 x 3675
    b = round(BLEED * DPI)                                     # 38 (trim inset)

    def X(inch: float) -> int:                                # trim-space inch -> canvas px
        return b + round(inch * DPI)

    flapL1 = X(FLAP_IN)
    back1 = X(FLAP_IN + PANEL_IN)
    spine0, spine1 = back1, X(FLAP_IN + PANEL_IN + SPINE_IN)
    front0, front1 = spine1, X(FLAP_IN + PANEL_IN + SPINE_IN + PANEL_IN)

    # --- sunset across back+spine+front, extended >=0.5in past each fold into the flaps
    over = round(0.5 * DPI)
    sun_x0, sun_x1 = max(0, flapL1 - over), min(W, front1 + over)
    region_w = sun_x1 - sun_x0
    art_path = _repo_path(cover.get("art"))
    if art_path and art_path.is_file():
        sunset = Image.open(art_path).convert("RGB")
    else:
        # No cover art supplied: generate a neutral vertical gradient placeholder
        # (deep slate -> warm stone) so the exporter runs end-to-end with no assets.
        ramp = np.linspace(0.0, 1.0, H, dtype=np.float32)
        top, bot = np.array([26, 30, 38], np.float32), np.array([150, 140, 128], np.float32)
        col = top[None, :] + (bot - top)[None, :] * ramp[:, None]        # (H, 3)
        grad = np.broadcast_to(col[:, None, :], (H, region_w, 3))         # (H, region_w, 3)
        sunset = Image.fromarray(grad.astype("uint8"), "RGB")
    sc = max(region_w / sunset.width, H / sunset.height)
    sun = sunset.resize((round(sunset.width * sc), round(sunset.height * sc)), Image.Resampling.LANCZOS)
    lft, top = (sun.width - region_w) // 2, (sun.height - H) // 2
    sun = sun.crop((lft, top, lft + region_w, top + H))
    canvas = Image.new("RGB", (W, H), (18, 15, 13))
    canvas.paste(sun, (sun_x0, 0))
    # flaps: edge-replicate the sunset's outer columns (seamless; fold-placement tolerant)
    if sun_x0 > 0:
        canvas.paste(canvas.crop((sun_x0, 0, sun_x0 + 1, H)).resize((sun_x0, H)), (0, 0))
    if sun_x1 < W:
        canvas.paste(canvas.crop((sun_x1 - 1, 0, sun_x1, H)).resize((W - sun_x1, H)), (sun_x1, 0))
    canvas = canvas.convert("RGBA")

    # --- FRONT title: find the darkest ~2.6in band in the front panel (lower 60%) ---
    front_cx = (front0 + front1) // 2
    lum = np.asarray(canvas.crop((front0, 0, front1, H)).convert("L"), dtype=np.float32).mean(axis=1)
    band_h = round(2.6 * DPI)
    csum = np.cumsum(np.insert(lum, 0, 0))
    y_lo, y_hi = int(0.42 * H), int(0.90 * H)
    best_y, best_v = y_lo, 1e18
    for y in range(y_lo, max(y_lo + 1, y_hi - band_h)):
        v = csum[y + band_h] - csum[y]
        if v < best_v:
            best_v, best_y = v, y
    band_cy = best_y + band_h // 2

    CREAM = (244, 237, 226)
    f_title = _font(round(1.02 * DPI))
    f_dates = _font(round(0.34 * DPI))
    title = cover.get("title", "Your Book Title")
    dates = cover.get("dates", "")

    # feathered dark scrim behind the block for guaranteed contrast
    scrim_a = Image.new("L", (W, H), 0)
    ImageDraw.Draw(scrim_a).ellipse(
        [front_cx - round(4.7 * DPI), band_cy - round(1.75 * DPI),
         front_cx + round(4.7 * DPI), band_cy + round(1.75 * DPI)], fill=115)
    scrim_a = scrim_a.filter(ImageFilter.GaussianBlur(round(0.34 * DPI)))
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    scrim.putalpha(scrim_a)
    canvas.alpha_composite(scrim)

    title_y = band_cy - round(0.34 * DPI)
    tmp = ImageDraw.Draw(canvas)
    tb = tmp.textbbox((front_cx, title_y), title, font=f_title, anchor="mm")
    rule_y = tb[3] + round(0.34 * DPI)
    dates_y = rule_y + round(0.42 * DPI)

    # blurred shadow, then crisp cream text/rule — all on one layer
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shd = ImageDraw.Draw(shadow)
    shd.text((front_cx + 5, title_y + 5), title, font=f_title, fill=(0, 0, 0, 175), anchor="mm")
    shd.text((front_cx + 4, dates_y + 4), dates, font=f_dates, fill=(0, 0, 0, 160), anchor="mm")
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(7)))
    fd = ImageDraw.Draw(canvas)
    fd.text((front_cx, title_y), title, font=f_title, fill=CREAM + (255,), anchor="mm")
    fd.line([(front_cx - round(1.55 * DPI), rule_y), (front_cx + round(1.55 * DPI), rule_y)],
            fill=CREAM + (235,), width=4)
    fd.text((front_cx, dates_y), dates, font=f_dates, fill=CREAM + (240,), anchor="mm")

    # --- SPINE: scrimmed title reading top-to-bottom (US convention) ---
    spine_w = spine1 - spine0
    strip = Image.new("RGBA", (H, spine_w), (0, 0, 0, 130))   # horizontal; dark scrim
    f_spine = _font(round(0.24 * DPI))
    stext = (cover.get("spine") or title).upper()
    if dates.strip():
        stext += "   ·   " + dates.strip()
    ssd = ImageDraw.Draw(strip)
    sb = ssd.textbbox((0, 0), stext, font=f_spine)
    ssd.text(((H - (sb[2] - sb[0])) / 2, (spine_w - (sb[3] - sb[1])) / 2 - sb[1]),
             stext, font=f_spine, fill=CREAM + (255,))
    canvas.alpha_composite(strip.rotate(-90, expand=True), (spine0, 0))

    cover_png = output_dir / "cover.png"
    canvas.convert("RGB").save(cover_png, format="PNG", dpi=(DPI, DPI))
    _write_image_pdf([cover_png], output_dir / "cover.pdf", [(width_in, height_in)],
                     trim_bleed_in=BLEED, lossless=False)
    cover_mb = round((output_dir / "cover.pdf").stat().st_size / 1e6, 1)
    if cover_mb > 90:
        raise ValueError(f"cover.pdf is {cover_mb}MB — exceeds Blurb's 90MB cover cap")
    return {
        "spine_in": SPINE_IN,
        "flap_in": FLAP_IN,
        "panel_in": PANEL_IN,
        "width_in": width_in,
        "height_in": height_in,
        "trim_w_in": TRIM_W,
        "cover_pdf_mb": cover_mb,
        "note": "Blurb Large-Square dust-jacket template; spine 0.75in for 154 premium pages. "
                "Re-run Blurb's spine calculator if page count changes.",
    }


def _dpi_flags(page_specs: list, book: dict, assets: dict) -> list[dict]:
    flags: list[dict] = []
    for page_number, (spread_index, side, page) in enumerate(page_specs, 1):
        template = book["templates"][page["template"]]
        for cell_index, cell in enumerate(template["cells"]):
            cells = page.get("cells", [])
            entry = cells[cell_index] if cell_index < len(cells) else None
            asset_id = cell_asset(entry)
            if not asset_id:
                continue
            asset = assets[asset_id]
            dpi = effective_dpi(asset, cell, cell_crop(entry))
            if dpi < 300:
                flags.append({"page": page_number, "spread": spread_index, "side": side,
                              "cell": cell_index, "asset_id": asset_id, "dpi": round(dpi, 1),
                              "below_auto_floor": dpi < DPI_OK})
    return flags


def _reset_output(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    if resolved in {ROOT.resolve(), ROOT.parent.resolve(), Path(resolved.anchor)}:
        raise ValueError(f"Refusing unsafe export target: {resolved}")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)


def export_book(book_path: Path = BOOK_JSON, output_dir: Path = DEFAULT_OUTPUT,
                db_path: Path | None = None,
                spread_indices: Sequence[int] | None = None) -> dict:
    """Export the full book, or selected zero-based spreads, to PDF and PNG."""
    book_path, output_dir = Path(book_path), Path(output_dir)
    book = json.loads(book_path.read_text(encoding="utf-8"))
    # GEOMETRY GUARD: this exporter's page/cover math is tuned to ONE trim size —
    # Blurb Large-Square 12x12in. A book meta.page_in other than 12 would silently
    # produce a corrupted PDF, so refuse it loudly. To target another shop/size,
    # adapt the geometry per docs/PRINT_RUNBOOK.md (it teaches the math) — this is
    # deliberately a code change, not a config knob the transform would ignore.
    page_in = float(book.get("meta", {}).get("page_in", PAGE_IN))
    if abs(page_in - PAGE_IN) > 1e-6:
        raise ValueError(
            f"This exporter supports Blurb Large-Square {PAGE_IN:g}x{PAGE_IN:g}in only "
            f"(book meta.page_in={page_in:g}). See docs/PRINT_RUNBOOK.md to adapt the "
            f"trim/bleed/spine/cover geometry to your shop and size."
        )
    # Cheap conservative pre-gate: an explicit stored print_ready:False mock is
    # refused before we touch the DB or the output dir.
    if book.get("meta", {}).get("print_ready") is False:
        raise ValueError(
            "Book is not print-ready: it is flagged as a composition mock-up; "
            "resolve all low-DPI cells before production export."
        )
    # Authoritative DERIVED recompute (crop-aware, honoring per-cell
    # dpi_override) — catches a stale/True flag hiding sub-240 cells. The gate is
    # zero non-overridden sub-240 cells across the WHOLE book, so a subset export
    # still cannot smuggle a low-DPI cell into a physical proof.
    gate_assets = _load_assets(_placed_ids(book), db_path)
    print_ready, low_dpi = derive_print_ready(book, gate_assets)
    if not print_ready:
        raise ValueError(
            "Book is not print-ready: "
            f"{len(low_dpi)} non-overridden sub-{DPI_OK}-DPI cell(s) must be "
            f"resolved (replace/upscale/override) before production export. "
            f"low_dpi_cells={low_dpi}"
        )
    # Resume-aware output handling: keep pages from a prior run of the SAME book
    # revision (a marker file records it); any revision change forces a full reset
    # so stale pages can never leak into the PDF.
    pages_dir, work_dir = output_dir / "pages", output_dir / "_work"
    marker = output_dir / "_book_revision.txt"
    revision = str(book.get("meta", {}).get("book_revision", ""))
    if not (marker.is_file() and marker.read_text() == revision and pages_dir.is_dir()):
        _reset_output(output_dir)
        pages_dir.mkdir()
        marker.write_text(revision)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir()
    selected = set(spread_indices) if spread_indices is not None else None
    page_specs: list = []
    for spread_index, spread in enumerate(book["spreads"]):
        if selected is None or spread_index in selected:
            page_specs += [(spread_index, "left", spread["left"]),
                           (spread_index, "right", spread["right"])]
    if not page_specs:
        raise ValueError("No spreads selected")
    # Blurb imposition: the book opens on a single recto and closes on a single
    # verso, with facing spreads between. Bracket the content with one blank leaf
    # (front + back) so every designed left|right spread lands on a true
    # (verso|recto) facing pair instead of straddling a leaf. Full-book export only.
    if selected is None:
        book["templates"].setdefault("__blank", {"cells": []})
        blank_page = {"template": "__blank", "cells": [], "fits": []}
        page_specs = [(-1, "front", blank_page)] + page_specs + [(-1, "back", blank_page)]
    assets = _load_assets(_placed_ids(book, spread_indices), db_path)
    sources = {asset_id: _full_res(asset_id, asset, work_dir)
               for asset_id, asset in assets.items()}
    page_paths = _render_pages(page_specs, book, sources, assets, work_dir, pages_dir)
    # --- Blurb Option A (verified): transform each full-bleed 12.25in proof page to
    # Blurb's asymmetric 11.875 x 12.0in interior page with a parity-correct gutter
    # (recto=binding left, verso=binding right) and a per-page asymmetric TrimBox.
    # The pages/ PNGs stay as the full-bleed proofs; book.pdf is the print-correct one.
    print_dir = output_dir / "pages_print"
    if print_dir.exists():
        shutil.rmtree(print_dir)
    print_dir.mkdir()
    BW_IN, BH_IN = 11.875, 12.0
    W_PT, H_PT = BW_IN * 72, BH_IN * 72           # 855 x 864 pt
    B_PT, TRIM_PT = 0.125 * 72, 11.75 * 72        # 9pt bleed, 846pt trim span
    print_paths: list[Path] = []
    page_sizes: list[tuple[float, float]] = []
    trim_boxes: list[tuple[float, float, float, float]] = []
    for index, src_path in enumerate(page_paths):
        recto = ((index + 1) % 2 == 1)            # PDF page 1 = recto; alternates
        with Image.open(src_path) as opened:
            transformed = _blurb_page_transform(opened.convert("RGB"), recto)
        out_path = print_dir / src_path.name
        transformed.save(out_path, "PNG")
        print_paths.append(out_path)
        page_sizes.append((BW_IN, BH_IN))
        if recto:                                  # binding LEFT: no left bleed
            trim_boxes.append((0.0, B_PT, TRIM_PT, B_PT + TRIM_PT))
        else:                                      # binding RIGHT: no right bleed
            trim_boxes.append((B_PT, B_PT, B_PT + TRIM_PT, W_PT))
    _write_image_pdf(print_paths, output_dir / "book.pdf", page_sizes, trim_boxes_pt=trim_boxes)
    cover = _write_cover(page_paths, len(page_paths), output_dir,
                         book.get("meta", {}).get("cover", {}))
    dpi_flags = _dpi_flags(page_specs, book, assets)
    derivatives = sorted(aid for aid, src in sources.items() if src.kind.startswith("derivative"))
    hash_sample = None
    upscaled = {aid for aid, a in assets.items() if "upscaled" in (a.get("tags") or "")}
    for asset_id, source in sources.items():
        if asset_id in upscaled:
            continue  # an upscaled staged file can't match the original-bytes sha1
        if source.kind.startswith(("staged", "zip")) and "watermark-crop" not in source.kind:
            actual = _sha1(source.original_path)
            hash_sample = {"asset_id": asset_id, "actual_sha1": actual,
                           "matches": actual == asset_id, "source_kind": source.kind}
            if actual != asset_id:
                raise ValueError(f"{asset_id}: sampled original sha1 mismatch")
            break
    report = {
        "page_count": len(page_paths), "page_px": [DOC_PX, DOC_PX], "dpi": 300,
        "color_mode": "RGB", "background": book.get("meta", {}).get("bg"),
        "spread_indices": sorted(selected) if selected is not None else None,
        "full_resolution_sources": len(sources) - len(derivatives),
        "derivative_fallback_assets": derivatives,
        "watermark_crop_assets": sorted(aid for aid, src in sources.items()
                                            if "watermark-crop" in src.kind),
        "dpi_flags": dpi_flags,
        "below_240_dpi": [flag for flag in dpi_flags if flag["below_auto_floor"]],
        "hash_sample": hash_sample, "cover": cover,
    }
    (output_dir / "resolution_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    shutil.rmtree(work_dir)
    return report


if __name__ == "__main__":
    print(json.dumps(export_book(), indent=2))