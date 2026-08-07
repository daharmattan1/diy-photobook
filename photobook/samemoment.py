"""Conservative 'same moment' clustering — group photos that are almost certainly
the same instant so the human can thin a burst to one frame.

Three CONSERVATIVE signals (union-find over accepts); tuned to avoid false
groupings (a false group would suggest cutting a distinct photo):
  1. tight visual pHash (Hamming <= 6) — near-identical frames.
  2. pure-COUNTER filename adjacency — IMG_9948/9949, DSC_0041/0042 (3-4 digit
     sequence counters), same source, within 2 counts. NOT date-based names
     (PXL_20240622 is a DATE; +1 there is next DAY, not next frame — excluded).
  3. real-timestamp burst — EXIF/sidecar/filename dates within 120s, same source.
     Event/unplaced synthetic dates are ignored (they'd falsely group a whole event).

Advisory only: the caller shows clusters so the human picks which to keep.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional


def _ham(a: Optional[str], b: Optional[str]) -> int:
    if not a or not b:
        return 99
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _counter(name: Optional[str]):
    b = os.path.basename(name or "")
    m = re.match(r"(IMG|DSC|DSCN|PICT|CIMG)_?(\d{3,4})\b", b, re.I)
    return (m.group(1).upper(), int(m.group(2))) if m else (None, None)


def _ts(row) -> Optional[float]:
    if row["date_source"] not in ("exif", "sidecar", "filename"):
        return None
    try:
        return datetime.fromisoformat(
            (row["best_datetime"] or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def cluster(rows) -> dict[str, int]:
    """rows: sequence of sqlite rows with asset_id, src, phash, orig_path,
    best_datetime, date_source. Returns {asset_id: cluster_id} for photos that
    are in a multi-photo same-moment cluster (singletons omitted)."""
    ids = [r["asset_id"] for r in rows]
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1. visual
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if _ham(rows[i]["phash"], rows[j]["phash"]) <= 6:
                union(rows[i]["asset_id"], rows[j]["asset_id"])

    # 2. pure-counter adjacency
    bygrp: dict = defaultdict(list)
    for r in rows:
        pre, num = _counter(r["orig_path"])
        if pre is not None:
            bygrp[(r["src"], pre)].append((num, r["asset_id"]))
    for _, v in bygrp.items():
        v.sort()
        for i in range(len(v) - 1):
            if 0 < v[i + 1][0] - v[i][0] <= 2:
                union(v[i][1], v[i + 1][1])

    # 3. real-timestamp burst
    dated = [(_ts(r), r) for r in rows if _ts(r) is not None]
    dated.sort(key=lambda t: (t[1]["src"], t[0]))
    for i in range(len(dated) - 1):
        a, b = dated[i], dated[i + 1]
        if a[1]["src"] == b[1]["src"] and 0 <= b[0] - a[0] <= 120:
            union(a[1]["asset_id"], b[1]["asset_id"])

    groups: dict = defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    out: dict[str, int] = {}
    cid = 0
    for root, members in groups.items():
        if len(members) > 1:
            cid += 1
            for m in members:
                out[m] = cid
    return out
