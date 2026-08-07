"""Local, zero-cloud editor server for the photo book.

A stdlib ``ThreadingHTTPServer`` bound to 127.0.0.1 that serves the SPA and a
small JSON API. The book is the artifact: writes are atomic (temp + os.replace)
and revision-guarded (409 on a stale base_revision). The manifest DB is READ
ONLY here — the tray always enumerates the full accepted set (all 455), the
book only *places* a subset, and a photo is never lost.

Design notes
------------
* All book state lives in one ``ServerState`` guarded by a lock; GET handlers
  read a consistent snapshot, POST handlers mutate under the lock.
* Thumbnails are resolved via the DB only (never a client-supplied path) and
  served from a DOWNSCALED ~280px cache — we never ship the 2560px derivative
  to the tray.
* ``/api/relayout`` reuses the composer's ``_spread_packed_layouts`` +
  ``_align_spread_templates`` so a dropped/swapped photo re-fits its cell to the
  new aspect (no letterbox regression), and returns per-cell effective DPI.
* ``print_ready`` is DERIVED on every save and again inside export — never
  hand-set.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image, ImageOps

from . import bookdef, bookrender, db
from .bookmodel import (
    DPI_MIN,
    DPI_OK,
    OFF_WHITE,
    PAGE_IN,
    cell_asset,
    contained_print_size,
    derive_print_ready,
    effective_dpi,
    geometry_meta,
    migrate_book,
    migrate_book_file,
)

ROOT = Path(__file__).resolve().parents[1]
EDITOR_DIR = Path(__file__).resolve().parent / "editor"
DEFAULT_BOOK = ROOT / "book.json"
THUMB_MAX = 280
_ASSET_RE = re.compile(r"^[0-9a-f]{40}$")

_ACCEPTED_SQL = (
    "SELECT asset_id, chapter, event, tags, is_hero, favorited, people_count, "
    "w, h, best_datetime, orig_path, derivative_path "
    "FROM assets WHERE review_status='accept'"
)


def _tags_list(value) -> list[str]:
    if not value:
        return []
    return [t.strip() for t in str(value).split(",") if t.strip()]


def _rev_int(book: dict) -> int:
    """meta.book_revision as an int; a hand-authored non-int value degrades to 0
    (the editor re-numbers on the next save) instead of crashing the server."""
    try:
        return int(book.get("meta", {}).get("book_revision", 0) or 0)
    except (TypeError, ValueError):
        return 0


class ServerState:
    """All mutable editor state, guarded by a single lock."""

    def __init__(self, book_path: Path, db_path: Path | None, thumb_cache_dir: Path):
        self.book_path = Path(book_path)
        self.db_path = Path(db_path) if db_path else None
        self.thumb_cache_dir = Path(thumb_cache_dir)
        self.thumb_cache_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        # Migrate the on-disk book to v2 (`.bak`-guarded) and hold it in memory.
        self.book = migrate_book_file(self.book_path)
        self.revision = _rev_int(self.book)
        # The manifest is read-only; load the full accepted set once.
        self.photos = self._load_photos()
        self.photo_by_id = {p["asset_id"]: p for p in self.photos}
        self.derivative_by_id = {p["asset_id"]: p.get("derivative_path") for p in self.photos}

    def _load_photos(self) -> list[dict]:
        with db.session(self.db_path) as conn:
            rows = conn.execute(_ACCEPTED_SQL).fetchall()
        photos: list[dict] = []
        for row in rows:
            record = dict(row)
            record["favorited"] = bool(record.get("favorited"))
            record["is_hero"] = bool(record.get("is_hero"))
            photos.append(record)
        return photos

    # --- placement -----------------------------------------------------------
    def _uses_index(self, book: dict) -> dict[str, list[dict]]:
        uses: dict[str, list[dict]] = {}
        for spread in book.get("spreads", []):
            spread_id = spread.get("id")
            for side in ("left", "right"):
                page = spread.get(side, {})
                for cell_index, entry in enumerate(page.get("cells", [])):
                    asset_id = cell_asset(entry)
                    if asset_id:
                        uses.setdefault(asset_id, []).append(
                            {"spread_id": spread_id, "side": side, "cell": cell_index}
                        )
        return uses

    def photos_payload(self) -> dict:
        with self.lock:
            book = self.book
            uses = self._uses_index(book)
        photos_out: list[dict] = []
        eras: set[str] = set()
        events: set[str] = set()
        tags: set[str] = set()
        months: set[str] = set()
        favorited_count = 0
        for p in self.photos:
            asset_id = p["asset_id"]
            month = (p.get("best_datetime") or "")[:7]
            photo_tags = _tags_list(p.get("tags"))
            record = {
                "asset_id": asset_id,
                "era": p.get("chapter"),
                "event": p.get("event"),
                "tags": photo_tags,
                "favorited": p["favorited"],
                "is_hero": p["is_hero"],
                "people_count": p.get("people_count"),
                "w": p.get("w"),
                "h": p.get("h"),
                "best_datetime": p.get("best_datetime"),
                "orig_path": p.get("orig_path"),
                "month": month or None,
                "placed": asset_id in uses,
                "uses": uses.get(asset_id, []),
            }
            photos_out.append(record)
            if p.get("chapter"):
                eras.add(p["chapter"])
            if p.get("event"):
                events.add(p["event"])
            tags.update(photo_tags)
            if month:
                months.add(month)
            if p["favorited"]:
                favorited_count += 1
        return {
            "photos": photos_out,
            "count": len(photos_out),
            "placed_count": sum(1 for r in photos_out if r["placed"]),
            "facets": {
                "eras": sorted(eras),
                "events": sorted(events),
                "tags": sorted(tags),
                "months": sorted(months),
                "favorited": favorited_count,
            },
        }

    # --- save ----------------------------------------------------------------
    def _disk_revision(self) -> int | None:
        """book_revision as currently on disk (None if unreadable)."""
        try:
            disk = json.loads(self.book_path.read_text(encoding="utf-8"))
            return _rev_int(disk)
        except Exception:
            return None

    def save_book(self, base_revision: int, incoming: dict) -> tuple[int, dict]:
        with self.lock:
            if int(base_revision) != self.revision:
                return 409, {"error": "stale", "current_revision": self.revision}
            # EXTERNAL-CHANGE GUARD: if another writer (repack/smart_fill batch, a
            # second server) rewrote book.json since we loaded it, a blind save
            # would silently clobber their work with our stale in-memory book —
            # the exact hazard a long-lived editor process creates. Reload the
            # disk book as the new truth and 409; the client's stale-handler
            # refetches and re-renders from it.
            disk_rev = self._disk_revision()
            if disk_rev is not None and disk_rev != self.revision:
                self.book = migrate_book_file(self.book_path)
                self.revision = _rev_int(self.book)
                return 409, {"error": "external_change", "current_revision": self.revision}
            book = migrate_book(incoming)
            print_ready, low_dpi = derive_print_ready(book, self.photo_by_id)
            book.setdefault("meta", {})
            book["meta"]["print_ready"] = print_ready
            book["meta"]["low_dpi_mockup_cells"] = len(low_dpi)
            self.revision += 1
            book["meta"]["book_revision"] = self.revision
            self._atomic_write(book)
            self.book = book
            return 200, {
                "ok": True,
                "book_revision": self.revision,
                "print_ready": print_ready,
                "low_dpi_cells": low_dpi,
            }

    def _atomic_write(self, book: dict) -> None:
        tmp = self.book_path.with_suffix(self.book_path.suffix + f".tmp-{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(book, indent=2), encoding="utf-8")
        os.replace(tmp, self.book_path)

    def book_payload(self) -> dict:
        with self.lock:
            return {"book": self.book, "book_revision": self.revision}


# --- relayout ----------------------------------------------------------------
def _pack_side(arranged, cells, style) -> dict:
    return {
        "assets": [p["asset_id"] for p in arranged],
        "cells": cells,
        "dpi": [round(effective_dpi(p, c, None), 1) for p, c in zip(arranged, cells)],
        "weight": 1 if len(arranged) <= 3 else 2 if len(arranged) <= 5 else 3,
        "layout_style": style,
    }


def _full_bleed_side(ids: list[str], has_title: bool, photo_by_id: dict[str, dict]) -> dict:
    """Lay ONE page out with the SAME full-bleed engine the book uses (smart_fill),
    returning the cover-crop geometry so an edit KEEPS the edge-to-edge fill instead
    of reverting to the old inset composer (the E2 durability bug). Returns cells +
    per-cell crops + fits, so the client can re-apply the crops (not reset them)."""
    if not ids:
        return {"assets": [], "cells": [], "crops": [], "fits": [], "dpi": [],
                "weight": 1, "layout_style": "smart_fill", "bleed": True}
    from scripts.smart_fill import fill_page
    from . import faces as _faces
    meta = {a: photo_by_id[a] for a in ids if a in photo_by_id}
    faces_by = {
        a: (_faces.detect_faces(meta[a].get("derivative_path"), a)
            if meta.get(a, {}).get("derivative_path") else [])
        for a in ids
    }
    _faces.flush_cache()
    _rows, (cells, crops, fits, _cov) = fill_page(ids, meta, faces_by, has_title)
    dpi = [round(effective_dpi(meta[a], c, cr), 1) for a, c, cr in zip(ids, cells, crops)]
    return {"assets": ids, "cells": cells, "crops": crops, "fits": fits, "dpi": dpi,
            "weight": 1 if len(ids) <= 3 else 2 if len(ids) <= 5 else 3,
            "layout_style": "smart_fill", "bleed": True}


def relayout_pages(
    left_ids: list[str],
    right_ids: list[str],
    *,
    left_title: bool,
    right_title: bool,
    photo_by_id: dict[str, dict],
) -> dict:
    """Regenerate a spread's two facing pages for a new photo set, using the
    full-bleed smart_fill engine so an editor edit STAYS edge-to-edge (E2 fix).
    Either side may be empty. Raises ValueError for a side with >7 photos."""
    for ids in (left_ids, right_ids):
        if len(ids) > 7:
            raise ValueError(f"A page may hold at most 7 photos (got {len(ids)})")
    return {
        "left": _full_bleed_side(left_ids, bool(left_title), photo_by_id),
        "right": _full_bleed_side(right_ids, bool(right_title), photo_by_id),
    }


# --- layout picker (A3 "change layout" + A2 "make big") ----------------------
def layout_candidates(
    ids: list[str],
    *,
    title: bool,
    side: str,
    photo_by_id: dict[str, dict],
) -> list[dict]:
    """Enumerate the composer's traced recipes for one page's photo set.

    Reuses ``bookdef._packed_candidates`` (ONE best photo->cell assignment per
    recipe) so every returned layout is real composer geometry — never a
    hand-invented rectangle. Each entry carries the geometry + per-cell DPI (via
    ``_pack_side``), plus the metadata the editor needs: ``coverage`` (printed
    photo area / full page), ``max_cell_asset`` (which photo lands in the
    largest cell, for A2 "make big"), and ``feature_area`` (per-asset contained
    print area, the A2 fallback). Sorted by coverage desc (least white space
    first). Empty ``ids`` -> ``[]``; >7 raises ValueError (handled as 400).
    """
    if not ids:
        return []
    batch = [photo_by_id[a] for a in ids]
    style = bookdef._layout_style(len(batch), side=side, opener=False)
    candidates = bookdef._packed_candidates(batch, title=title, layout_style=style)
    page_area = PAGE_IN * PAGE_IN
    out: list[dict] = []
    for used_area, arranged, cells, recipe in candidates:
        side_pack = _pack_side(arranged, cells, style)
        areas = [c[2] * c[3] for c in cells]
        max_idx = areas.index(max(areas)) if areas else None
        out.append({
            **side_pack,  # assets, cells, dpi, weight, layout_style
            "recipe": recipe,
            "coverage": round(used_area / page_area, 4),
            "max_cell_asset": (
                side_pack["assets"][max_idx] if max_idx is not None else None
            ),
            "feature_area": {
                p["asset_id"]: round(math.prod(contained_print_size(p, c)), 3)
                for p, c in zip(arranged, cells)
            },
        })
    out.sort(key=lambda entry: entry["coverage"], reverse=True)
    return out


# --- thumbnails --------------------------------------------------------------
def _thumb_path(state: ServerState, asset_id: str) -> Path | None:
    if not _ASSET_RE.match(asset_id):
        return None
    # Wedding proofs carry watermark-free crops (bookrender.CROPS, already used by
    # the exporter + contact sheet). Honor them here so the studio signature never
    # shows in the editor tray/canvas either. A distinct cache name guarantees a
    # warm cache of the old watermarked thumb can never mask the crop.
    crop = bookrender.CROPS.get(asset_id)
    if crop is not None:
        src = Path(crop)
        cache_name = f"{asset_id}.crop.jpg"
    else:
        derivative = state.derivative_by_id.get(asset_id)
        if not derivative:
            return None
        src = Path(derivative)
        cache_name = f"{asset_id}.jpg"
    if not src.is_absolute():
        src = ROOT / src
    if not src.is_file():
        return None
    cache = state.thumb_cache_dir / cache_name
    if cache.is_file() and cache.stat().st_mtime >= src.stat().st_mtime:
        return cache
    try:
        with Image.open(src) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.LANCZOS)
        image.save(cache, format="JPEG", quality=82, optimize=True)
    except Exception:
        return None
    return cache


# --- HTTP handler ------------------------------------------------------------
class EditorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # quiet by default
        return

    # -- response helpers --
    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, content_type: str, body: bytes,
                    cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # -- routing --
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/book":
                self._send_json(200, self.state.book_payload())
            elif path == "/api/photos":
                self._send_json(200, self.state.photos_payload())
            elif path == "/api/constants":
                self._send_json(200, self._constants())
            elif path.startswith("/api/thumb/"):
                self._serve_thumb(unquote(path[len("/api/thumb/"):]))
            elif path.startswith("/api/"):
                self._send_json(404, {"error": "unknown endpoint"})
            elif path == "/favicon.ico":
                # No favicon; 204 avoids a browser console 404 error.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # never leak a stack to the browser console
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/book":
                self._save_book()
            elif path == "/api/relayout":
                self._relayout()
            elif path == "/api/layouts":
                self._layouts()
            elif path == "/api/export":
                self._export()
            elif path == "/api/favorite":
                self._favorite()
            elif path == "/api/hero":
                self._hero()
            elif path == "/api/reject":
                self._reject()
            else:
                self._send_json(404, {"error": "unknown endpoint"})
        except BrokenPipeError:
            pass
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    # -- endpoint impls --
    def _constants(self) -> dict:
        return {
            "geometry": geometry_meta(),
            "dpi_ok": DPI_OK,
            "dpi_min": DPI_MIN,
            "off_white": OFF_WHITE,
            "thumb_max": THUMB_MAX,
        }

    def _save_book(self) -> None:
        payload = self._read_json()
        if "book" not in payload or "base_revision" not in payload:
            self._send_json(400, {"error": "expected {base_revision, book}"})
            return
        code, result = self.state.save_book(payload["base_revision"], payload["book"])
        self._send_json(code, result)

    def _favorite(self) -> None:
        payload = self._read_json()
        aid = payload.get("asset_id")
        fav = 1 if payload.get("favorited") else 0
        if not aid or aid not in self.state.photo_by_id:
            self._send_json(400, {"error": "unknown asset"})
            return
        with db.session(self.state.db_path) as conn:
            conn.execute("UPDATE assets SET favorited=? WHERE asset_id=?", (fav, aid))
        self.state.photo_by_id[aid]["favorited"] = bool(fav)  # keep in-memory in sync
        self._send_json(200, {"ok": True, "asset_id": aid, "favorited": bool(fav)})

    def _hero(self) -> None:
        """Set/clear is_hero on the manifest. Heroes are prominent regardless of
        resolution: the composer/repack gives them their own page (or feature
        cell) and smart_fill waives the DPI shrink for them. The client pairs
        this with a move-to-own-page action ("Promote to hero")."""
        payload = self._read_json()
        aid = payload.get("asset_id")
        val = 1 if payload.get("is_hero") else 0
        if not aid or aid not in self.state.photo_by_id:
            self._send_json(400, {"error": "unknown asset"})
            return
        with db.session(self.state.db_path) as conn:
            conn.execute("UPDATE assets SET is_hero=? WHERE asset_id=?", (val, aid))
        self.state.photo_by_id[aid]["is_hero"] = bool(val)  # keep in-memory in sync
        self._send_json(200, {"ok": True, "asset_id": aid, "is_hero": bool(val)})

    def _reject(self) -> None:
        """Remove from entire book / don't show again: flip review_status off
        'accept' so the composer's candidate query (WHERE review_status='accept')
        never re-selects this photo on any future recompose. The client removes
        the photo's cell from the page(s); this makes the removal PERMANENT."""
        payload = self._read_json()
        aid = payload.get("asset_id")
        if not aid or aid not in self.state.photo_by_id:
            self._send_json(400, {"error": "unknown asset"})
            return
        with db.session(self.state.db_path) as conn:
            conn.execute(
                "UPDATE assets SET review_status='reject' WHERE asset_id=?", (aid,))
        self.state.photo_by_id[aid]["review_status"] = "reject"  # in-memory sync
        # Drop it from the in-memory tray pool too — otherwise a browser reload
        # (which re-fetches /api/photos from this list) resurrects the photo in
        # the tray until the server restarts.
        self.state.photos = [p for p in self.state.photos if p["asset_id"] != aid]
        self._send_json(200, {"ok": True, "asset_id": aid, "review_status": "reject"})

    def _relayout(self) -> None:
        payload = self._read_json()
        left_ids = list(payload.get("left_assets", []))
        right_ids = list(payload.get("right_assets", []))
        missing = [a for a in (left_ids + right_ids) if a not in self.state.photo_by_id]
        if missing:
            self._send_json(400, {"error": "unknown asset(s)", "assets": missing[:5]})
            return
        result = relayout_pages(
            left_ids, right_ids,
            left_title=bool(payload.get("left_title")),
            right_title=bool(payload.get("right_title")),
            photo_by_id=self.state.photo_by_id,
        )
        self._send_json(200, result)

    def _layouts(self) -> None:
        """Return the composer's alternate layouts for one page (A2/A3)."""
        payload = self._read_json()
        side = payload.get("side")
        if side not in ("left", "right"):
            self._send_json(400, {"error": "side must be 'left' or 'right'"})
            return
        try:
            spread_index = int(payload.get("spread_index"))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "spread_index must be an integer"})
            return
        with self.state.lock:
            spreads = self.state.book.get("spreads", [])
            if not 0 <= spread_index < len(spreads):
                self._send_json(400, {"error": "spread_index out of range"})
                return
            page = spreads[spread_index].get(side, {})
            ids = [a for a in (cell_asset(c) for c in page.get("cells", [])) if a]
            template = self.state.book.get("templates", {}).get(page.get("template"), {})
            title = bool(template.get("title") and page.get("title"))
        if not ids:
            self._send_json(200, {"layouts": []})
            return
        missing = [a for a in ids if a not in self.state.photo_by_id]
        if missing:
            self._send_json(400, {"error": "unknown asset(s)", "assets": missing[:5]})
            return
        layouts = layout_candidates(
            ids, title=title, side=side, photo_by_id=self.state.photo_by_id,
        )
        self._send_json(200, {"layouts": layouts})

    def _export(self) -> None:
        payload = self._read_json()
        spread_indices = payload.get("spread_indices")
        from . import export_book as exporter

        try:
            report = exporter.export_book(
                book_path=self.state.book_path,
                db_path=self.state.db_path,
                spread_indices=spread_indices,
            )
            self._send_json(200, report)
        except ValueError as exc:
            # print gate — surface the low-DPI list to the UI
            with self.state.lock:
                _, low_dpi = derive_print_ready(self.state.book, self.state.photo_by_id)
            self._send_json(409, {
                "error": "not_print_ready",
                "message": str(exc),
                "low_dpi_cells": low_dpi,
            })
        except FileNotFoundError as exc:
            self._send_json(500, {"error": str(exc)})

    def _serve_thumb(self, asset_id: str) -> None:
        cache = _thumb_path(self.state, asset_id)
        if not cache:
            self._send_json(404, {"error": "unknown asset"})
            return
        self._send_bytes(200, "image/jpeg", cache.read_bytes(),
                         cache="no-cache, must-revalidate")

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (EDITOR_DIR / rel).resolve()
        # Traversal guard: resolved path must stay inside EDITOR_DIR.
        if EDITOR_DIR.resolve() not in target.parents and target != EDITOR_DIR.resolve():
            self._send_json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        ext = target.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        self._send_bytes(200, content_type, target.read_bytes(), cache="no-store")


# --- server factory ----------------------------------------------------------
def make_server(book_path: Path = DEFAULT_BOOK, db_path: Path | None = None,
                host: str = "127.0.0.1", port: int = 0,
                thumb_cache_dir: Path | None = None) -> tuple[ThreadingHTTPServer, ServerState]:
    from . import paths

    cache_dir = thumb_cache_dir or (paths.LOGS_DIR / "editor_thumb_cache")
    state = ServerState(book_path=book_path, db_path=db_path, thumb_cache_dir=cache_dir)
    httpd = ThreadingHTTPServer((host, port), EditorHandler)
    httpd.state = state  # type: ignore[attr-defined]
    return httpd, state


def serve(book_path: Path = DEFAULT_BOOK, db_path: Path | None = None,
          host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd, _ = make_server(book_path=book_path, db_path=db_path, host=host, port=port)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"[photobook] editor serving at {url}")
    print("[photobook] the tray shows all accepted photos; edits autosave to book.json.")
    print("[photobook] press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[photobook] editor stopped.")
    finally:
        httpd.server_close()
