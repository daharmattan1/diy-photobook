"""Quality gate — cheap, ADVISORY heuristics that flag likely junk.

Nothing is deleted. Every flag is a signal the scorer (Phase 7) and the human
reviewer (Phase 8) can weigh. Heuristics run on the 2560px DERIVATIVE (fast):

  - blur:        variance-of-Laplacian; per-source lower-tail calibration so a
                 softer source isn't unfairly punished.
  - dark/blown:  mean luminance + highlight-clipping fraction.
  - screenshot:  device aspect ratio + PNG-without-EXIF + 'Screenshot_' name.
  - document:    high edge density + low saturation + extreme aspect.

Sets: blur_var, is_screenshot, is_document, quality_pass (all advisory).
quality_pass=0 means "at least one strong junk signal"; it never blocks a photo
from appearing in review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import yaml

from . import db, funnel, paths


def _load_cfg() -> dict:
    scoring = yaml.safe_load(paths.read_config(paths.SCORING_YAML)) or {}
    return scoring.get("quality", {}) or {}


def _blur_variance(gray: np.ndarray) -> float:
    """Variance of the Laplacian — low = blurry."""
    import cv2

    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _luma_stats(rgb: np.ndarray) -> tuple[float, float]:
    """Return (mean luminance 0-255, fraction of near-white pixels)."""
    luma = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    mean = float(luma.mean())
    blown = float((luma > 250).mean())
    return mean, blown


def _saturation_mean(rgb: np.ndarray) -> float:
    import cv2

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    return float(hsv[..., 1].mean()) / 255.0


def _edge_density(gray: np.ndarray) -> float:
    import cv2

    edges = cv2.Canny(gray, 100, 200)
    return float((edges > 0).mean())


def _aspect_is_device(w: int, h: int, ratios: list) -> bool:
    if w == 0 or h == 0:
        return False
    long_edge, short_edge = max(w, h), min(w, h)
    r = long_edge / short_edge
    for pair in ratios:
        target = max(pair) / min(pair)
        if abs(r - target) / target < 0.06:  # within 6%
            return True
    return False


def quality(db_path: Optional[Path] = None, only_missing: bool = True,
            commit_every: int = 500) -> dict:
    """Advisory quality gate.

    only_missing=True (default): compute metrics only for rows not yet scored
      (quality_pass IS NULL). Already-scored rows keep their values. This makes a
      re-run after a crash cheap and resumable — it picks up exactly where it
      stopped.
    commit_every: flush UPDATEs to disk every N rows so an interrupted run loses
      at most N rows of work (not the whole pass). Durability for long jobs.

    Per-source blur threshold: to keep the percentile stable across incremental
    runs, calibration reads EVERY row's blur_var (already-scored rows via the DB,
    newly-scored rows from this pass), not just this pass's rows.
    """
    import cv2

    cfg = _load_cfg()
    blur_pct = float(cfg.get("blur_percentile", 15))
    dark_max = float(cfg.get("dark_luma_max", 25))
    blown_max = float(cfg.get("blown_clip_frac_max", 0.35))
    ss_ratios = cfg.get("screenshot_aspect_ratios", [[9, 16], [9, 19.5], [3, 4]])
    doc_edge = float(cfg.get("document_min_edge_density", 0.12))
    doc_sat = float(cfg.get("document_max_saturation", 0.20))

    with db.session(db_path) as conn:
        where = "WHERE quality_pass IS NULL" if only_missing else ""
        rows = conn.execute(
            f"SELECT asset_id, src, orig_path, derivative_path, w, h FROM assets {where}"
        ).fetchall()
        # Seed per-source blur samples from ALREADY-scored rows so the percentile
        # threshold stays stable when we only process the new (missing) rows.
        prior_blur: dict[str, list[float]] = {}
        if only_missing:
            for src, bv in conn.execute(
                "SELECT src, blur_var FROM assets WHERE quality_pass IS NOT NULL "
                "AND blur_var IS NOT NULL"
            ):
                prior_blur.setdefault(src, []).append(bv)

        # First pass: compute raw metrics.
        metrics: dict[str, dict] = {}
        blur_by_source: dict[str, list[float]] = {}
        for r in rows:
            aid = r["asset_id"]
            try:
                img = cv2.imread(r["derivative_path"])  # BGR
                if img is None:
                    raise ValueError("cv2 could not read derivative")
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                bvar = _blur_variance(gray)
                luma_mean, blown = _luma_stats(rgb.astype(np.float32))
                sat = _saturation_mean(rgb)
                edge = _edge_density(gray)
            except Exception:
                metrics[aid] = None
                continue

            name = Path(r["orig_path"]).name if r["orig_path"] else ""
            is_png = name.lower().endswith(".png")
            is_ss = (
                _aspect_is_device(r["w"] or 0, r["h"] or 0, ss_ratios)
                and (is_png or name.lower().startswith("screenshot"))
            ) or name.lower().startswith("screenshot")
            is_doc = (edge >= doc_edge and sat <= doc_sat)

            metrics[aid] = {
                "blur": bvar, "luma": luma_mean, "blown": blown,
                "sat": sat, "edge": edge, "is_ss": is_ss, "is_doc": is_doc,
            }
            blur_by_source.setdefault(r["src"], []).append(bvar)

        # Per-source blur threshold = the configured lower percentile, computed
        # over this pass's values PLUS any already-scored rows (prior_blur) so the
        # threshold is stable across incremental runs.
        blur_thresh: dict[str, float] = {}
        all_srcs = set(blur_by_source) | set(prior_blur)
        for src in all_srcs:
            vals = blur_by_source.get(src, []) + prior_blur.get(src, [])
            if len(vals) >= 8:
                blur_thresh[src] = float(np.percentile(vals, blur_pct))
            else:
                blur_thresh[src] = 0.0  # too few to calibrate → don't punish

        n_pass = n_ss = n_doc = n_blur = n_dark = 0
        for i, r in enumerate(rows):
            aid = r["asset_id"]
            m = metrics.get(aid)
            if m is None:
                conn.execute(
                    "UPDATE assets SET blur_var=NULL, is_screenshot=0, is_document=0, quality_pass=0 "
                    "WHERE asset_id=?", (aid,))
                continue
            is_blurry = m["blur"] < blur_thresh.get(r["src"], 0.0)
            is_dark = m["luma"] < dark_max
            is_blown = m["blown"] > blown_max
            q_pass = 0 if (m["is_ss"] or m["is_doc"] or is_blurry or is_dark or is_blown) else 1

            n_pass += q_pass
            n_ss += int(m["is_ss"]); n_doc += int(m["is_doc"])
            n_blur += int(is_blurry); n_dark += int(is_dark or is_blown)

            conn.execute(
                "UPDATE assets SET blur_var=?, is_screenshot=?, is_document=?, quality_pass=? "
                "WHERE asset_id=?",
                (m["blur"], int(m["is_ss"]), int(m["is_doc"]), q_pass, aid),
            )
            # Durability: flush periodically so a crash loses at most commit_every rows.
            if commit_every and (i + 1) % commit_every == 0:
                conn.commit()

        total = len(rows)

    funnel.append(
        f"[quality] quality_pass={n_pass}/{total} "
        f"(screenshot={n_ss} document={n_doc} blur={n_blur} dark/blown={n_dark})"
    )
    return {
        "total": total, "quality_pass": n_pass,
        "screenshot": n_ss, "document": n_doc, "blurry": n_blur, "dark_blown": n_dark,
    }
