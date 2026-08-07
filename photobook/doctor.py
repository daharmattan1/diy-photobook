"""`photobook doctor` — the dependency + environment health gate.

Verifies every hard dependency the pipeline needs actually works on THIS
interpreter, before any real pipeline stage runs. Writes a green/red report
to logs/doctor.txt and exits non-zero if any MANDATORY check fails.

Mandatory checks (must be GREEN):
  - Pillow imports
  - HEIC decodes (pillow-heif)  — iPhone default; can't ingest without it
  - imagehash imports           — dedup depends on it
  - exiftool on PATH            — read-only date extraction
  - Playwright Chromium launches — contact sheets + proof PDF

Conditional check:
  - RAW decode (rawpy)          — GREEN if a RAW sample decodes, else
                                  SKIP-with-warning if no RAW present.
                                  "Mandatory-if-present": becomes RED only
                                  when RAW files exist but cannot decode.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths


@dataclass
class CheckResult:
    name: str
    status: str  # "GREEN" | "RED" | "SKIP"
    detail: str

    @property
    def is_fatal(self) -> bool:
        return self.status == "RED"


def _green(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "GREEN", detail)


def _red(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "RED", detail)


def _skip(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "SKIP", detail)


def check_pillow() -> CheckResult:
    try:
        import PIL
        from PIL import Image  # noqa: F401

        return _green("Pillow imports", f"PIL {getattr(PIL, '__version__', '?')}")
    except Exception as e:  # pragma: no cover - environment dependent
        return _red("Pillow imports", f"{type(e).__name__}: {e}")


def check_heic() -> CheckResult:
    """Register pillow-heif and decode a tiny synthetic HEIC round-trip."""
    try:
        from PIL import Image
        import pillow_heif

        pillow_heif.register_heif_opener()

        # Encode a 2x2 image to HEIC in-memory, then decode it back.
        buf = io.BytesIO()
        img = Image.new("RGB", (2, 2), (123, 45, 67))
        img.save(buf, format="HEIF")
        buf.seek(0)
        decoded = Image.open(buf)
        decoded.load()
        if decoded.size != (2, 2):
            return _red("HEIC decodes", f"unexpected size {decoded.size}")
        return _green(
            "HEIC decodes",
            f"pillow-heif {getattr(pillow_heif, '__version__', '?')} round-trip OK",
        )
    except Exception as e:  # pragma: no cover - environment dependent
        return _red("HEIC decodes", f"{type(e).__name__}: {e}")


def check_imagehash() -> CheckResult:
    try:
        import imagehash
        from PIL import Image

        h = imagehash.phash(Image.new("RGB", (16, 16), (10, 20, 30)))
        return _green(
            "imagehash imports",
            f"imagehash {getattr(imagehash, '__version__', '?')} phash={h}",
        )
    except Exception as e:  # pragma: no cover - environment dependent
        return _red("imagehash imports", f"{type(e).__name__}: {e}")


def check_exiftool() -> CheckResult:
    exe = paths.resolve_exiftool()
    if not exe:
        return _red(
            "exiftool available",
            "exiftool not found on PATH or known install dirs — install standalone "
            "exiftool(.exe), or set PHOTOBOOK_EXIFTOOL to its path",
        )
    try:
        out = subprocess.run(
            [exe, "-ver"], capture_output=True, text=True, timeout=30
        )
        ver = (out.stdout or "").strip()
        if out.returncode != 0 or not ver:
            return _red("exiftool available", f"`exiftool -ver` failed: {out.stderr!r}")
        on_path = shutil.which("exiftool") is not None
        note = "on PATH" if on_path else "resolved off-PATH (new shells will see it)"
        return _green("exiftool available", f"v{ver} ({note}) at {exe}")
    except Exception as e:  # pragma: no cover - environment dependent
        return _red("exiftool available", f"{type(e).__name__}: {e}")


def check_opencv() -> CheckResult:
    """Not in the mandatory SC list, but the quality gate needs it — report it."""
    try:
        import cv2

        return _green("opencv (headless)", f"cv2 {cv2.__version__}")
    except Exception as e:  # pragma: no cover - environment dependent
        return _red("opencv (headless)", f"{type(e).__name__}: {e}")


def check_chromium() -> CheckResult:
    """Launch headless Chromium via Playwright and open a blank page."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<html><body>doctor</body></html>")
            title_ok = page.evaluate("() => document.body.innerText") == "doctor"
            browser.close()
        if not title_ok:
            return _red("Playwright Chromium", "launched but page eval mismatch")
        return _green("Playwright Chromium", "headless launch + page eval OK")
    except Exception as e:  # pragma: no cover - environment dependent
        return _red(
            "Playwright Chromium",
            f"{type(e).__name__}: {e} (run `python -m playwright install chromium`)",
        )


def _find_raw_sample() -> Optional[Path]:
    """Look for any RAW file already ingested under staging/originals."""
    raw_exts = {
        ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf",
        ".orf", ".rw2", ".pef", ".srw", ".raw",
    }
    if not paths.ORIGINALS_DIR.exists():
        return None
    for f in paths.ORIGINALS_DIR.rglob("*"):
        if f.suffix.lower() in raw_exts:
            return f
    return None


def check_raw(sample: Optional[Path] = None) -> CheckResult:
    """RAW decode is mandatory-if-present.

    - If a RAW sample exists and rawpy decodes it -> GREEN.
    - If a RAW sample exists but rawpy fails      -> RED (fatal).
    - If no RAW sample is present                 -> SKIP-with-warning.
    """
    if sample is None:
        sample = _find_raw_sample()

    try:
        import rawpy  # noqa: F401
    except Exception as e:  # pragma: no cover - environment dependent
        # If RAW files exist we cannot decode them -> fatal; else just skip.
        if sample is not None:
            return _red("RAW decode (rawpy)", f"rawpy import failed but RAW present: {e}")
        return _skip(
            "RAW decode (rawpy)",
            f"rawpy import failed and no RAW files present ({type(e).__name__}) — "
            "OK for now; will be enforced if RAW is ingested",
        )

    if sample is None:
        return _skip(
            "RAW decode (rawpy)",
            "no RAW files present in any ingested source — mandatory-if-present, skipped",
        )

    try:
        import rawpy

        with rawpy.imread(str(sample)) as raw:
            rgb = raw.postprocess()
        return _green(
            "RAW decode (rawpy)",
            f"decoded {sample.name} -> {rgb.shape[1]}x{rgb.shape[0]}",
        )
    except Exception as e:
        return _red("RAW decode (rawpy)", f"failed to decode {sample.name}: {e}")


def check_data_root() -> CheckResult:
    """Report where BULK data lands + free space there (advisory, never fatal).

    The full-res originals for 9 years of photos can be ~140 GB. This surfaces
    which drive will hold them and how much room it has, so a too-small target
    is caught BEFORE a multi-hour ingest fills the disk.
    """
    import os as _os

    root = paths.DATA_ROOT
    redirected = bool(_os.environ.get("PHOTOBOOK_DATA_ROOT"))
    try:
        # disk_usage needs an existing dir; walk up to the first that exists.
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        total, used, free = shutil.disk_usage(probe)
        gb = 1024 ** 3
        free_gb = free / gb
        where = f"{root}" + ("  (PHOTOBOOK_DATA_ROOT)" if redirected else "  (repo default)")
        detail = f"{where} — {free_gb:.0f} GB free on {probe.anchor or probe}"
        # Advisory thresholds: a full Takeout can be ~140 GB. Warn under 160 GB
        # of headroom, but never fail (user may have scoped a smaller export).
        if free_gb < 60:
            return CheckResult("data root free space", "SKIP",
                               detail + "  [WARN: <60 GB — likely too small for a full Takeout; "
                               "set PHOTOBOOK_DATA_ROOT to an external drive]")
        if free_gb < 160:
            return CheckResult("data root free space", "SKIP",
                               detail + "  [note: <160 GB headroom; fine for a scoped export, "
                               "tight for a full ~140 GB Takeout]")
        return _green("data root free space", detail)
    except Exception as e:  # pragma: no cover
        return _skip("data root free space", f"could not stat {root}: {e}")


ALL_CHECKS = (
    check_pillow,
    check_heic,
    check_imagehash,
    check_exiftool,
    check_opencv,
    check_data_root,
    check_chromium,
    check_raw,
)


def run_doctor(write_log: bool = True) -> int:
    """Run every check, print a report, optionally write logs/doctor.txt.

    Returns process exit code: 0 if no fatal (RED) checks, else 1.
    """
    results: list[CheckResult] = [check() for check in ALL_CHECKS]

    icon = {"GREEN": "[OK] ", "RED": "[FAIL]", "SKIP": "[SKIP]"}
    lines: list[str] = []
    lines.append("photobook doctor — dependency + environment health")
    lines.append(f"interpreter: Python {sys.version.split()[0]} ({sys.executable})")
    lines.append("-" * 64)
    for r in results:
        lines.append(f"{icon[r.status]:7} {r.name:24} {r.detail}")
    lines.append("-" * 64)

    fatal = [r for r in results if r.is_fatal]
    skipped = [r for r in results if r.status == "SKIP"]
    if fatal:
        lines.append(f"RESULT: RED — {len(fatal)} mandatory check(s) failed.")
    else:
        note = f" ({len(skipped)} skipped)" if skipped else ""
        lines.append(f"RESULT: GREEN — all mandatory checks passed{note}.")

    report = "\n".join(lines)
    print(report)

    if write_log:
        paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        paths.DOCTOR_LOG.write_text(report + "\n", encoding="utf-8")
        print(f"\nwrote {paths.DOCTOR_LOG}")

    return 1 if fatal else 0
