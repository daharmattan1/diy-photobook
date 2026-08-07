"""Chaptering — the chronological spine.

Two operations:
  - validate: assert config/chapters.yaml is contiguous and non-overlapping —
    each chapter's start == prior chapter's end + 1 day, no gaps, no overlaps.
    Exits non-zero on any violation (used as a gate).
  - assign:   bisect each asset's best_datetime into exactly one chapter.

Assets whose date falls before the first chapter's start or after the last
chapter's end are clamped to the nearest edge chapter (with a warning count) so
nothing is left unchaptered — a stray date must not silently drop a photo.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from . import db, funnel, paths


def _load_chapters() -> list[dict]:
    cfg = yaml.safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    chapters = cfg.get("chapters", []) or []
    # Normalize dates to date objects, sorted by start.
    out = []
    for ch in chapters:
        out.append({
            "id": ch["id"],
            "title": ch.get("title", ch["id"]),
            "start": _as_date(ch["start"]),
            "end": _as_date(ch["end"]),
        })
    out.sort(key=lambda c: c["start"])
    return out


def _as_date(v) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    return datetime.strptime(str(v), "%Y-%m-%d").date()


def validate(db_path: Optional[Path] = None) -> tuple[bool, list[str]]:
    """Return (ok, problems). ok=True iff chapters are contiguous & non-overlapping."""
    chapters = _load_chapters()
    problems: list[str] = []
    if not chapters:
        return False, ["no chapters defined in config/chapters.yaml"]

    for ch in chapters:
        if ch["end"] < ch["start"]:
            problems.append(f"{ch['id']}: end {ch['end']} before start {ch['start']}")

    for prev, cur in zip(chapters, chapters[1:]):
        expected = prev["end"] + timedelta(days=1)
        if cur["start"] != expected:
            problems.append(
                f"{cur['id']}: start {cur['start']} != {prev['id']}.end+1day ({expected}) "
                f"— {'overlap' if cur['start'] <= prev['end'] else 'gap'}"
            )
    return (len(problems) == 0), problems


def assign(db_path: Optional[Path] = None) -> dict:
    chapters = _load_chapters()
    ok, problems = validate(db_path)
    if not ok:
        raise ValueError("chapters not contiguous; run validate: " + "; ".join(problems))

    starts = [c["start"] for c in chapters]
    first_start = chapters[0]["start"]
    last_end = chapters[-1]["end"]

    assigned = 0
    clamped_low = clamped_high = 0
    per_chapter: dict[str, int] = {c["id"]: 0 for c in chapters}

    with db.session(db_path) as conn:
        rows = conn.execute("SELECT asset_id, best_datetime FROM assets").fetchall()
        for r in rows:
            try:
                d = datetime.fromisoformat(
                    r["best_datetime"].replace("Z", "+00:00")
                ).date()
            except (AttributeError, ValueError):
                d = last_end  # undated → last chapter (should not happen post-Phase-4)

            if d < first_start:
                ch = chapters[0]; clamped_low += 1
            elif d > last_end:
                ch = chapters[-1]; clamped_high += 1
            else:
                # bisect_right(starts, d) - 1 → the chapter whose start <= d
                idx = bisect.bisect_right(starts, d) - 1
                idx = max(0, min(idx, len(chapters) - 1))
                ch = chapters[idx]

            conn.execute("UPDATE assets SET chapter=? WHERE asset_id=?", (ch["id"], r["asset_id"]))
            per_chapter[ch["id"]] += 1
            assigned += 1

    funnel.append(
        f"[chapter] assigned={assigned} across {len(chapters)} chapters "
        f"(clamped_low={clamped_low} clamped_high={clamped_high})"
    )
    return {
        "assigned": assigned,
        "chapters": len(chapters),
        "per_chapter": per_chapter,
        "clamped_low": clamped_low,
        "clamped_high": clamped_high,
    }
