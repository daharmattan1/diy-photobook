"""Schema v2 spine for the photo book: the single owner of the geometry
constants, the tolerant cell accessors, the ONE unified DPI formula, and the
`.bak`-guarded idempotent migrator.

Every renderer, the composer, the validator, the exporter, and the editor read
a cell through the SAME accessors and compute resolution through the SAME
`effective_dpi`, so the preview and the print exporter can never silently
disagree about what a cell holds or whether it prints.

Import direction: this module imports NOTHING from ``bookdef``/``export_book``/
``bookrender`` — those import FROM here — so the constants have a single home
without an import cycle.

Schema v2 cell shapes (both accepted everywhere via the accessors):

* legacy / bare  : ``"<asset_id>"``  or ``null`` (an empty slot)
* v2 object      : ``{"id", "asset_id", "crop", "dpi_override"}``

``crop`` is a normalized cover-window ``{"x","y","w","h"}`` in 0..1 (a
sub-rectangle of the source that cover-fills the cell). ``dpi_override`` is an
explicit human allow-with-confirm record ``{"approved_by","dpi","date"}``.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Canonical geometry + resolution constants (single source of truth) -------
PAGE_IN = 12.0
M = 0.12          # outer margin (inches)
G = 0.05          # gutter between cells (inches)
TITLE_H = 0.65    # reserved chapter-title band height (inches)
DPI_MIN = 300     # ideal print DPI
DPI_OK = 240      # hard print floor
OFF_WHITE = "#F6F6F3"
SCHEMA_VERSION = "2.0"


# --- Tolerant cell accessors --------------------------------------------------
def cell_asset(cell: Any) -> str | None:
    """Return the asset_id a cell holds, or None for an empty slot.

    Accepts a bare asset_id string, a v2 cell object, or ``None``.
    """
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell or None
    if isinstance(cell, dict):
        value = cell.get("asset_id")
        return value or None
    return None


def cell_crop(cell: Any) -> dict | None:
    """Return the normalized cover-crop window ``{x,y,w,h}`` or None."""
    if isinstance(cell, dict):
        crop = cell.get("crop")
        if isinstance(crop, dict) and all(k in crop for k in ("x", "y", "w", "h")):
            return crop
    return None


def cell_dpi_override(cell: Any) -> dict | None:
    """Return the per-cell dpi_override record ``{approved_by,dpi,date}`` or None."""
    if isinstance(cell, dict):
        override = cell.get("dpi_override")
        if isinstance(override, dict) and override.get("dpi"):
            return override
    return None


def cell_caption(cell: Any) -> str | None:
    """Return the optional per-cell caption text, or None.

    Tolerant like :func:`cell_crop`: a bare string / ``None`` cell, a missing
    key, a non-string value, or an all-whitespace caption all read as no
    caption. The original (untrimmed) string is returned so an editor edit
    round-trips exactly; renderers escape + trim as needed.
    """
    if isinstance(cell, dict):
        caption = cell.get("caption")
        if isinstance(caption, str) and caption.strip():
            return caption
    return None


def cell_id(cell: Any) -> str | None:
    if isinstance(cell, dict):
        return cell.get("id")
    return None


def make_cell(
    asset_id: str | None,
    *,
    crop: dict | None = None,
    dpi_override: dict | None = None,
    id: str | None = None,
    caption: str | None = None,
) -> dict:
    """Build a v2 cell object with a stable id.

    ``caption`` is an optional per-cell caption string (``None`` = no caption).
    """
    return {
        "id": id or uuid.uuid4().hex,
        "asset_id": asset_id,
        "crop": crop,
        "dpi_override": dpi_override,
        "caption": caption,
    }


def normalize_cell(cell: Any) -> dict:
    """Coerce any accepted cell form into a v2 object (idempotent)."""
    if isinstance(cell, dict) and "id" in cell:
        cell.setdefault("asset_id", cell.get("asset_id"))
        cell.setdefault("crop", None)
        cell.setdefault("dpi_override", None)
        cell.setdefault("caption", None)
        return cell
    return make_cell(
        cell_asset(cell),
        crop=cell_crop(cell),
        dpi_override=cell_dpi_override(cell),
        caption=cell_caption(cell),
    )


# --- The ONE unified resolution formula --------------------------------------
def contained_print_size(photo: dict, cell) -> tuple[float, float]:
    """Printed inches when the whole photo is CONTAIN-fit inside the cell."""
    source_aspect = (photo.get("w") or 1) / max(1, photo.get("h") or 1)
    cell_aspect = cell[2] / max(0.0001, cell[3])
    if source_aspect >= cell_aspect:
        return cell[2], cell[2] / source_aspect
    return cell[3] * source_aspect, cell[3]


def effective_dpi(photo: dict, cell, crop: dict | None = None) -> float:
    """Print DPI of ``photo`` in a cell of geometry ``cell`` (x,y,w,h inches).

    * No crop  -> contain-fit DPI (the whole image, letterboxed). For an
      aspect-matched cell this equals the cover DPI, so the composer floor and
      the exporter agree.
    * Crop     -> the crop window ``{x,y,w,h}`` (0..1) cover-fills the cell;
      DPI is limited by ``min(px_w/cell_w, px_h/cell_h)``.
    """
    w = float(photo.get("w") or 0.0)
    h = float(photo.get("h") or 0.0)
    cell_w = float(cell[2]) if len(cell) > 2 else 0.0
    cell_h = float(cell[3]) if len(cell) > 3 else 0.0
    if w <= 0 or h <= 0 or cell_w <= 0 or cell_h <= 0:
        return 0.0
    if crop:
        px_w = w * float(crop.get("w", 1.0) or 1.0)
        px_h = h * float(crop.get("h", 1.0) or 1.0)
        return min(px_w / cell_w, px_h / cell_h)
    print_w, print_h = contained_print_size(photo, cell)
    return min(w / max(1e-6, print_w), h / max(1e-6, print_h))


def cell_override_ok(cell: Any, dpi: float, floor: int = DPI_OK) -> bool:
    """True if a sub-floor cell carries a human dpi_override covering it."""
    override = cell_dpi_override(cell)
    if not override:
        return False
    # The override records the DPI it approved; accept if it approved at or
    # below the actual placement DPI (i.e. it acknowledged this bad-or-worse fit).
    try:
        return float(override.get("dpi")) <= dpi + 0.5
    except (TypeError, ValueError):
        return True


def derive_print_ready(book: dict, photo_by_id: dict[str, dict]) -> tuple[bool, list[dict]]:
    """Recompute print-readiness crop-aware. print_ready == no non-overridden
    sub-240 cells. Returns ``(print_ready, low_dpi_cells)``.
    """
    low: list[dict] = []
    templates = book.get("templates", {})
    for spread_index, spread in enumerate(book.get("spreads", [])):
        for side_index, side in enumerate(("left", "right")):
            page = spread.get(side, {})
            template = templates.get(page.get("template"), {})
            geom = template.get("cells", [])
            entries = page.get("cells", [])
            page_number = spread_index * 2 + side_index + 1
            for cell_index, entry in enumerate(entries):
                asset_id = cell_asset(entry)
                if not asset_id or cell_index >= len(geom):
                    continue
                photo = photo_by_id.get(asset_id)
                if not photo:
                    continue
                dpi = effective_dpi(photo, geom[cell_index], cell_crop(entry))
                # 4-decimal cell geometry can make an exact 240 read as 239.99.
                if dpi + 0.1 < DPI_OK and not cell_override_ok(entry, dpi):
                    low.append({
                        "page": page_number,
                        "spread": spread_index,
                        "side": side,
                        "cell": cell_index,
                        "asset_id": asset_id,
                        "dpi": round(dpi, 1),
                    })
    return (not low, low)


def geometry_meta() -> dict:
    return {
        "page_in": PAGE_IN,
        "margin_in": M,
        "gutter_in": G,
        "title_in": TITLE_H,
    }


# --- Migrator (in-memory + `.bak`-guarded file) ------------------------------
def migrate_book(book: dict) -> dict:
    """Upgrade a book dict to schema v2 IN PLACE. Idempotent.

    * stable ``spread.id`` (uuid) on every spread
    * ``page.cells`` string arrays -> v2 cell objects
    * ``meta.geometry`` + ``meta.book_revision`` + ``meta.schema_version``
    """
    meta = book.setdefault("meta", {})
    if meta.get("schema_version") == SCHEMA_VERSION:
        return book

    meta["schema_version"] = SCHEMA_VERSION
    meta.setdefault("book_revision", 0)
    meta["geometry"] = geometry_meta()

    for spread in book.get("spreads", []):
        if not spread.get("id"):
            spread["id"] = uuid.uuid4().hex
        for side in ("left", "right"):
            page = spread.get(side)
            if not page:
                continue
            page["cells"] = [normalize_cell(cell) for cell in page.get("cells", [])]
    return book


def migrate_book_file(path: Path) -> dict:
    """Load, `.bak`-guard, migrate, and (if changed) atomically rewrite a book.json.

    Returns the migrated book dict. A book already at v2 is returned untouched
    (no `.bak`, no rewrite) — the migration is idempotent.
    """
    path = Path(path)
    book = json.loads(path.read_text(encoding="utf-8"))
    if book.get("meta", {}).get("schema_version") == SCHEMA_VERSION:
        return book

    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    migrate_book(book)
    _atomic_write_json(path, book)
    return book


def _atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
