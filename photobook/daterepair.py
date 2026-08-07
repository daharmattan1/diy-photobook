"""Timestamp repair — resolve best_datetime per asset. MANIFEST-ONLY.

Resolution order (first hit wins), all READ-ONLY on the originals:
    1. EXIF DateTimeOriginal   (read via exiftool)     -> confidence high
    2. sidecar_taken_time      (captured at ingest)    -> confidence high
    3. filename regexes        (config/sources.yaml)   -> confidence medium
    4. file mtime of original  (last resort)           -> confidence low

The result is written ONLY into the manifest (best_datetime / date_source /
date_confidence). We NEVER invoke an exiftool WRITE against source or staged
originals — a cross-model-review finding: writing EXIF back into 9 years of
irreplaceable files is unnecessary risk. Export naming (Phase 10) carries the
chronological order; if a print service wants embedded dates, EXIF is written
onto the EXPORTED COPIES there, never here.

exiftool is resolved via paths.resolve_exiftool() (PATH may be stale on
Windows after a winget install).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from . import db, paths

# --- exiftool batch read ----------------------------------------------------


def _exif_datetimes_batch(exe: str, files: list[Path]) -> dict[str, Optional[str]]:
    """Read DateTimeOriginal (fallback CreateDate) for many files at once.

    Returns {abs_path: iso_or_None}. exiftool -j returns a JSON array; -d
    normalizes the date format so we can parse it uniformly.
    """
    if not files:
        return {}
    cmd = [
        exe, "-j", "-n",
        "-d", "%Y-%m-%dT%H:%M:%S",
        "-DateTimeOriginal", "-CreateDate", "-SourceFile",
        *[str(f) for f in files],
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        data = json.loads(out.stdout or "[]")
    except Exception:
        return {str(f): None for f in files}

    result: dict[str, Optional[str]] = {}
    for entry in data:
        src = entry.get("SourceFile")
        if not src:
            continue
        dt = entry.get("DateTimeOriginal") or entry.get("CreateDate")
        # exiftool sometimes returns "0000:00:00 00:00:00" style junk → guard.
        if isinstance(dt, str) and dt.startswith("0000"):
            dt = None
        result[str(Path(src))] = dt if isinstance(dt, str) and dt else None
    # Ensure every requested file has a key.
    for f in files:
        result.setdefault(str(f), None)
    return result


# --- filename patterns ------------------------------------------------------


def _load_filename_patterns() -> list[dict]:
    cfg = yaml.safe_load(paths.read_config(paths.SOURCES_YAML)) or {}
    return cfg.get("filename_date_patterns", []) or []


def _date_from_filename(name: str, patterns: list[dict]) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat["pattern"], name)
        if not m:
            continue
        groups = pat.get("groups", [])
        try:
            gd = {g: m.group(i + 1) for i, g in enumerate(groups)}
            y = int(gd["year"]); mo = int(gd["month"]); d = int(gd["day"])
            if not (1990 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            return datetime(y, mo, d, 12, 0, 0).isoformat()  # noon = date-only sentinel
        except (KeyError, ValueError):
            continue
    return None


def _mtime_iso(path: Path) -> Optional[str]:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts).replace(microsecond=0).isoformat()
    except OSError:
        return None


# --- driver -----------------------------------------------------------------


def repair(db_path: Optional[Path] = None, exiftool_batch: int = 200) -> dict:
    """Resolve best_datetime for every asset. Returns a summary dict."""
    exe = paths.resolve_exiftool()
    patterns = _load_filename_patterns()

    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, orig_path, staged_original_path, sidecar_taken_time "
            "FROM assets"
        ).fetchall()

        # Batch EXIF read against the STAGED ORIGINALS (read-only).
        exif_map: dict[str, Optional[str]] = {}
        if exe:
            staged = [Path(r["staged_original_path"]) for r in rows if r["staged_original_path"]]
            for i in range(0, len(staged), exiftool_batch):
                exif_map.update(_exif_datetimes_batch(exe, staged[i : i + exiftool_batch]))

        source_counter: Counter[str] = Counter()
        conf_counter: Counter[str] = Counter()
        low_conf: list[str] = []

        for r in rows:
            best_dt: Optional[str] = None
            source: Optional[str] = None
            confidence: Optional[str] = None

            staged = r["staged_original_path"]
            exif_dt = exif_map.get(str(Path(staged))) if staged else None

            if exif_dt:
                best_dt, source, confidence = exif_dt, "exif", "high"
            elif r["sidecar_taken_time"]:
                best_dt, source, confidence = r["sidecar_taken_time"], "sidecar", "high"
            else:
                fn = Path(r["orig_path"]).name if r["orig_path"] else ""
                fdt = _date_from_filename(fn, patterns)
                if fdt:
                    best_dt, source, confidence = fdt, "filename", "medium"
                else:
                    mdt = _mtime_iso(Path(staged)) if staged else None
                    if mdt:
                        best_dt, source, confidence = mdt, "mtime", "low"

            if best_dt is None:
                # Absolute last resort: epoch, flagged low, so no NULLs remain.
                best_dt = datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()
                source, confidence = "mtime", "low"

            conn.execute(
                "UPDATE assets SET best_datetime=?, date_source=?, date_confidence=? "
                "WHERE asset_id=?",
                (best_dt, source, confidence, r["asset_id"]),
            )
            source_counter[source] += 1
            conf_counter[confidence] += 1
            if confidence == "low":
                low_conf.append(f"{Path(r['orig_path']).name if r['orig_path'] else r['asset_id']}")

    _write_audit(source_counter, conf_counter, low_conf)
    return {
        "by_source": dict(source_counter),
        "by_confidence": dict(conf_counter),
        "low_confidence_count": len(low_conf),
        "exiftool_used": bool(exe),
    }


def _write_audit(source_counter: Counter, conf_counter: Counter, low_conf: list[str]) -> None:
    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["date repair audit", "=" * 40, "", "by date_source:"]
    for k, v in source_counter.most_common():
        lines.append(f"  {k:10} {v}")
    lines.append("")
    lines.append("by confidence:")
    for k in ("high", "medium", "low"):
        lines.append(f"  {k:10} {conf_counter.get(k, 0)}")
    lines.append("")
    lines.append(f"low-confidence assets: {len(low_conf)}")
    for name in low_conf[:50]:
        lines.append(f"  - {name}")
    if len(low_conf) > 50:
        lines.append(f"  ... and {len(low_conf) - 50} more")
    paths.DATE_AUDIT_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
