"""logs/funnel.txt accounting — the running record of how the pile shrinks.

Each stage appends a line so the final funnel reads like:

    [ingest]  thumbdrive  scanned=812 ingested=812 ...
    [ingest]  whatsapp    scanned=2201 ingested=2190 ...
    [dedup]   ingested=5104 -> deduped_keep=3388 (removed 1716 dup-flagged)
    [quality] quality_pass=3120 / 3388
    [score]   shortlist=?? across N chapters

It is append-only and human-readable; the manifest holds the authoritative
numbers. `funnel.txt` is gitignored (regenerable).
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import paths


def append(line: str) -> None:
    """Append one timestamped line to logs/funnel.txt."""
    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with paths.FUNNEL_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{stamp}  {line}\n")


def read() -> str:
    if paths.FUNNEL_LOG.is_file():
        return paths.FUNNEL_LOG.read_text(encoding="utf-8")
    return ""
