"""Export the accepted set as a service-agnostic print bundle (Phase 10).

For every accepted asset, in chronological order within its chapter:
  - copy the full-res ORIGINAL (staged_original_path, NOT the 2560px
    derivative) to  export/<NN_chapter>/<0000_YYYY-MM-DD>_<asset>.<ext>
  - the NNNN prefix guarantees chronological sort within the chapter folder
Then write:
  - export/book_manifest.csv   (order, chapter, filename, date, source, hero, caption)
  - export/captions.txt        (human-readable caption list)
  - export/proof.pdf           (all photos in print order, via Playwright)

EXIF-on-copies-only: if --embed-dates is passed, best_datetime is written into
the EXPORTED COPIES via exiftool — NEVER into source/staged originals.

Originals are only READ + copied here; never mutated.
"""

from __future__ import annotations

import csv
import html
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from . import db, paths


def _chapter_order() -> dict[str, int]:
    ch = yaml.safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    return {c["id"]: i + 1 for i, c in enumerate(ch.get("chapters", []) or [])}


def _chapter_titles() -> dict[str, str]:
    ch = yaml.safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    return {c["id"]: c.get("title", c["id"]) for c in (ch.get("chapters", []) or [])}


def _date_prefix(dt_iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return "0000-00-00"


def _synthetic_order_iso(era_nn: int, within_idx: int) -> str:
    """A monotonic synthetic timestamp encoding (era, position-within-era).

    Per-photo real dates are unreliable here (the era was hand-assigned and
    authoritative, but the underlying capture dates are largely junk). To make a
    date-sorting layout service reproduce the owner's EXACT era spine, we stamp the
    exported COPY with a fake date: each era lands in its own year (era 1 ->
    2001 ... era 7 -> 2007) and photos step forward one hour within the era.
    Ascending date-sort == the owner's order; year-grouping == era chapters.
    Real dates are preserved untouched in book_manifest.csv.
    """
    from datetime import timedelta
    base = datetime(2000 + max(era_nn, 1), 1, 1, 0, 0, 0)
    return (base + timedelta(hours=within_idx)).isoformat()


def _slug(s: str) -> str:
    """Lowercase, keep alnum, collapse everything else to single hyphens."""
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s


def _label_filename(cid: str, event, tags, i: int, asset8: str, ext: str) -> str:
    """Descriptive filename so Mixbook's photo-tray search filters by our metadata.

    Shape:  <era>__<event>__<tag_tag_tag>__NNNN_<asset8><ext>
    e.g.    03-summer__beach-day__family_kids__0003_8fafe662.jpg

    Every metadata token is substring-searchable in Mixbook's tray ("summer",
    "family", the event name). The leading era slug + NNNN also make an A-Z
    filename sort reproduce the era spine. Empty event/tags segments are omitted.
    Mixbook's Pro Tip explicitly recommends this rename-for-search workflow.
    """
    parts = [_slug(cid)]
    ev = _slug(event) if event else ""
    if ev:
        parts.append(ev)
    tagslugs = [_slug(t) for t in (tags or "").split(",") if t.strip()]
    tagslugs = [t for t in tagslugs if t]
    if tagslugs:
        parts.append("_".join(tagslugs))
    prefix = "__".join(parts)
    return f"{prefix}__{i:04d}_{asset8}{ext}"


def export(
    db_path: Optional[Path] = None,
    embed_dates: bool = False,
    order_dates: bool = False,
    label_filenames: bool = False,
    make_pdf: bool = True,
) -> dict:
    ch_order = _chapter_order()
    ch_titles = _chapter_titles()

    # Fresh export dir each run (idempotent, deterministic).
    if paths.EXPORT_DIR.exists():
        shutil.rmtree(paths.EXPORT_DIR)
    paths.EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[dict] = []
    caption_lines: list[str] = []
    copied = 0

    zip_cache: dict[str, "zipfile.ZipFile"] = {}
    missing: list[str] = []
    reduced: list[str] = []   # accepted but original gone -> shipped the 2560px derivative

    with db.session(db_path) as conn:
        accepted = conn.execute(
            "SELECT asset_id, chapter, staged_original_path, zip_source, zip_entry, "
            "derivative_path, orig_path, best_datetime, src, is_hero, event, tags, caption "
            "FROM assets WHERE review_status='accept' "
            "ORDER BY chapter, best_datetime, asset_id"
        ).fetchall()

        # Group by chapter to compute per-chapter ordering.
        by_chapter: dict[str, list] = {}
        for r in accepted:
            by_chapter.setdefault(r["chapter"], []).append(r)

        global_order = 0
        for cid in sorted(by_chapter, key=lambda c: ch_order.get(c, 999)):
            nn = ch_order.get(cid, 999)
            folder = paths.EXPORT_DIR / f"{nn:02d}_{cid}"
            folder.mkdir(parents=True, exist_ok=True)
            rows = sorted(by_chapter[cid], key=lambda r: (r["best_datetime"] or "", r["asset_id"]))
            for i, r in enumerate(rows):
                global_order += 1
                ext = _accept_ext(r)
                if label_filenames:
                    fname = _label_filename(cid, r["event"], r["tags"], i, r["asset_id"][:8], ext)
                else:
                    fname = f"{i:04d}_{_date_prefix(r['best_datetime'])}_{r['asset_id'][:8]}{ext}"
                dest = folder / fname
                kind = _materialize_original(r, dest, zip_cache)
                if not kind:
                    missing.append(r["asset_id"])
                    continue
                if kind == "derivative":
                    dest = dest.with_suffix(".jpg")   # derivative fallback lands as .jpg
                    reduced.append(r["asset_id"])
                copied += 1

                if order_dates:
                    _embed_date_on_copy(dest, _synthetic_order_iso(nn, i))
                elif embed_dates:
                    _embed_date_on_copy(dest, r["best_datetime"])

                rel = str(dest.relative_to(paths.EXPORT_DIR)).replace("\\", "/")
                manifest_rows.append({
                    "order": global_order,
                    "chapter": cid,
                    "chapter_title": ch_titles.get(cid, cid),
                    "file": rel,
                    "date": _date_prefix(r["best_datetime"]),
                    "source": r["src"],
                    "hero": int(r["is_hero"] or 0),
                    "event": r["event"] or "",
                    "tags": r["tags"] or "",
                    "res": "full" if kind == "original" else "reduced-2560",
                    "caption": r["caption"] or "",
                })
                if r["caption"]:
                    caption_lines.append(f"{rel}\t{r['caption']}")

    # Close any ZIP handles opened for lean-mode re-extraction.
    for zf in zip_cache.values():
        try:
            zf.close()
        except Exception:
            pass

    # book_manifest.csv
    csv_path = paths.EXPORT_DIR / "book_manifest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["order", "chapter", "chapter_title", "file", "date", "source", "hero", "event", "tags", "res", "caption"],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    # captions.txt
    (paths.EXPORT_DIR / "captions.txt").write_text(
        "\n".join(caption_lines) + ("\n" if caption_lines else ""), encoding="utf-8"
    )

    pdf_ok = False
    if make_pdf and manifest_rows:
        pdf_ok = _render_proof_pdf(manifest_rows)

    return {
        "accepted_copied": copied,
        "manifest_rows": len(manifest_rows),
        "proof_pdf": pdf_ok,
        "export_dir": str(paths.EXPORT_DIR),
        "missing": missing,
        "reduced": reduced,
    }


def _accept_ext(row) -> str:
    """Extension for an accepted asset's exported file (works staged or lean)."""
    if row["staged_original_path"]:
        return Path(row["staged_original_path"]).suffix.lower()
    if row["zip_entry"]:
        return Path(row["zip_entry"]).suffix.lower()
    if row["orig_path"]:
        return Path(row["orig_path"]).suffix.lower()
    return ".jpg"


def _materialize_original(row, dest: Path, zip_cache: dict, allow_derivative: bool = True) -> str:
    """Put the best available copy of an accepted asset at `dest`.

    Source preference:
      1. staged_original_path        -> full-res original (default mode)
      2. zip_source/zip_entry        -> re-extract full-res from the ZIP part
         (--lean mode). Verifies the bytes hash back to asset_id so a corrupt or
         edited ZIP can't silently ship the wrong photo.
      3. derivative_path (fallback)  -> the 2560px derivative, when the original
         is genuinely unrecoverable (e.g. its Takeout ZIP was deleted). Prints
         fine up to ~half-page; the caller records it as reduced-res so layout
         can avoid a full-bleed blowup. An accepted photo is NEVER silently
         dropped from the book just because its original is gone.

    Returns: "original" | "derivative" | "" (nothing available).
    """
    import hashlib

    staged = row["staged_original_path"]
    if staged and Path(staged).is_file():
        shutil.copy2(staged, dest)
        return "original"

    zip_source = row["zip_source"]
    zip_entry = row["zip_entry"]
    if zip_source and zip_entry:
        zpath = Path(zip_source)
        if zpath.is_file():
            zf = zip_cache.get(str(zpath))
            if zf is None:
                zf = zipfile.ZipFile(zpath)
                zip_cache[str(zpath)] = zf
            try:
                data = zf.read(zip_entry)
            except KeyError:
                data = None
            if data is not None and hashlib.sha1(data).hexdigest() == row["asset_id"]:
                dest.write_bytes(data)
                return "original"

    if allow_derivative:
        deriv = row["derivative_path"]
        if deriv and Path(deriv).is_file():
            # derivative is always .jpg; keep dest's stem, force .jpg suffix
            d = dest.with_suffix(".jpg")
            shutil.copy2(deriv, d)
            return "derivative"

    return ""


def _embed_date_on_copy(copy_path: Path, dt_iso: Optional[str]) -> None:
    """Write best_datetime into an EXPORTED COPY via exiftool. Never the original."""
    if not dt_iso:
        return
    exe = paths.resolve_exiftool()
    if not exe:
        return
    try:
        dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
        stamp = dt.strftime("%Y:%m:%d %H:%M:%S")
        subprocess.run(
            [exe, "-overwrite_original",
             f"-DateTimeOriginal={stamp}", f"-CreateDate={stamp}", str(copy_path)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        pass  # embedding is best-effort; the copy itself is what matters


def _render_proof_pdf(manifest_rows: list[dict]) -> bool:
    """Render export/proof.pdf: all photos in print order via Playwright.

    Uses the deterministic document.fonts.ready render pattern. Images are
    referenced by absolute file:// URIs so Chromium can load them without a server.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False

    # Build a self-contained HTML proof page.
    def _img_uri(rel: str) -> str:
        p = (paths.EXPORT_DIR / rel).resolve()
        return p.as_uri()

    cards = []
    for m in manifest_rows:
        hero = " ★" if m["hero"] else ""
        cards.append(
            f'<figure><img src="{html.escape(_img_uri(m["file"]))}">'
            f'<figcaption>#{m["order"]} · {html.escape(m["chapter_title"])} · '
            f'{html.escape(m["date"])}{hero}</figcaption></figure>'
        )
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<style>
@page {{ size: A4; margin: 12mm; }}
body {{ font-family: system-ui, sans-serif; }}
.grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8mm; }}
figure {{ margin: 0; break-inside: avoid; }}
img {{ width: 100%; height: 70mm; object-fit: cover; border-radius: 3mm; }}
figcaption {{ font-size: 9pt; color: #444; margin-top: 2mm; }}
h1 {{ font-size: 16pt; }}
</style></head><body>
<h1>Photo Book — Proof ({len(manifest_rows)} photos)</h1>
<div class="grid">{''.join(cards)}</div>
</body></html>"""

    proof_html = paths.EXPORT_DIR / "_proof.html"
    proof_html.write_text(doc, encoding="utf-8")
    pdf_path = paths.EXPORT_DIR / "proof.pdf"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(proof_html.as_uri())
            page.evaluate("async () => { await document.fonts.ready; }")
            page.pdf(path=str(pdf_path), format="A4", print_background=True)
            browser.close()
        return pdf_path.exists()
    except Exception:
        return False
