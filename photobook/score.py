"""Curation scoring — rank a per-chapter shortlist for human review.

Composite score per asset:
    score = w_quality * quality_component
          + w_hero    * hero_boost          (wedding source + milestone dates)
          + w_diverse * diversity            (visual spread within its chapter)
          - w_penalty * junk                 (screenshot | document | extreme blur)

Then, per chapter, a greedy shortlist picks the top-scoring assets up to a
per-chapter target (shortlist = target * shortlist_multiplier, so the human has
real choice), enforcing:
  - max-per-day cap (don't let one burst dominate a chapter)
  - pHash-neighbor skip (don't shortlist two near-identical frames)

Shortlisted assets get review_status='pending'; everything else stays NULL.
Only dedup KEEPERS (is_dup_keep=1) are eligible.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from . import db, funnel, paths


def _load_cfg() -> dict:
    scoring = yaml.safe_load(paths.read_config(paths.SCORING_YAML)) or {}
    return scoring.get("score", {}) or {}


def _load_targets_and_chapters() -> tuple[dict, list[str]]:
    ch = yaml.safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    chapters = [c["id"] for c in (ch.get("chapters", []) or [])]
    targets = ch.get("targets", {}) or {}
    return targets, chapters


def _priority_map() -> dict:
    src = yaml.safe_load(paths.read_config(paths.SOURCES_YAML)) or {}
    return src.get("priority", {}) or {}


def _hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _day_key(dt_iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(dt_iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return "nodate"


def score(db_path: Optional[Path] = None,
          total_target: int = 200) -> dict:
    cfg = _load_cfg()
    w = cfg.get("weights", {}) or {}
    w_q = float(w.get("quality", 1.0))
    w_h = float(w.get("hero", 2.0))
    w_d = float(w.get("diverse", 1.0))
    w_p = float(w.get("penalty", 3.0))
    w_fav = float(w.get("favorite", 3.0))    # Google star — big boost (the owner's call)
    w_ppl = float(w.get("people", 1.0))      # 2+ tagged people — surface groups, never penalize
    max_per_day = int(cfg.get("max_per_day", 3))
    shortlist_mult = float(cfg.get("shortlist_multiplier", 2.5))
    milestone_dates = set(cfg.get("milestone_dates", []) or [])

    targets, chapter_ids = _load_targets_and_chapters()
    priority = _priority_map()

    with db.session(db_path) as conn:
        # Eligible = dedup keepers that the owner has NOT already decided on.
        # A human decision (accept/reject/hero) is authoritative — re-scoring
        # must never re-shortlist or overwrite it. Only NULL/'pending' rows are
        # in play. (Bug fix: a prior version re-marked accepted photos as
        # 'pending', wiping picks on a re-score.)
        rows = conn.execute(
            "SELECT asset_id, src, chapter, best_datetime, phash, blur_var, "
            "is_screenshot, is_document, quality_pass, favorited, people_count, w, h "
            "FROM assets WHERE is_dup_keep=1 "
            "AND (review_status IS NULL OR review_status='pending')"
        ).fetchall()

        # Normalize blur to a 0..1 quality component per-chapter (robust min-max).
        blur_by_chapter: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            if r["blur_var"] is not None:
                blur_by_chapter[r["chapter"]].append(r["blur_var"])
        blur_bounds = {
            c: (min(v), max(v)) for c, v in blur_by_chapter.items() if v
        }

        # Reset any prior scoring/shortlist for a clean idempotent run.
        conn.execute("UPDATE assets SET score=NULL")
        conn.execute("UPDATE assets SET review_status=NULL WHERE review_status='pending'")

        scored: dict[str, dict] = {}
        for r in rows:
            ch = r["chapter"]
            # quality component
            if r["blur_var"] is not None and ch in blur_bounds:
                lo, hi = blur_bounds[ch]
                q = (r["blur_var"] - lo) / (hi - lo) if hi > lo else 0.5
            else:
                q = 0.5
            q = max(0.0, min(1.0, q))
            if r["quality_pass"] == 1:
                q = 0.5 + 0.5 * q  # passed frames float above failed ones

            # hero boost: wedding-source (thumbdrive) or milestone date
            hero = 0.0
            if int(priority.get(r["src"], 0)) >= 100:
                hero += 1.0
            if _day_key(r["best_datetime"]) in milestone_dates:
                hero += 1.0

            # favorite boost: a photo the owner starred in Google Photos. Big boost
            # (his call) — nearly always surfaces, but a favorited junk/blur shot
            # can still lose to a great non-favorite because quality still counts.
            fav = 1.0 if r["favorited"] else 0.0

            # people boost: 2+ face-tagged people => friends/family/group. ADDITIVE
            # ONLY — a solo/couple photo (people_count 0/1/NULL) is never penalized;
            # Google misses many faces, and the book is still about the two of them.
            ppl = 0.0
            pc = r["people_count"]
            if pc is not None and pc >= 2:
                ppl = 1.0

            # junk penalty
            junk = 0.0
            if r["is_screenshot"]:
                junk += 1.0
            if r["is_document"]:
                junk += 1.0

            # diversity filled in after we know chapter members (below); seed 0.
            s = w_q * q + w_h * hero + w_fav * fav + w_ppl * ppl - w_p * junk
            scored[r["asset_id"]] = {
                "row": r, "q": q, "hero": hero, "fav": fav, "ppl": ppl,
                "junk": junk, "base": s,
            }

        # Diversity: reward being far (in pHash) from the chapter's centroid-ish
        # set. Cheap proxy: average Hamming distance to up to 20 chapter peers.
        by_chapter: dict[str, list[str]] = defaultdict(list)
        for aid, d in scored.items():
            by_chapter[d["row"]["chapter"]].append(aid)

        for ch, members in by_chapter.items():
            peers = [scored[a]["row"]["phash"] for a in members if scored[a]["row"]["phash"]]
            for aid in members:
                ph = scored[aid]["row"]["phash"]
                if ph and len(peers) > 1:
                    sample = peers[:20]
                    dists = [_hamming_hex(ph, p) for p in sample if p != ph]
                    div = (sum(dists) / len(dists) / 64.0) if dists else 0.0
                else:
                    div = 0.0
                final = scored[aid]["base"] + w_d * div
                scored[aid]["final"] = final
                conn.execute("UPDATE assets SET score=? WHERE asset_id=?", (final, aid))

        # Per-chapter target allocation.
        n_chapters = max(1, len(chapter_ids))
        default_target = max(1, round(total_target / n_chapters))

        shortlist_total = 0
        per_chapter_short: dict[str, int] = {}
        for ch, members in by_chapter.items():
            tgt = int(targets.get(ch, default_target))
            cap = max(1, round(tgt * shortlist_mult))

            # Greedy by score desc, enforcing max-per-day + pHash-neighbor skip.
            ordered = sorted(members, key=lambda a: scored[a].get("final", 0.0), reverse=True)
            picked: list[str] = []
            per_day: dict[str, int] = defaultdict(int)
            picked_hashes: list[str] = []
            for aid in ordered:
                if len(picked) >= cap:
                    break
                row = scored[aid]["row"]
                dk = _day_key(row["best_datetime"])
                if per_day[dk] >= max_per_day:
                    continue
                ph = row["phash"]
                if ph and any(_hamming_hex(ph, h) <= 6 for h in picked_hashes):
                    continue  # too similar to an already-picked shortlist frame
                picked.append(aid)
                per_day[dk] += 1
                if ph:
                    picked_hashes.append(ph)

            for aid in picked:
                conn.execute(
                    "UPDATE assets SET review_status='pending' WHERE asset_id=?", (aid,)
                )
            per_chapter_short[ch] = len(picked)
            shortlist_total += len(picked)

    funnel.append(
        f"[score]   shortlist(pending)={shortlist_total} across {len(by_chapter)} chapters "
        f"(target~{total_target}, mult={shortlist_mult})"
    )
    return {
        "shortlist_total": shortlist_total,
        "per_chapter": per_chapter_short,
        "eligible_keepers": len(scored),
    }
