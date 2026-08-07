"""Face detection helper for the smart-fill layout pass.

`detect_faces(derivative_path)` returns normalized face boxes ``(x0,y0,x1,y1)``
in ``0..1`` using OpenCV's Haar frontal-face cascade (cv2 4.x ships it; OpenCV
5.x removed it — pin ``opencv-python-headless<5``, see requirements.txt).

Results are cached to ``logs/face_cache.json`` keyed by ``asset_id`` (the
derivative filename stem is the asset_id), so re-runs of the fill pass do not
re-scan every derivative. The cache is loaded once per process and persisted on
``flush_cache()``.

Haar OVER-detects slightly. That is the safe direction here: a spurious face box
only makes a cover-crop more conservative (the crop must avoid it), it never
causes a real face to be cut. The caller treats every returned box as a no-cut
region.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2

from . import paths

# Lazy-initialized so importing this module never crashes on an OpenCV build
# without the Haar API (cv2 5.x); the failure surfaces at USE time with a clear
# remedy instead of an AttributeError at import.
_CASCADE = None


def _cascade():
    global _CASCADE
    if _CASCADE is None:
        try:
            haar_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            _CASCADE = cv2.CascadeClassifier(haar_path)
        except AttributeError as exc:
            raise RuntimeError(
                "This OpenCV build has no Haar CascadeClassifier (OpenCV 5.x "
                "removed it). Install opencv-python-headless<5 — see "
                "requirements.txt."
            ) from exc
    return _CASCADE

_CACHE_PATH = paths.LOGS_DIR / "face_cache.json"
_cache: dict[str, list[list[float]]] | None = None
_dirty = False


def _load_cache() -> dict[str, list[list[float]]]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def flush_cache() -> None:
    """Persist the in-memory face cache to logs/face_cache.json (if changed)."""
    global _dirty
    if _cache is None or not _dirty:
        return
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_cache, indent=0), encoding="utf-8")
    tmp.replace(_CACHE_PATH)
    _dirty = False


def _detect(derivative_path: str) -> list[list[float]]:
    """Run Haar detection on one derivative; normalized (x0,y0,x1,y1) boxes."""
    img = cv2.imread(str(derivative_path))
    if img is None:
        return []
    h, w = img.shape[:2]
    if w <= 0 or h <= 0:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # 1.1 / 5 mirror the POC; minSize scales with the derivative so a 2560px
    # image does not report speck-sized false faces.
    min_side = max(20, int(round(min(w, h) * 0.02)))
    faces = _cascade().detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_side, min_side)
    )
    return [
        [x / w, y / h, (x + fw) / w, (y + fh) / h]
        for (x, y, fw, fh) in faces
    ]


def detect_faces(derivative_path: str, asset_id: str | None = None) -> list[tuple[float, float, float, float]]:
    """Return cached-or-computed normalized face boxes for one derivative.

    ``asset_id`` defaults to the derivative filename stem (which IS the asset_id
    in this pipeline). Boxes are ``(x0,y0,x1,y1)`` in 0..1.
    """
    global _dirty
    aid = asset_id or Path(derivative_path).stem
    cache = _load_cache()
    if aid not in cache:
        cache[aid] = _detect(derivative_path)
        _dirty = True
    return [tuple(box) for box in cache[aid]]
