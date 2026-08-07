"""Source-agnostic ingest — copy originals once, build derivatives, capture dates.

For every file under a source directory:
  1. Compute `asset_id = sha1(bytes)`.
  2. Copy the original ONCE to `staging/originals/<aa>/<asset_id>.<ext>`.
  3. Render a <=2560px sRGB JPEG derivative to
     `staging/derivatives/<aa>/<asset_id>.jpg`.
  4. Look for a Google Takeout JSON sidecar for the ORIGINAL filename and, if
     found, capture `photoTakenTime.timestamp` into `sidecar_taken_time`.
  5. Upsert one manifest row (idempotent — re-running adds nothing).

Decode paths: HEIC/HEIF via pillow-heif; RAW via rawpy; everything else via
Pillow directly. Non-image files (and anything that fails to decode) are
skipped with a logged reason — the pile is photos, not a filesystem mirror.

NOTHING here mutates the source files. The original is read (for bytes + decode)
and copied; never written.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

from . import db, paths

# --- Format knowledge -------------------------------------------------------

HEIC_EXTS = {".heic", ".heif", ".hif"}
RAW_EXTS = {
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf",
    ".orf", ".rw2", ".pef", ".srw", ".raw",
}
PIL_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
# Video is out of scope for the book (stills only) but we note them in the log.
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".webm"}

IMAGE_EXTS = HEIC_EXTS | RAW_EXTS | PIL_EXTS

DERIVATIVE_MAX_EDGE = 2560
DERIVATIVE_QUALITY = 90

_pillow_heif_registered = False


def _ensure_heif() -> None:
    global _pillow_heif_registered
    if not _pillow_heif_registered:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _pillow_heif_registered = True


# --- Hashing ----------------------------------------------------------------

def sha1_of_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# --- Decode to a PIL image (for derivative + dimensions) --------------------

def _open_image(path: Path) -> Image.Image:
    """Return an RGB PIL image for any supported format. Raises on failure."""
    ext = path.suffix.lower()
    if ext in RAW_EXTS:
        import numpy as np
        import rawpy

        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess()  # numpy HxWx3 uint8
        return Image.fromarray(np.asarray(rgb, dtype="uint8"), mode="RGB")

    if ext in HEIC_EXTS:
        _ensure_heif()

    img = Image.open(path)
    # Respect EXIF orientation so derivatives are upright.
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def _write_derivative(img: Image.Image, dest: Path) -> tuple[int, int]:
    """Write a <=2560px sRGB JPEG. Returns the ORIGINAL (pre-resize) dimensions."""
    orig_w, orig_h = img.size
    work = img.copy()
    work.thumbnail((DERIVATIVE_MAX_EDGE, DERIVATIVE_MAX_EDGE), Image.LANCZOS)
    if work.mode != "RGB":
        work = work.convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    work.save(dest, format="JPEG", quality=DERIVATIVE_QUALITY, optimize=True)
    return orig_w, orig_h


# --- Takeout sidecar capture ------------------------------------------------

# Google Takeout has used several sidecar-suffix conventions over the years.
# The 2024+ exports name the sidecar "<image-name>.supplemental-metadata.json";
# older ones used a bare ".json". Google also ABBREVIATES the supplemental token
# (…-metadata → …-metadat, …-meta, …-me, …) to keep the whole sidecar filename
# under a length cap, so we try the abbreviations too.
_SIDECAR_TOKENS = (
    ".supplemental-metadata",
    ".supplemental-metadat",
    ".supplemental-metada",
    ".supplemental-metad",
    ".supplemental-meta",
    ".supplemental-met",
    ".supplemental-me",
    ".supplemental-m",
    ".supplemental-",
    ".supplemental",
    ".suppl",
    "",  # bare ".json" (oldest convention)
)


def find_sidecar(original: Path) -> Optional[Path]:
    """Find a Google Takeout JSON sidecar for `original`, handling the messy
    real-world naming Takeout produces across export vintages:

        image.jpg     -> image.jpg.supplemental-metadata.json   (2024+ default)
        image.jpg     -> image.jpg.supplemental-metad….json     (abbreviated)
        image.jpg     -> image.jpg.json                          (older)
        image.jpg     -> image.json                              (oldest)
        image(1).jpg  -> image.jpg(1).supplemental-metadata.json (dedup quirk)
        image(1).jpg  -> image(1).jpg.supplemental-metadata.json

    Returns the first existing candidate, or None.
    """
    d = original.parent
    name = original.name          # e.g. "image(1).jpg"
    stem = original.stem          # e.g. "image(1)"
    ext = original.suffix         # e.g. ".jpg"

    # Base name-stems the sidecar might be keyed on, most-specific first.
    bases: list[str] = [name, stem]

    # Takeout's "(N)" dedup quirk: for image(1).jpg the sidecar is sometimes keyed
    # to image.jpg(N) (the "(N)" migrates to after the extension) or to image.
    if stem.endswith(")") and "(" in stem:
        root = stem[: stem.rindex("(")]        # "image"
        paren = stem[stem.rindex("("):]        # "(1)"
        bases.extend([f"{root}{ext}{paren}", f"{root}{paren}{ext}", root])

    candidates: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        for tok in _SIDECAR_TOKENS:
            fname = f"{base}{tok}.json"
            if fname not in seen:
                seen.add(fname)
                candidates.append(d / fname)

    for c in candidates:
        if c.is_file():
            return c
    return None


def _load_sidecar(sidecar: Path) -> Optional[dict]:
    """Parse a sidecar JSON file to a dict, or None if unreadable."""
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None


def _taken_time_from_data(data: dict) -> Optional[str]:
    """photoTakenTime.timestamp (unix seconds) → ISO 8601 UTC string."""
    node = data.get("photoTakenTime") or data.get("creationTime")
    if not isinstance(node, dict):
        return None
    ts = node.get("timestamp")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def parse_sidecar_taken_time(sidecar: Path) -> Optional[str]:
    """Extract photoTakenTime.timestamp (unix seconds) → ISO 8601 UTC string."""
    data = _load_sidecar(sidecar)
    return _taken_time_from_data(data) if data else None


def parse_sidecar_signals(data: dict) -> tuple[Optional[int], Optional[int]]:
    """Extract curation signals from a parsed sidecar dict.

    Returns (favorited, people_count):
      - favorited: 1 if Google Photos 'favorited' is truthy, else 0 (None if the
        key is absent — most sidecars omit it, and absent != not-favorited only
        in the sense that we simply have no positive signal).
      - people_count: number of face-tagged people (len of the 'people' array),
        or None if the key is absent. >=2 indicates a group photo.
    """
    favorited: Optional[int] = None
    if "favorited" in data:
        favorited = 1 if data.get("favorited") in (True, "true", 1, "1") else 0
    elif "favorite" in data:
        favorited = 1 if data.get("favorite") in (True, "true", 1, "1") else 0

    people_count: Optional[int] = None
    people = data.get("people")
    if isinstance(people, list):
        people_count = len(people)
    return favorited, people_count


# --- Ingest driver ----------------------------------------------------------

@dataclass
class IngestStats:
    source: str
    scanned: int = 0
    ingested: int = 0          # new rows added
    already: int = 0           # asset_id already present (idempotent skip)
    skipped_nonimage: int = 0
    videos: int = 0
    failed: int = 0
    with_sidecar: int = 0
    failures: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"source={self.source} scanned={self.scanned} "
            f"ingested={self.ingested} already={self.already} "
            f"sidecar={self.with_sidecar} videos={self.videos} "
            f"nonimage={self.skipped_nonimage} failed={self.failed}"
        )


def _shard_dir(base: Path, asset_id: str) -> Path:
    return base / asset_id[:2]


def ingest_dir(
    src: Path,
    source_name: str,
    db_path: Optional[Path] = None,
    lean: bool = False,
    zip_source: Optional[Path] = None,
    batch: Optional[str] = None,
) -> IngestStats:
    """Walk `src` recursively and ingest every supported image. Idempotent.

    lean=True: DO NOT keep the full-res original. Generate the 2560px derivative
      directly from the source file, record which ZIP part the original lives in
      (`zip_source` + the entry's relative name), and leave staged_original_path
      NULL. Used for a huge Takeout on a small disk: peak footprint is one
      unpacked part + the tiny derivatives; accepted originals are re-extracted
      from the ZIPs at export time.
    zip_source: when lean, the .zip file whose unpacked contents `src` holds, so
      export can find each original again.
    """
    paths.ensure_dirs()
    db.init_db(db_path)
    stats = IngestStats(source=source_name)

    with db.session(db_path) as conn:
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lower()

            # Skip the JSON sidecars themselves and other non-images.
            if ext == ".json":
                continue
            if ext in VIDEO_EXTS:
                stats.videos += 1
                continue
            if ext not in IMAGE_EXTS:
                stats.skipped_nonimage += 1
                continue

            stats.scanned += 1
            try:
                asset_id = sha1_of_file(path)
                deriv_dest = _shard_dir(paths.DERIVATIVES_DIR, asset_id) / f"{asset_id}.jpg"

                # Capture sidecar BEFORE we lose the original filename context.
                # Parse it once, then pull date + curation signals from the dict.
                sidecar = find_sidecar(path)
                sidecar_data = _load_sidecar(sidecar) if sidecar else None
                if sidecar_data is not None:
                    sidecar_time = _taken_time_from_data(sidecar_data)
                    favorited, people_count = parse_sidecar_signals(sidecar_data)
                else:
                    sidecar_time = None
                    favorited = people_count = None
                if sidecar_time:
                    stats.with_sidecar += 1

                if lean:
                    # No staged original. Derivative comes straight from the source
                    # file; record the ZIP part + entry so export can re-extract.
                    staged_str = None
                    zip_str = str(zip_source) if zip_source else None
                    zip_entry = _zip_entry_name(path, src)
                    if deriv_dest.exists():
                        orig_w, orig_h = _dims_from_source(path)
                    else:
                        img = _open_image(path)
                        orig_w, orig_h = _write_derivative(img, deriv_dest)
                        img.close()
                else:
                    # Default: copy original into staging (content-addressed shard).
                    orig_dest = _shard_dir(paths.ORIGINALS_DIR, asset_id) / f"{asset_id}{ext}"
                    staged_str = str(orig_dest)
                    zip_str = None
                    zip_entry = None
                    if not orig_dest.exists():
                        orig_dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, orig_dest)
                    if deriv_dest.exists():
                        orig_w, orig_h = _original_dimensions(orig_dest)
                    else:
                        img = _open_image(orig_dest)
                        orig_w, orig_h = _write_derivative(img, deriv_dest)
                        img.close()

                row = {
                    "asset_id": asset_id,
                    "src": source_name,
                    "orig_path": str(path),
                    "staged_original_path": staged_str,
                    "derivative_path": str(deriv_dest),
                    "zip_source": zip_str,
                    "zip_entry": zip_entry,
                    "sidecar_taken_time": sidecar_time,
                    "favorited": favorited,
                    "people_count": people_count,
                    "ingest_batch": batch,
                    "mime": Image.MIME.get(Image.registered_extensions().get(ext, ""), None),
                    "w": orig_w,
                    "h": orig_h,
                    "bytes": path.stat().st_size,
                }
                inserted = db.upsert_asset(conn, row)
                if inserted:
                    stats.ingested += 1
                else:
                    stats.already += 1

            except Exception as e:  # noqa: BLE001 - record and continue
                stats.failed += 1
                stats.failures.append(f"{path.name}: {type(e).__name__}: {e}")

    return stats


def _zip_entry_name(path: Path, unzip_root: Path) -> str:
    """The entry name inside the ZIP = the file's path relative to the unzip root."""
    try:
        return str(path.relative_to(unzip_root)).replace("\\", "/")
    except ValueError:
        return path.name


def _dims_from_source(path: Path) -> tuple[int, int]:
    """Original dimensions read directly from a source file (lean mode)."""
    ext = path.suffix.lower()
    if ext in RAW_EXTS:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            return raw.sizes.width, raw.sizes.height
    if ext in HEIC_EXTS:
        _ensure_heif()
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.size


def _original_dimensions(staged_original: Path) -> tuple[int, int]:
    """Dimensions of a staged ORIGINAL (used when its derivative already exists)."""
    ext = staged_original.suffix.lower()
    if ext in RAW_EXTS:
        import rawpy

        with rawpy.imread(str(staged_original)) as raw:
            sizes = raw.sizes
            return sizes.width, sizes.height
    if ext in HEIC_EXTS:
        _ensure_heif()
    with Image.open(staged_original) as im:
        im = ImageOps.exif_transpose(im)
        return im.size
