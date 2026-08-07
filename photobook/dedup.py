"""Deduplication — group near-duplicates, pick one keeper each. NON-destructive.

Exact-byte duplicates are already collapsed (same bytes -> same asset_id -> one
row). This stage catches NEAR duplicates (bursts, re-saves, cross-source
recompressions) via perceptual hashing.

Passes:
  1. Primary: bucket assets by day (from best_datetime) + adjacent day, then
     union-find any pair whose pHash Hamming distance <= threshold.
  2. Low-confidence widening: for assets whose date_confidence is 'low' (mtime-
     dated, metadata-broken), compare pHash within a bounded +/-30-day window so
     their dups still collapse even when the day-bucket is unreliable.

Keeper selection per group (first differentiator wins):
    source priority (config) -> resolution (w*h) -> bytes -> EXIF completeness
Exactly one asset per dup_group gets is_dup_keep=1; the rest get 0 (RETAINED,
never deleted).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from . import db, funnel, paths


# --- config -----------------------------------------------------------------


def _load_cfg() -> tuple[int, int, dict]:
    scoring = yaml.safe_load(paths.read_config(paths.SCORING_YAML)) or {}
    dd = scoring.get("dedup", {}) or {}
    hamming = int(dd.get("phash_hamming_max", 6))
    window = int(dd.get("low_confidence_window_days", 30))
    sources = yaml.safe_load(paths.read_config(paths.SOURCES_YAML)) or {}
    priority = sources.get("priority", {}) or {}
    return hamming, window, priority


# --- union-find -------------------------------------------------------------


class _UF:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


# --- pHash ------------------------------------------------------------------


def _compute_phash(derivative_path: str) -> Optional[str]:
    import imagehash
    from PIL import Image

    try:
        with Image.open(derivative_path) as im:
            return str(imagehash.phash(im))
    except Exception:
        return None


def _hamming_hex(a: str, b: str) -> int:
    """Hamming distance between two hex pHash strings (same length)."""
    # imagehash hex strings are 16 hex chars (64 bits).
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# --- driver -----------------------------------------------------------------


def dedup(db_path: Optional[Path] = None) -> dict:
    hamming_max, window_days, priority = _load_cfg()

    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id, src, derivative_path, best_datetime, date_confidence, "
            "w, h, bytes FROM assets"
        ).fetchall()

        # 1. Compute + persist pHash for each asset.
        info: dict[str, dict] = {}
        for r in rows:
            ph = _compute_phash(r["derivative_path"])
            info[r["asset_id"]] = {
                "src": r["src"],
                "phash": ph,
                "dt": _parse_dt(r["best_datetime"]),
                "conf": r["date_confidence"],
                "w": r["w"] or 0,
                "h": r["h"] or 0,
                "bytes": r["bytes"] or 0,
            }
            conn.execute("UPDATE assets SET phash=? WHERE asset_id=?", (ph, r["asset_id"]))

        uf = _UF()
        for aid in info:
            uf.add(aid)

        hashed = [(aid, d) for aid, d in info.items() if d["phash"]]

        # 2. Primary pass: bucket by day-key, compare within day + adjacent days.
        by_day: dict[str, list[str]] = defaultdict(list)
        for aid, d in hashed:
            key = d["dt"].strftime("%Y-%m-%d") if d["dt"] else "nodate"
            by_day[key].append(aid)

        def _day_neighbors(key: str) -> list[str]:
            if key == "nodate":
                return ["nodate"]
            base = datetime.strptime(key, "%Y-%m-%d")
            return [(base + timedelta(days=off)).strftime("%Y-%m-%d") for off in (-1, 0, 1)]

        compared_keys: set[str] = set()
        for key in list(by_day.keys()):
            if key in compared_keys:
                continue
            # Gather candidate pool = this day + adjacent days.
            pool: list[str] = []
            for nk in _day_neighbors(key):
                pool.extend(by_day.get(nk, []))
            compared_keys.add(key)
            # Pairwise within the (small) pool.
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    a, b = pool[i], pool[j]
                    pa, pb = info[a]["phash"], info[b]["phash"]
                    if pa and pb and _hamming_hex(pa, pb) <= hamming_max:
                        uf.union(a, b)

        # 3. Low-confidence widening: +/-window_days pHash match (bounded).
        low = [(aid, d) for aid, d in hashed if d["conf"] == "low" and d["dt"]]
        for aid, d in low:
            lo = d["dt"] - timedelta(days=window_days)
            hi = d["dt"] + timedelta(days=window_days)
            for other, od in hashed:
                if other == aid or not od["dt"]:
                    continue
                if lo <= od["dt"] <= hi:
                    if _hamming_hex(d["phash"], od["phash"]) <= hamming_max:
                        uf.union(aid, other)

        # 4. Materialize groups, assign integer dup_group ids, pick keepers.
        groups: dict[str, list[str]] = defaultdict(list)
        for aid in info:
            groups[uf.find(aid)].append(aid)

        def _priority(aid: str) -> int:
            return int(priority.get(info[aid]["src"], 0))

        def _keeper_rank(aid: str) -> tuple:
            d = info[aid]
            # Higher is better on each; sort descending.
            return (_priority(aid), d["w"] * d["h"], d["bytes"])

        group_id = 0
        n_dup_flagged = 0
        n_groups_multi = 0
        for members in groups.values():
            group_id += 1
            keeper = max(members, key=_keeper_rank)
            if len(members) > 1:
                n_groups_multi += 1
            for aid in members:
                keep = 1 if aid == keeper else 0
                if keep == 0:
                    n_dup_flagged += 1
                conn.execute(
                    "UPDATE assets SET dup_group=?, is_dup_keep=? WHERE asset_id=?",
                    (group_id, keep, aid),
                )

        total = len(info)
        kept = total - n_dup_flagged

    funnel.append(
        f"[dedup]   ingested={total} -> deduped_keep={kept} "
        f"(removed {n_dup_flagged} dup-flagged; {n_groups_multi} multi-member groups)"
    )
    return {
        "total": total,
        "kept": kept,
        "dup_flagged": n_dup_flagged,
        "multi_member_groups": n_groups_multi,
    }
