"""Import the owner's downloaded decisions.json into the manifest (Phase 9).

The contact sheet downloads one JSON per chapter:
    { "chapter": "...", "decisions": { asset_id: "accept"|"reject"|"hero", ... } }

A browser cannot write into the repo, so decisions arrive as downloaded files
whose paths the owner supplies. This command imports one or many such files:

    photobook decisions --import decisions_04_the_house.json
    photobook decisions --import downloads/           # a directory of them

Semantics:
  - "hero"   -> review_status='accept', is_hero=1  (a hero is also accepted)
  - "accept" -> review_status='accept', is_hero=0
  - "reject" -> review_status='reject'
Optional per-hero captions may ride along in a companion "captions" map.

The import is idempotent (re-importing the same file yields no net change) and,
by default, REJECTS the whole import (non-zero exit) if the resulting accepted
count falls outside [min_accept, max_accept] — a guardrail so the book stays in
the 150–250 range. `--no-bound` disables the check (used for testing on small
sets).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from . import db, paths

VALID = {"accept", "reject", "hero"}


class BoundError(Exception):
    pass


def _iter_decision_files(path: Path) -> Iterable[Path]:
    if path.is_dir():
        yield from sorted(path.glob("*.json"))
    else:
        yield path


def _load_one(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    decisions = data.get("decisions", {}) or {}
    captions = data.get("captions", {}) or {}
    clean: dict[str, str] = {}
    for aid, verdict in decisions.items():
        v = str(verdict).lower()
        if v in VALID:
            clean[aid] = v
    return clean, captions


def import_decisions(
    import_path: Path,
    db_path: Optional[Path] = None,
    min_accept: int = 150,
    max_accept: int = 250,
    enforce_bound: bool = True,
) -> dict:
    files = list(_iter_decision_files(import_path))
    if not files:
        raise FileNotFoundError(f"no decisions JSON found at {import_path}")

    merged: dict[str, str] = {}
    captions: dict[str, str] = {}
    for f in files:
        d, c = _load_one(f)
        merged.update(d)
        captions.update(c)

    with db.session(db_path) as conn:
        # Validate asset_ids exist; collect unknowns rather than silently dropping.
        known = {r["asset_id"] for r in conn.execute("SELECT asset_id FROM assets").fetchall()}
        unknown = [aid for aid in merged if aid not in known]

        applied = 0
        for aid, verdict in merged.items():
            if aid not in known:
                continue
            if verdict == "hero":
                conn.execute(
                    "UPDATE assets SET review_status='accept', is_hero=1 WHERE asset_id=?",
                    (aid,),
                )
            elif verdict == "accept":
                conn.execute(
                    "UPDATE assets SET review_status='accept', is_hero=0 WHERE asset_id=?",
                    (aid,),
                )
            else:  # reject
                conn.execute(
                    "UPDATE assets SET review_status='reject' WHERE asset_id=?",
                    (aid,),
                )
            if aid in captions:
                conn.execute(
                    "UPDATE assets SET caption=? WHERE asset_id=?", (captions[aid], aid)
                )
            applied += 1

        accept_count = conn.execute(
            "SELECT count(*) n FROM assets WHERE review_status='accept'"
        ).fetchone()["n"]
        hero_count = conn.execute(
            "SELECT count(*) n FROM assets WHERE is_hero=1"
        ).fetchone()["n"]

        if enforce_bound and not (min_accept <= accept_count <= max_accept):
            # Roll back by raising AFTER the session context — but we already
            # wrote. Undo by raising and letting caller decide; simplest robust
            # approach: raise here; the session commits on exit, so instead we
            # explicitly rollback before raising.
            conn.rollback()
            raise BoundError(
                f"accepted count {accept_count} outside [{min_accept},{max_accept}] "
                f"— import rejected (use --no-bound to override on small/test sets)"
            )

    return {
        "files": len(files),
        "applied": applied,
        "accept_count": accept_count,
        "hero_count": hero_count,
        "unknown_ids": len(unknown),
    }
