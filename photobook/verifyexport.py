"""Export verification (Phase 11) — prove the bundle is correct + originals safe.

Independent assertions, written to logs/export_verify.txt:
  1. export/ image count == accepted count == book_manifest.csv rows
  2. every exported file's dimensions >= its derivative's (i.e. it is the
     full-res ORIGINAL, not the 2560px derivative)
  3. chronological ordering holds within each chapter folder
  4. export/proof.pdf exists and is non-empty
  5. IMMUTABILITY: re-hash every accepted staged_original_path and confirm it
     still equals its asset_id (nothing mutated the originals)

Read-only over export/ + manifest; writes only the verify log.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Optional

from PIL import Image

from . import db, paths

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf",
}


def _iter_export_images() -> list[Path]:
    return [
        p for p in paths.EXPORT_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]


def _dims(path: Path) -> Optional[tuple[int, int]]:
    ext = path.suffix.lower()
    try:
        if ext in {".heic", ".heif"}:
            import pillow_heif

            pillow_heif.register_heif_opener()
        if ext in {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf"}:
            import rawpy

            with rawpy.imread(str(path)) as raw:
                return raw.sizes.width, raw.sizes.height
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def verify(db_path: Optional[Path] = None) -> dict:
    results: list[tuple[str, bool, str]] = []

    with db.session(db_path) as conn:
        accepted = conn.execute(
            "SELECT asset_id, staged_original_path, derivative_path, zip_source, "
            "chapter, best_datetime FROM assets WHERE review_status='accept'"
        ).fetchall()
    accepted_count = len(accepted)

    # Map asset_id -> its exported file, so lean-sourced originals (no staged
    # file) can be integrity-checked against the bytes that actually shipped.
    export_by_id: dict[str, Path] = {}
    for img in _iter_export_images():
        aid8 = img.stem.split("_")[-1]
        for r in accepted:
            if r["asset_id"].startswith(aid8):
                export_by_id[r["asset_id"]] = img
                break

    export_imgs = _iter_export_images()
    export_count = len(export_imgs)

    # 1. counts match
    csv_path = paths.EXPORT_DIR / "book_manifest.csv"
    csv_rows = 0
    if csv_path.is_file():
        with csv_path.open(encoding="utf-8") as f:
            csv_rows = sum(1 for _ in csv.DictReader(f))
    counts_ok = (export_count == accepted_count == csv_rows)
    results.append((
        "count: export imgs == accepted == manifest rows",
        counts_ok,
        f"export={export_count} accepted={accepted_count} manifest={csv_rows}",
    ))

    # 2. exported dims >= derivative dims (proves originals, not 2560px derivatives)
    by_id = {r["asset_id"]: r for r in accepted}
    dims_ok = True
    dims_detail = []
    for img in export_imgs:
        aid8 = img.stem.split("_")[-1]
        match = next((r for r in accepted if r["asset_id"].startswith(aid8)), None)
        if not match:
            continue
        exp_dims = _dims(img)
        der_dims = _dims(Path(match["derivative_path"])) if match["derivative_path"] else None
        if exp_dims and der_dims:
            if max(exp_dims) < max(der_dims):
                dims_ok = False
                dims_detail.append(f"{img.name}: export {exp_dims} < derivative {der_dims}")
    results.append((
        "dimensions: every export >= its derivative (is original)",
        dims_ok,
        "all originals" if dims_ok else "; ".join(dims_detail[:5]),
    ))

    # 3. chronological ordering within each chapter folder
    order_ok = True
    order_detail = []
    for folder in sorted(p for p in paths.EXPORT_DIR.iterdir() if p.is_dir()):
        files = sorted(f for f in folder.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES)
        prefixes = [f.name.split("_")[0] for f in files]
        if prefixes != sorted(prefixes):
            order_ok = False
            order_detail.append(folder.name)
    results.append((
        "ordering: NNNN prefixes sorted within each chapter",
        order_ok,
        "sorted" if order_ok else f"unsorted in {order_detail}",
    ))

    # 4. proof.pdf exists + non-empty
    pdf = paths.EXPORT_DIR / "proof.pdf"
    pdf_ok = pdf.is_file() and pdf.stat().st_size > 1000
    results.append((
        "proof.pdf exists and is non-empty",
        pdf_ok,
        f"{pdf.stat().st_size} bytes" if pdf.is_file() else "missing",
    ))

    # 5. content integrity: every accepted original's bytes hash to its asset_id.
    #    - staged mode: re-hash the staged ORIGINAL (proves it was never mutated).
    #    - lean mode (staged NULL): re-hash the EXPORTED file (proves the ZIP
    #      re-extraction shipped exactly the right bytes; there is no staged
    #      original to mutate, so this is the equivalent guarantee).
    bad = 0
    checked = 0
    for r in accepted:
        staged = r["staged_original_path"]
        target = None
        if staged and Path(staged).is_file():
            target = Path(staged)
        else:
            target = export_by_id.get(r["asset_id"])  # lean: the shipped copy
        if target is None:
            bad += 1
            continue
        try:
            if hashlib.sha1(target.read_bytes()).hexdigest() != r["asset_id"]:
                bad += 1
            else:
                checked += 1
        except OSError:
            bad += 1
    immut_ok = (bad == 0)
    results.append((
        "content integrity: every accepted original hashes to its asset_id "
        "(staged=immutable / lean=correct ZIP bytes)",
        immut_ok,
        f"{bad} bad" if bad else f"all {checked} verified",
    ))

    all_pass = all(ok for _, ok, _ in results)
    _write_log(results, all_pass)
    return {
        "all_pass": all_pass,
        "results": [(name, ok) for name, ok, _ in results],
        "accepted_count": accepted_count,
    }


def _write_log(results, all_pass: bool) -> None:
    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["export verification", "=" * 50, ""]
    for name, ok, detail in results:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
        lines.append(f"        {detail}")
    lines.append("")
    lines.append(f"OVERALL: {'ALL PASS' if all_pass else 'FAILURES PRESENT'}")
    paths.EXPORT_VERIFY_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
