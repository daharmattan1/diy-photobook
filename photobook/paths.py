"""Canonical repo paths — one source of truth for where everything lives.

Every module imports these so the layout is defined once. The repo root is
resolved relative to this file, so the pipeline works regardless of the
current working directory.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# photobook/paths.py -> photobook/ -> repo root
REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    """Where the BULK data lives (staging originals/derivatives, manifest, export).

    Defaults to the repo root, so out-of-the-box everything is self-contained.
    But the full-res originals for 9 years of photos can be ~140 GB — far more
    than a laptop's C: drive may have free. Set PHOTOBOOK_DATA_ROOT to an
    external drive (e.g. E:/photobook-data) and ALL the heavy directories move
    there, while code + config + the tiny review HTML stay in the repo on C:.

    If you unzip a Google Takeout export as an ingest source, give that scratch
    space its own home on the external drive too.
    """
    env = os.environ.get("PHOTOBOOK_DATA_ROOT")
    if env:
        return Path(env)
    return REPO_ROOT


DATA_ROOT: Path = _data_root()

# --- Small, repo-resident (version-controlled or trivially small) ---
CONFIG_DIR: Path = REPO_ROOT / "config"
LOGS_DIR: Path = REPO_ROOT / "logs"
REVIEW_DIR: Path = REPO_ROOT / "review"          # tiny HTML you open in a browser

# --- Bulk, redirectable to an external drive via PHOTOBOOK_DATA_ROOT ---
STAGING_DIR: Path = DATA_ROOT / "staging"
ORIGINALS_DIR: Path = STAGING_DIR / "originals"   # the ~140 GB of full-res copies
DERIVATIVES_DIR: Path = STAGING_DIR / "derivatives"
EXPORT_DIR: Path = DATA_ROOT / "export"           # the final ~200-photo bundle
MANIFEST_DB: Path = DATA_ROOT / "manifest.db"

# Config files
CHAPTERS_YAML: Path = CONFIG_DIR / "chapters.yaml"
SOURCES_YAML: Path = CONFIG_DIR / "sources.yaml"
SCORING_YAML: Path = CONFIG_DIR / "scoring.yaml"

def read_config(path: Path) -> str:
    """Read a required config file, with a clear remedy instead of a traceback.

    Every config ships as ``config/<name>.example.yaml``; the pipeline reads the
    non-example name. A brand-new checkout has only the examples, so the very
    first real-photo command would otherwise die with a raw FileNotFoundError.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(
            f"[photobook] missing config: {path}\n"
            f"  Copy config/{path.stem}.example.yaml to config/{path.name} "
            f"and edit it for your book (see README 'Docs' section)."
        ) from None


# Log files (created on demand by their stages)
DOCTOR_LOG: Path = LOGS_DIR / "doctor.txt"
FUNNEL_LOG: Path = LOGS_DIR / "funnel.txt"
DATE_AUDIT_LOG: Path = LOGS_DIR / "date_audit.txt"
EXPORT_VERIFY_LOG: Path = LOGS_DIR / "export_verify.txt"


def resolve_exiftool() -> Optional[str]:
    """Locate the exiftool executable robustly.

    exiftool is a standalone binary, not a pip package. It is normally on
    PATH, but on Windows the winget/Oliver-Betz installer drops it in
    %LOCALAPPDATA%\\Programs\\ExifTool\\ExifTool.exe and only updates the
    *persistent* PATH — a shell started before the install won't see it.
    So we check PATH first, then a list of known install locations.

    Returns the absolute path to the executable, or None if not found.
    The env var PHOTOBOOK_EXIFTOOL overrides everything (explicit escape hatch).
    """
    override = os.environ.get("PHOTOBOOK_EXIFTOOL")
    if override and Path(override).is_file():
        return override

    # 1. On PATH (case-insensitive, PATHEXT-aware via shutil.which on Windows).
    for name in ("exiftool", "exiftool.exe", "ExifTool.exe"):
        found = shutil.which(name)
        if found:
            return found

    # 2. Known fixed install locations (Windows-first, then POSIX).
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local) / "Programs" / "ExifTool" / "ExifTool.exe" if local else None,
        Path("C:/Program Files/ExifTool/exiftool.exe"),
        Path("C:/Program Files/ExifTool/ExifTool.exe"),
        Path("C:/Windows/exiftool.exe"),
        Path("/usr/local/bin/exiftool"),
        Path("/usr/bin/exiftool"),
        Path("/opt/homebrew/bin/exiftool"),
    ]
    for c in candidates:
        if c is not None and c.is_file():
            return str(c)

    return None


def ensure_dirs() -> None:
    """Create the working directories if they do not yet exist.

    Safe to call repeatedly. Does NOT create the DB (that is db.py's job).
    """
    for d in (
        LOGS_DIR,
        STAGING_DIR,
        ORIGINALS_DIR,
        DERIVATIVES_DIR,
        REVIEW_DIR,
        EXPORT_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
