"""Photo-book definition and automatic draft composition.

The automatic draft is intentionally conservative:

* every generated page contains photos;
* every automatic placement preserves the complete source image;
* page geometry is derived from the photos' real aspect ratios;
* photos stay in story order instead of being ranked into arbitrary cells;
* small chapters collapse into one complete spread instead of sparse leftovers;
* only explicitly designated heroes may occupy a page alone.

The browser editor remains the place for taste decisions.  This module produces
a coherent, printable starting point without silently cropping or losing photos.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime
from itertools import permutations, product
from pathlib import Path
from typing import Iterable, Sequence

import yaml

from . import db, paths
from .bookmodel import (
    DPI_MIN,
    DPI_OK,
    G,
    M,
    OFF_WHITE,
    PAGE_IN,
    SCHEMA_VERSION,
    TITLE_H,
    cell_asset,
    contained_print_size,
    effective_dpi,
    geometry_meta,
    make_cell,
)

AUTO_PHOTO_TARGET = 480  # EPIC/comprehensive book: place ~all accepted photos (the owner pares down in the editor)
ROOT = Path(__file__).resolve().parents[1]
BOOK_JSON = ROOT / "book.json"
STORY_YAML = ROOT / "config" / "story.yaml"

# --- Chronology anchors (optional) --------------------------------------------
# Era windows are OPTIONAL date anchors for the era-consistency validator and the
# per-chapter chronology repair: a photo whose resolved date falls outside its
# era's window gets flagged for review. Keys are your own era/chapter ids; values
# are (start, end) inclusive dates. This is EXAMPLE data — replace the ids and
# dates with your own eras, or leave it empty (an unknown era simply has no
# window and is never flagged). Omit an era here to leave it unbounded.
ERA_WINDOWS: dict[str, tuple[str, str]] = {
    "01_early_years": ("2001-01-01", "2003-12-31"),
    "02_travel": ("2004-01-01", "2006-12-31"),
    "03_new_home": ("2007-01-01", "2009-12-31"),
}

# Tags that mark a photo as a family/relationship beat (feature-selection bias).
# EXAMPLE tag names — use whatever tags you actually apply in the editor.
FAMILY_TAGS = {
    "family", "immediate family", "extended family", "cousins", "siblings",
    "milestones",
}

# date_source values grouped by how much we trust the resolved best_datetime.
# AUTHORITATIVE  = a real capture date (EXIF/sidecar/URL/embedded-in-filename/
#                  event-anchored) — sort chronologically, may drive auto-moves.
# SYNTHETIC      = filename_seq — an ORDINAL spread, not a real date; only its
#                  relative order within a chapter cohort is meaningful.
# FALLBACK       = mtime/path-year/friend-estimate — a plausibility guess; never
#                  sort it to a chapter's front and never auto-move on it.
_AUTHORITATIVE_SOURCES = {"exif", "sidecar", "url_path", "filename", "event", "known"}
_SYNTHETIC_SOURCES = {"filename_seq"}
_FALLBACK_SOURCES = {"mtime", "path_year", "epoch", "friend-estimate"}


def _date_trust(photo: dict) -> str:
    """Classify the trust tier of a photo's resolved date."""
    source = str(photo.get("date_source") or "").strip().lower()
    if source in _SYNTHETIC_SOURCES:
        return "synthetic"
    if source in _FALLBACK_SOURCES:
        return "fallback"
    if source in _AUTHORITATIVE_SOURCES:
        return "authoritative"
    # Unknown source: fall back to the confidence flag.
    return (
        "authoritative"
        if str(photo.get("date_confidence") or "").lower() in ("high", "medium")
        else "fallback"
    )

# Compatibility surface for callers that imported the prior static dictionary.
# build_book() replaces its contents with the generated aspect-aware templates.
TEMPLATES: dict[str, dict] = {}

SPREAD_ARCHETYPES = {
    "chapter_feature_plus_cluster": {
        "left_role": "feature",
        "right_role": "cluster",
        "purpose": "Chapter title and story lead face a denser supporting cluster.",
    },
    "chapter_balanced_pair": {
        "left_role": "feature",
        "right_role": "feature",
        "purpose": "A short chapter becomes one complete, balanced spread.",
    },
    "chapter_dense_pair": {
        "left_role": "cluster",
        "right_role": "cluster",
        "purpose": "A chapter opens with two dense reference-page collages and no filler space.",
    },
    "chapter_triptych_plus_feature": {
        "left_role": "cluster",
        "right_role": "feature",
        "purpose": "A four-photo chapter traces the reference-book triptych facing a titled feature.",
    },
    "feature_plus_cluster": {
        "left_role": "feature",
        "right_role": "cluster",
        "purpose": "A calm page opens into a denser story page.",
    },
    "cluster_plus_feature": {
        "left_role": "cluster",
        "right_role": "feature",
        "purpose": "A dense story page resolves into a calmer feature page.",
    },
    "balanced_sequence": {
        "left_role": "sequence",
        "right_role": "sequence",
        "purpose": "Related images progress evenly across the facing spread.",
    },
    "panorama_plus_support": {
        "left_role": "panorama",
        "right_role": "support",
        "purpose": "A naturally wide scene anchors contextual images.",
    },
}


def _compositions(total: int, max_part: int = 3, max_rows: int = 3) -> list[tuple[int, ...]]:
    results: list[tuple[int, ...]] = []

    def visit(remaining: int, parts: list[int]) -> None:
        if remaining == 0:
            results.append(tuple(parts))
            return
        if len(parts) >= max_rows:
            return
        for size in range(1, min(max_part, remaining) + 1):
            visit(remaining - size, parts + [size])

    visit(total, [])
    return results


def aspect_layout(aspects: Sequence[float], *, title: bool = False) -> list[list[float]]:
    """Return non-overlapping cells whose ratios match the source photos.

    The algorithm evaluates all sequence-preserving row partitions (up to three
    photos per row and three rows), then selects the arrangement using the most
    printable area. Rows are centered when their natural geometry does not fill
    the page. No source pixels need to be discarded.
    """
    if not aspects:
        return []
    if len(aspects) > 5:
        raise ValueError("A page may contain at most five photos")
    normalized = [max(0.05, float(value)) for value in aspects]
    usable_w = PAGE_IN - 2 * M
    y_start = M + (TITLE_H if title else 0.0)
    usable_h = PAGE_IN - M - y_start
    best: tuple[float, list[list[float]]] | None = None

    for row_sizes in _compositions(len(normalized)):
        rows: list[tuple[list[float], float]] = []
        offset = 0
        natural_total_h = G * (len(row_sizes) - 1)
        for size in row_sizes:
            row_aspects = normalized[offset:offset + size]
            offset += size
            row_h = (usable_w - G * (size - 1)) / sum(row_aspects)
            rows.append((row_aspects, row_h))
            natural_total_h += row_h

        scale = min(1.0, usable_h / natural_total_h)
        scaled_total_h = (
            sum(height * scale for _, height in rows) + G * (len(rows) - 1)
        )
        y = y_start + max(0.0, (usable_h - scaled_total_h) / 2)
        cells: list[list[float]] = []
        used_area = 0.0
        for row_aspects, natural_h in rows:
            row_h = natural_h * scale
            widths = [aspect * row_h for aspect in row_aspects]
            row_w = sum(widths) + G * (len(widths) - 1)
            x = M + max(0.0, (usable_w - row_w) / 2)
            for width, aspect in zip(widths, row_aspects):
                # Calculate height from the rounded width so the persisted cell
                # keeps the source ratio to within the schema precision.
                rounded_w = round(width, 4)
                rounded_h = round(rounded_w / aspect, 4)
                cells.append([round(x, 4), round(y, 4), rounded_w, rounded_h])
                used_area += rounded_w * rounded_h
                x += width + G
            y += row_h + G

        # Prefer used photographic area. A small penalty for extra rows prevents
        # needlessly fragmented pages when two arrangements use similar area.
        score = used_area - 0.12 * (len(rows) - 1)
        if best is None or score > best[0]:
            best = (score, cells)

    assert best is not None
    return best[1]


def page_counts(photo_count: int, *, allow_single_hero: bool = False) -> list[int]:
    """Split a chapter into a complete, even number of populated pages."""
    if photo_count < 1:
        return []
    if photo_count == 3:
        if not allow_single_hero:
            raise ValueError("Three photos require an explicit single-image hero")
        return [1, 2]
    if photo_count < 4:
        raise ValueError("A chapter needs at least four photos for a complete spread")

    page_count = max(2, math.ceil(photo_count / 4))
    if page_count % 2:
        page_count += 1
    while photo_count < page_count * 2:
        page_count -= 2
    while photo_count > page_count * 5:
        page_count += 2

    base, extra = divmod(photo_count, page_count)
    if not 2 <= base <= 5 or base + (1 if extra else 0) > 5:
        raise ValueError(f"Cannot distribute {photo_count} photos into complete spreads")
    counts = [base] * page_count
    if page_count == 2 and extra == 1:
        counts[0 if base % 2 == 0 else 1] += 1
    else:
        rhythm = list(range(1, page_count, 2)) + list(range(0, page_count, 2))
        for index in rhythm[:extra]:
            counts[index] += 1
    return counts


def spread_counts(photo_count: int) -> list[tuple[int, int]]:
    """Trace the dense/calm spread rhythm from the owner's reference pages.

    The collage page may contain up to seven photos. This is intentional: the
    supplied books routinely use 5-9 photos per spread with very small gutters.
    """
    if photo_count < 4:
        raise ValueError("A chapter needs at least four photos for a complete spread")
    # Bigger photos / more spreads (design doctrine: "fewer small images, build
    # more pages"). Target ~7 photos per spread and cap page density at FOUR
    # photos so nothing prints tiny. Combined with smart-fill (edge-to-edge),
    # this is the "photos too small" fix.
    spread_count = 1 if photo_count <= 7 else math.ceil(photo_count / 7)
    while photo_count < spread_count * 4:
        spread_count -= 1
    while photo_count > spread_count * 8:
        spread_count += 1

    base, extra = divmod(photo_count, spread_count)
    totals = [base + (1 if index < extra else 0) for index in range(spread_count)]
    patterns = {
        # Balanced splits only, max four photos per page (no tiny collages).
        4: [(2, 2)],
        5: [(3, 2), (2, 3)],
        6: [(3, 3)],
        7: [(4, 3), (3, 4)],
        8: [(4, 4)],
    }
    return [
        patterns[total][index % len(patterns[total])]
        for index, total in enumerate(totals)
    ]


def _story_aware_spread_counts(photos: Sequence[dict]) -> list[tuple[int, int]]:
    """Move page boundaries to metadata-cluster boundaries when possible.

    The spread totals retain the dense/calm rhythm. Only the left/right split
    changes, choosing from the supplied 2-through-6-photo archetype families.
    That keeps a trip, birthday, wedding, or other named group on one page
    instead of interleaving it with the next event merely because a fixed
    4+5 split happened to land in the middle.

    COVERAGE (crop-free white-space fix): a CALM page (<=3 photos) facing a DENSE
    page (>=5) is the dominant source of empty bands — the calm page is
    letterboxed short, and `_align_spread_templates` then drags the dense page to
    that short shared height, leaving big horizontal margins. So the split is now
    chosen to AVOID that clash first, then keep story groups whole, then balance.
    """
    defaults = spread_counts(len(photos))
    groups = [photo.get("story_group") for photo in photos]
    if len({group for group in groups if group}) <= 1:
        return defaults

    result: list[tuple[int, int]] = []
    cursor = 0
    for default_left, default_right in defaults:
        total = default_left + default_right
        options = [
            (left, total - left)
            for left in range(2, 5)
            if 2 <= total - left <= 4   # max FOUR photos per page (bigger photos)
        ]
        if not options:
            result.append((default_left, default_right))
            cursor += total
            continue

        def penalty(pair: tuple[int, int]) -> tuple[int, int, int, int, int]:
            left, right = pair
            boundary = cursor + left
            splits_group = (
                0 < boundary < len(groups)
                and groups[boundary - 1]
                and groups[boundary - 1] == groups[boundary]
            )
            # A <=3-photo page next to a >=5-photo page wastes the most space
            # after the shared-height alignment. Never do it if avoidable.
            calm_dense_clash = 1 if (min(pair) <= 3 and max(pair) >= 5) else 0
            # Cap 7-photo pages: 7-up only survives if it hits >=80% coverage,
            # which a balanced alternative reaches more reliably, so avoid a
            # 7-photo side whenever a smaller split exists.
            oversize = sum(1 for count in pair if count >= 7)
            return (
                calm_dense_clash,               # 1) coverage: no calm/dense clash
                oversize,                        # 2) coverage: avoid 7-photo pages
                1 if splits_group else 0,        # 3) keep a story group whole
                abs(left - right),               # 4) most balanced
                abs(left - default_left),        # 5) closest to the default rhythm
                left,                            # 6) deterministic tiebreak
            )

        chosen = min(options, key=penalty)
        result.append(chosen)
        cursor += total
    return result


def _spread_archetype(
    left_batch: Sequence[dict],
    right_batch: Sequence[dict],
    *,
    chapter_opener: bool,
) -> str:
    left_count, right_count = len(left_batch), len(right_batch)
    all_photos = [*left_batch, *right_batch]
    has_panorama = any(
        (photo.get("w") or 1) / max(1, photo.get("h") or 1) >= 2.2
        for photo in all_photos
    )
    if has_panorama and not chapter_opener:
        return "panorama_plus_support"
    if chapter_opener:
        if left_count == 3 and right_count == 1:
            return "chapter_triptych_plus_feature"
        if left_count == right_count == 2:
            return "chapter_balanced_pair"
        if left_count >= 3 and right_count >= 3:
            return "chapter_dense_pair"
        if left_count < right_count:
            return "chapter_feature_plus_cluster"
    if left_count < right_count:
        return "feature_plus_cluster"
    if left_count > right_count:
        return "cluster_plus_feature"
    return "balanced_sequence"


def _layout_role(photo_count: int) -> str:
    if photo_count <= 2:
        return "feature"
    if photo_count >= 4:
        return "cluster"
    return "sequence"


def _cell_dpi(width_px: int | None, height_px: int | None, cell: Sequence[float]) -> float:
    if not width_px or not height_px or cell[2] <= 0 or cell[3] <= 0:
        return 0.0
    return min(width_px / cell[2], height_px / cell[3])


def _shrink_to_dpi(cell: list[float], photo: dict, floor: int = DPI_OK) -> list[float]:
    """Reduce a cell around its center when the source cannot print at floor DPI."""
    dpi = _cell_dpi(photo.get("w"), photo.get("h"), cell)
    if dpi <= 0 or dpi >= floor:
        return cell
    factor = max(0.1, dpi / floor)
    x, y, width, height = cell
    new_w, new_h = width * factor, height * factor
    return [
        round(x + (width - new_w) / 2, 4),
        round(y + (height - new_h) / 2, 4),
        round(new_w, 4),
        round(new_h, 4),
    ]


def _feature_weight(photo: dict) -> float:
    """How strongly a photo wants the anchor/largest cell on its page.

    hero > favorited > family-tagged > everything else. Untagged photos get 0,
    so feature photos out-compete them for the big cells and untagged ones settle
    into the small cells. (the owner's revised doctrine: the auto-draft SHOULD lead
    with the people beats, not place neutrally.)
    """
    if photo.get("is_hero"):
        return 3.0
    if photo.get("favorited"):
        return 2.0
    tags = {t.strip().lower() for t in str(photo.get("tags") or "").split(",")}
    if tags & FAMILY_TAGS:
        return 1.0
    return 0.0


def _quality_key(photo: dict) -> tuple:
    return (
        int(bool(photo.get("is_hero"))),
        int(bool(photo.get("favorited"))),
        float(photo.get("score") or 0),
        int(photo.get("w") or 0) * int(photo.get("h") or 0),
    )


def _story_key(photo: dict) -> tuple:
    if photo.get("story_order") is not None:
        return (0, float(photo["story_order"]), photo["asset_id"])
    return (
        1,
        photo.get("best_datetime") or "9999",
        photo.get("event") or "",
        photo["asset_id"],
    )


def _select_evenly(photos: Sequence[dict], target: int) -> list[dict]:
    """Select a neutral subset with timeline coverage, then restore story order."""
    ordered = sorted(photos, key=_story_key)
    if target >= len(ordered):
        return ordered
    target = max(4, target)
    mandatory = [
        photo
        for photo in ordered
        if photo.get("is_hero")
        or photo.get("favorited")
        or photo.get("story_pinned")
    ]
    selected = {photo["asset_id"]: photo for photo in mandatory[:target]}
    remaining_slots = target - len(selected)
    candidates = [photo for photo in ordered if photo["asset_id"] not in selected]
    if remaining_slots > 0:
        for slot in range(remaining_slots):
            start = round(slot * len(candidates) / remaining_slots)
            end = round((slot + 1) * len(candidates) / remaining_slots)
            bucket = candidates[start:end] or candidates[start:start + 1]
            if bucket:
                pick = max(bucket, key=_quality_key)
                selected[pick["asset_id"]] = pick
    return sorted(selected.values(), key=_story_key)[:target]


def _phash_hamming(a: object, b: object) -> int:
    """Hamming distance between two hex pHash strings (99 if either missing)."""
    if not a or not b:
        return 99
    try:
        return bin(int(str(a), 16) ^ int(str(b), 16)).count("1")
    except ValueError:
        return 99


def _same_moment(a: dict, b: dict) -> bool:
    """True if two photos are near-duplicate frames of the same moment:
    same calendar day AND pHash Hamming distance <= 14 (a near-dup, e.g. the two
    two near-identical frames of the same moment)."""
    day_a = str(a.get("best_datetime") or "")[:10]
    day_b = str(b.get("best_datetime") or "")[:10]
    if not day_a or day_a != day_b:
        return False
    return _phash_hamming(a.get("phash"), b.get("phash")) <= 14


def _first_same_moment_pair(page: Sequence[dict]) -> tuple[int, int] | None:
    for x in range(len(page)):
        for y in range(x + 1, len(page)):
            if _same_moment(page[x], page[y]):
                return (x, y)
    return None


def _conflicts_with(photo: dict, others: Iterable[dict]) -> bool:
    return any(_same_moment(photo, other) for other in others if other is not photo)


def _separate_same_moment(pages: list[list[dict]]) -> list[list[dict]]:
    """Swap photos between ADJACENT pages so no two same-moment near-duplicates
    share a page. Size-preserving 1-for-1 swaps; bounded passes; order-agnostic
    for the near-dups (their relative order does not matter). Idempotent once
    clean. Pages the caller pinned via overrides are handled by NOT calling this.
    """
    for _ in range(4):
        changed = False
        for i, page in enumerate(pages):
            pair = _first_same_moment_pair(page)
            if not pair:
                continue
            mover = page[pair[1]]
            swapped = False
            for j in (i + 1, i - 1):
                if not (0 <= j < len(pages)):
                    continue
                target = pages[j]
                rest_here = [p for p in page if p is not mover]
                for k, candidate in enumerate(target):
                    rest_there = [p for p in target if p is not candidate]
                    if _conflicts_with(candidate, rest_here):
                        continue
                    if _conflicts_with(mover, rest_there):
                        continue
                    page[pair[1]] = candidate
                    target[k] = mover
                    changed = swapped = True
                    break
                if swapped:
                    break
        if not changed:
            break
    return pages


def _default_targets(grouped: dict[str, list[dict]], total_target: int) -> dict[str, int]:
    """Keep small chapters whole and distribute the remaining budget neutrally."""
    target_total = min(total_target, sum(len(items) for items in grouped.values()))
    result = {
        chapter: len(items)
        for chapter, items in grouped.items()
        if len(items) <= 24
    }
    large = {chapter: items for chapter, items in grouped.items() if chapter not in result}
    remaining = max(0, target_total - sum(result.values()))
    if not large:
        return result
    weights = {chapter: math.sqrt(len(items)) for chapter, items in large.items()}
    weight_total = sum(weights.values())
    for chapter, items in large.items():
        result[chapter] = min(len(items), max(4, round(remaining * weights[chapter] / weight_total)))
    delta = target_total - sum(result.values())
    ordered = sorted(large, key=lambda chapter: len(large[chapter]), reverse=True)
    while delta:
        moved = False
        for chapter in ordered:
            candidate = result[chapter] + (1 if delta > 0 else -1)
            if 4 <= candidate <= len(large[chapter]):
                result[chapter] = candidate
                delta += -1 if delta > 0 else 1
                moved = True
                if delta == 0:
                    break
        if not moved:
            break
    return result


def _box(x: float, y: float, width: float, height: float) -> list[float]:
    return [round(x, 4), round(y, 4), round(width, 4), round(height, 4)]


TRACE_PATTERNS: dict[str, list[list[int]]] = {
    "single_feature": [[1]],
    # Curated directly from the owner's supplied 2/3/4/5/6-photo picker
    # screenshots. Each partition is evaluated as horizontal rows and vertical
    # columns, yielding the mirrored/asymmetric variants visible in the source.
    "two_up": [[1, 1], [2]],
    "three_up": [[1, 2], [2, 1], [3]],
    "four_up": [[2, 2], [1, 3], [3, 1]],
    "masonry_quad": [[2, 2]],
    "hero_trio": [[1, 3]],
    "five_up": [[2, 3], [3, 2], [1, 2, 2], [2, 2, 1], [1, 4], [4, 1]],
    "six_up": [[3, 3], [2, 2, 2], [1, 2, 3], [3, 2, 1], [2, 4], [4, 2]],
    # Controlled additions for the requested mega-gallery pages.
    "seven_gallery": [[3, 4], [4, 3], [2, 3, 2], [1, 3, 3], [3, 3, 1]],
    "feature_six": [[1, 3, 3], [1, 2, 2, 2]],
}


def _row_grid(aspects: Sequence[float], row_sizes: Sequence[int],
              *, title: bool) -> list[list[float]]:
    """Lay out exact-aspect photos in curated horizontal bands.

    Rows and columns in the owner's references share real edges. The former
    implementation stretched abstract cell boxes to the page frame and then
    letterboxed the actual image inside them, which created the apparently
    random margins he flagged. Here the cell itself has the source ratio.
    Dense pages distribute any remainder between bands so their outer edges
    stay disciplined; calm 2/3-photo pages keep a fixed gutter and center.
    """
    x0, y0 = M, M + (TITLE_H if title else 0.0)
    usable_w = PAGE_IN - 2 * M
    usable_h = PAGE_IN - M - y0
    rows: list[tuple[list[float], float]] = []
    cursor = 0
    for size in row_sizes:
        row_aspects = list(aspects[cursor:cursor + size])
        cursor += size
        row_h = (usable_w - G * (size - 1)) / sum(row_aspects)
        rows.append((row_aspects, row_h))

    natural_h = sum(height for _, height in rows) + G * (len(rows) - 1)
    scale = min(1.0, usable_h / max(0.0001, natural_h))
    scaled_rows = [
        (row_aspects, row_h * scale)
        for row_aspects, row_h in rows
    ]
    dense = len(aspects) >= 4
    photo_h = sum(height for _, height in scaled_rows)
    if dense and len(scaled_rows) > 1:
        row_gap = max(G, (usable_h - photo_h) / (len(scaled_rows) - 1))
        y = y0
    else:
        row_gap = G
        block_h = photo_h + row_gap * (len(scaled_rows) - 1)
        y = y0 + max(0.0, (usable_h - block_h) / 2)

    cells: list[list[float]] = []
    for row_aspects, row_h in scaled_rows:
        widths = [row_h * aspect for aspect in row_aspects]
        if dense and len(widths) > 1:
            column_gap = max(
                G,
                (usable_w - sum(widths)) / (len(widths) - 1),
            )
            x = x0
        else:
            column_gap = G
            row_w = sum(widths) + column_gap * (len(widths) - 1)
            x = x0 + max(0.0, (usable_w - row_w) / 2)
        for width in widths:
            cells.append(_box(x, y, width, row_h))
            x += width + column_gap
        y += row_h + row_gap
    return cells


def _column_grid(aspects: Sequence[float], column_sizes: Sequence[int],
                 *, title: bool) -> list[list[float]]:
    """Lay out exact-aspect photos in curated vertical masonry columns."""
    x0, y0 = M, M + (TITLE_H if title else 0.0)
    usable_w = PAGE_IN - 2 * M
    usable_h = PAGE_IN - M - y0
    columns: list[tuple[list[float], float]] = []
    cursor = 0
    for size in column_sizes:
        column_aspects = list(aspects[cursor:cursor + size])
        cursor += size
        ideal_w = (
            usable_h - G * (size - 1)
        ) / sum(1 / aspect for aspect in column_aspects)
        columns.append((column_aspects, ideal_w))

    natural_w = sum(width for _, width in columns) + G * (len(columns) - 1)
    scale = min(1.0, usable_w / max(0.0001, natural_w))
    scaled_columns = [
        (column_aspects, column_w * scale)
        for column_aspects, column_w in columns
    ]
    dense = len(aspects) >= 4
    photo_w = sum(width for _, width in scaled_columns)
    if dense and len(scaled_columns) > 1:
        column_gap = max(G, (usable_w - photo_w) / (len(scaled_columns) - 1))
        x = x0
    else:
        column_gap = G
        block_w = photo_w + column_gap * (len(scaled_columns) - 1)
        x = x0 + max(0.0, (usable_w - block_w) / 2)

    cells: list[list[float]] = []
    for column_aspects, col_w in scaled_columns:
        heights = [col_w / aspect for aspect in column_aspects]
        if dense and len(heights) > 1:
            row_gap = max(
                G,
                (usable_h - sum(heights)) / (len(heights) - 1),
            )
            y = y0
        else:
            row_gap = G
            column_h = sum(heights) + row_gap * (len(heights) - 1)
            y = y0 + max(0.0, (usable_h - column_h) / 2)
        for height in heights:
            cells.append(_box(x, y, col_w, height))
            y += height + row_gap
        x += col_w + column_gap
    return cells


def traced_layout(style: str, aspects: Sequence[float],
                  *, title: bool = False) -> list[list[float]]:
    """Choose the best-fitting orientation of one fixed reference recipe."""
    patterns = TRACE_PATTERNS.get(style)
    if not patterns:
        raise ValueError(f"Unknown traced page layout: {style}")
    best: tuple[float, list[list[float]]] | None = None
    for pattern in patterns:
        for builder in (_row_grid, _column_grid):
            cells = builder(aspects, pattern, title=title)
            used_area = sum(cell[2] * cell[3] for cell in cells)
            if best is None or used_area > best[0]:
                best = (used_area, cells)
    assert best is not None
    return best[1]


def _layout_variants(
    style: str,
    aspects: Sequence[float],
    *,
    title: bool,
) -> Iterable[tuple[str, list[list[float]]]]:
    patterns = TRACE_PATTERNS.get(style)
    if not patterns:
        raise ValueError(f"Unknown traced page layout: {style}")
    builders = (
        (_column_grid,)
        if style == "masonry_quad"
        else (_row_grid, _column_grid)
    )
    for pattern_index, pattern in enumerate(patterns):
        for builder in builders:
            orientation = "rows" if builder is _row_grid else "columns"
            yield (
                f"{orientation}:{pattern_index}",
                builder(aspects, pattern, title=title),
            )


def _layout_style(photo_count: int, *, side: str, opener: bool) -> str:
    if photo_count == 1:
        return "single_feature"
    if photo_count == 2:
        return "two_up"
    if photo_count == 3:
        return "three_up"
    if photo_count == 4:
        return "four_up"
    if photo_count == 5:
        return "five_up"
    if photo_count == 6:
        return "six_up"
    if photo_count == 7:
        return "seven_gallery"
    raise ValueError(f"No traced layout for {photo_count} photos")


def _contained_print_size(photo: dict, cell: Sequence[float]) -> tuple[float, float]:
    # Single owner of the formula lives in bookmodel; kept as a thin alias so
    # existing call sites and imports stay valid.
    return contained_print_size(photo, cell)


def _placement_dpi(photo: dict, cell: Sequence[float]) -> float:
    return effective_dpi(photo, cell, None)


def _packed_layout(
    batch: Sequence[dict],
    *,
    title: bool,
    layout_style: str,
    fixed_order: bool = False,
) -> tuple[list[dict], list[list[float]]]:
    """Assign photos to one traced recipe; never synthesize new structure."""
    candidates = _packed_candidates(
        batch,
        title=title,
        layout_style=layout_style,
        fixed_order=fixed_order,
    )
    best = max(candidates, key=lambda candidate: candidate[0])
    return best[1], best[2]


def _packed_candidates(
    batch: Sequence[dict],
    *,
    title: bool,
    layout_style: str,
    fixed_order: bool = False,
) -> list[tuple[float, list[dict], list[list[float]], str]]:
    """Return the best photo assignment for each traced recipe orientation."""
    photos = list(batch)
    # Preserve the explicit first story beat on chapter-title pages. Elsewhere
    # the permutation is neutral: maximize photographic area. Resolution is
    # reported separately and never allowed to make a mock-up sparse.
    if fixed_order:
        orders = [tuple(range(len(photos)))]
    elif title:
        orders = ((0, *tail) for tail in permutations(range(1, len(photos))))
    else:
        orders = permutations(range(len(photos)))

    best_by_recipe: dict[
        str,
        tuple[float, list[dict], list[list[float]], str],
    ] = {}
    best_sel: dict[str, float] = {}
    for order in orders:
        arranged = [photos[index] for index in order]
        aspects = [
            (photo.get("w") or 1) / max(1, photo.get("h") or 1)
            for photo in arranged
        ]
        for recipe, cells in _layout_variants(
            layout_style,
            aspects,
            title=title,
        ):
            used_area = sum(
                math.prod(_contained_print_size(photo, cell))
                for photo, cell in zip(arranged, cells)
            )
            movement = sum(abs(position - source) for position, source in enumerate(order))
            # Resolution-aware routing decides which photo goes in which cell
            # WITHIN a recipe (low-res photos -> small cells so the studio proofs
            # etc. stay printable), but must NOT distort recipe choice toward
            # low-coverage arrangements. So: rank orderings within a recipe by a
            # penalised selection score, but store the TRUE contained area as
            # candidate[0] so the facing-page joint selection maximises real
            # coverage (less white space), not the penalty.
            # Resolution routing by DPI DEFICIT (how far below the 240 floor),
            # not a count of sub-240 cells. A count can't discriminate on an
            # all-lowish-res page (every order has the same count), so the proof
            # would land in the aspect-matched BIG cell; the deficit sum instead
            # always pushes the LOWEST-resolution source into the SMALLEST cell
            # (minimizing total shortfall) — the "low-res proofs stay small"
            # guarantee. It dominates coverage + feature, so nothing enlarges a
            # low-res source past the floor when a smaller cell is available.
            dpi_deficit = sum(
                max(0.0, DPI_OK - _placement_dpi(photo, cell))
                for photo, cell in zip(arranged, cells)
            )
            # Feature selection: bias hero/favorited/family-tagged photos toward
            # a big PRINTED size (their contained area, weighted), untagged into
            # the small cells. Weighting by contained area (not raw cell area)
            # keeps the bias coverage-safe — a feature photo only "wants" a big
            # cell it actually FILLS, never a huge cell it would letterbox.
            feature_score = sum(
                _feature_weight(photo) * math.prod(_contained_print_size(photo, cell))
                for photo, cell in zip(arranged, cells)
            )
            sel_score = (
                used_area
                + 0.6 * feature_score
                - 50.0 * dpi_deficit
                - movement * 0.000001
            )
            candidate = (used_area, arranged, cells, recipe)
            if recipe not in best_sel or sel_score > best_sel[recipe]:
                best_by_recipe[recipe] = candidate
                best_sel[recipe] = sel_score

    return list(best_by_recipe.values())


def _spread_packed_layouts(
    left_batch: Sequence[dict],
    right_batch: Sequence[dict],
    *,
    title_band: bool,
    left_style: str,
    right_style: str,
    left_fixed_order: bool = False,
    right_fixed_order: bool = False,
) -> tuple[
    tuple[list[dict], list[list[float]]],
    tuple[list[dict], list[list[float]]],
]:
    """Jointly select compatible facing-page recipes.

    Independent maximization was the source of the visibly broken spreads: one
    page could choose a tall column recipe while its mate chose a wide row
    recipe. Evaluate the compact recipe sets as pairs, maximizing photographic
    area after both pages are fitted to a shared height and penalizing severe
    silhouette mismatch.
    """
    left_candidates = _packed_candidates(
        left_batch,
        title=title_band,
        layout_style=left_style,
        fixed_order=left_fixed_order,
    )
    right_candidates = _packed_candidates(
        right_batch,
        title=title_band,
        layout_style=right_style,
        fixed_order=right_fixed_order,
    )
    usable_w = PAGE_IN - 2 * M
    y_start = M + (TITLE_H if title_band else 0.0)
    usable_h = PAGE_IN - M - y_start
    spread_area = 2 * usable_w * usable_h
    best: tuple[
        float,
        tuple[float, list[dict], list[list[float]], str],
        tuple[float, list[dict], list[list[float]], str],
    ] | None = None

    for left in left_candidates:
        left_env = _cell_envelope(left[2])
        left_w = left_env[2] - left_env[0]
        left_h = left_env[3] - left_env[1]
        left_aspect = left_w / max(0.0001, left_h)
        for right in right_candidates:
            right_env = _cell_envelope(right[2])
            right_w = right_env[2] - right_env[0]
            right_h = right_env[3] - right_env[1]
            right_aspect = right_w / max(0.0001, right_h)
            common_h = min(
                usable_h,
                usable_w / max(0.0001, left_aspect),
                usable_w / max(0.0001, right_aspect),
            )
            left_scale = common_h / max(0.0001, left_h)
            right_scale = common_h / max(0.0001, right_h)
            # True post-alignment COVERAGE: photo area survives the uniform
            # scale-to-common-height and horizontal centering, so this fraction
            # is exactly the image area the finished spread will show. Making it
            # the primary driver stops the tracer from picking a
            # silhouette-matched-but-sparse recipe over a fuller one (the empty
            # bands). Silhouette mismatch stays a mild tiebreak so facing pages
            # still read as one composition.
            photo_area = left[0] * left_scale ** 2 + right[0] * right_scale ** 2
            coverage = photo_area / spread_area
            silhouette_mismatch = abs(math.log(left_aspect / right_aspect))
            orientation_bonus = 1.0 if left[3].split(":")[0] == right[3].split(":")[0] else 0.0
            # DPI DEFICIT nudge (scaled so coverage wins the common case). Within
            # each recipe the low-res sources are already routed to the small
            # cells (_packed_candidates), so most recipes carry near-zero
            # deficit; this term only bites when a recipe would UNAVOIDABLY
            # enlarge a low-res source past the floor (a big deficit), where it
            # correctly overrides coverage to keep that source small. A tiny
            # deficit (a source a hair under 240) stays negligible, so an
            # all-low-res page no longer collapses to a wide-short recipe and
            # drags its facing page down.
            dpi_deficit_pair = sum(
                max(0.0, DPI_OK - _placement_dpi(photo, cell))
                for photo, cell in (*zip(left[1], left[2]), *zip(right[1], right[2]))
            )
            score = (
                coverage
                - 0.003 * dpi_deficit_pair
                - 0.05 * silhouette_mismatch
                + 0.01 * orientation_bonus
            )
            if best is None or score > best[0]:
                best = (score, left, right)

    assert best is not None
    return (best[1][1], best[1][2]), (best[2][1], best[2][2])

def _page(batch: Sequence[dict], template_id: str, title: str | None,
          templates: dict[str, dict], *, title_band: bool,
          layout_role: str, layout_style: str,
          intentional_hero: bool = False,
          packed: tuple[list[dict], list[list[float]]] | None = None,
          fixed_order: bool = False,
          preset_source: str = "owner-supplied-archetype-library") -> dict:
    arranged, cells = packed or _packed_layout(
        batch,
        title=title_band,
        layout_style=layout_style,
        fixed_order=fixed_order,
    )
    weight = 1 if len(batch) <= 3 else 2 if len(batch) <= 5 else 3
    templates[template_id] = {
        "cells": cells,
        "weight": weight,
        "title": bool(title),
        "title_band": title_band,
        "dynamic": False,
        "default_fit": "contain",
        "layout_role": layout_role,
        "layout_style": layout_style,
        "preset_source": preset_source,
        "page_bg": OFF_WHITE,
        "cell_bg": OFF_WHITE,
    }
    return {
        "template": template_id,
        "cells": [make_cell(photo["asset_id"]) for photo in arranged],
        "story_assets": [photo["asset_id"] for photo in batch],
        "fits": ["contain"] * len(batch),
        "title": title,
        "intentional_hero": intentional_hero or (
            len(batch) == 1 and bool(batch[0].get("is_hero"))
        ),
        "layout_role": layout_role,
        "layout_style": layout_style,
    }


def _cell_envelope(cells: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    return (
        min(cell[0] for cell in cells),
        min(cell[1] for cell in cells),
        max(cell[0] + cell[2] for cell in cells),
        max(cell[1] + cell[3] for cell in cells),
    )


def _align_spread_templates(
    left_template: dict,
    right_template: dict,
    *,
    title_band: bool,
) -> None:
    """Normalize facing photo groups into one shared spread geometry.

    Page recipes still decide the internal rows and columns. This final pass
    treats the two pages as a single composition: both photo envelopes receive
    the same top and bottom baseline, and each envelope is centered between the
    fixed page margins. Uniform scaling preserves every source aspect ratio and
    every internal alignment from the traced recipe.
    """
    templates = (left_template, right_template)
    envelopes = [_cell_envelope(template["cells"]) for template in templates]
    usable_w = PAGE_IN - 2 * M
    y_start = M + (TITLE_H if title_band else 0.0)
    usable_h = PAGE_IN - M - y_start
    group_aspects = [
        (right - left) / max(0.0001, bottom - top)
        for left, top, right, bottom in envelopes
    ]
    common_h = min(
        usable_h,
        *(usable_w / max(0.0001, aspect) for aspect in group_aspects),
    )
    # Four-decimal persistence can make an otherwise square full-frame group a
    # few thousandths too wide. Snap that serialization noise back to the
    # intended shared frame instead of manufacturing a visible micro-margin.
    if usable_h - common_h <= 0.01:
        common_h = usable_h
    common_y = (
        y_start
        if title_band
        else y_start + max(0.0, (usable_h - common_h) / 2)
    )

    for template, envelope in zip(templates, envelopes):
        left, top, right, bottom = envelope
        source_w = right - left
        source_h = bottom - top
        scale = common_h / max(0.0001, source_h)
        group_w = source_w * scale
        group_x = M + max(0.0, (usable_w - group_w) / 2)
        aligned = [
            _box(
                group_x + (cell[0] - left) * scale,
                common_y + (cell[1] - top) * scale,
                cell[2] * scale,
                cell[3] * scale,
            )
            for cell in template["cells"]
        ]
        snapped: list[list[float]] = []
        for cell in aligned:
            x, y, width, height = cell
            if abs(x - M) <= 0.005:
                x = M
            if abs(y - y_start) <= 0.005:
                y = y_start
            right_edge = x + width
            bottom_edge = y + height
            if abs(right_edge - (PAGE_IN - M)) <= 0.005:
                width = PAGE_IN - M - x
            if abs(bottom_edge - (PAGE_IN - M)) <= 0.005:
                height = PAGE_IN - M - y
            snapped.append(_box(x, y, width, height))
        template["cells"] = snapped
        template["spread_aligned"] = True


def build_book(
    photos: Sequence[dict],
    *,
    chapter_order: Sequence[str],
    chapter_titles: dict[str, str],
    chapter_targets: dict[str, int] | None = None,
    chapter_settings: dict[str, dict] | None = None,
) -> dict:
    """Build an aspect-aware, full-image automatic draft from accepted photos."""
    grouped: dict[str, list[dict]] = {}
    for photo in photos:
        if photo.get("chapter"):
            grouped.setdefault(photo["chapter"], []).append(dict(photo))
    targets = chapter_targets or _default_targets(grouped, AUTO_PHOTO_TARGET)
    templates: dict[str, dict] = {}
    spreads: list[dict] = []
    settings_by_chapter = chapter_settings or {}

    for chapter in chapter_order:
        available = grouped.get(chapter, [])
        if not available:
            continue
        chapter_config = settings_by_chapter.get(chapter, {}) or {}
        page_overrides = chapter_config.get("page_overrides", {}) or {}
        include_only = set(chapter_config.get("auto_include_only", []) or [])
        excluded = set(chapter_config.get("exclude_from_auto", []) or [])
        draft_pool = [
            photo
            for photo in available
            if (not include_only or photo["asset_id"] in include_only)
            and photo["asset_id"] not in excluded
        ]
        if len(draft_pool) < 4:
            raise ValueError(
                f"{chapter} needs at least four auto-draft photos after culling"
            )
        selected = _select_evenly(
            draft_pool,
            min(
                len(draft_pool),
                targets.get(chapter, len(draft_pool)),
            ),
        )
        allocations = _story_aware_spread_counts(selected)
        # Flatten to per-page batches so the same-moment guard can move a
        # near-duplicate to an adjacent page. Skip the reorder when the owner pinned
        # pages via page_overrides (those pins are authoritative).
        page_batches: list[list[dict]] = []
        cursor = 0
        for left_count, right_count in allocations:
            page_batches.append(list(selected[cursor:cursor + left_count]))
            cursor += left_count
            page_batches.append(list(selected[cursor:cursor + right_count]))
            cursor += right_count
        if not page_overrides:
            _separate_same_moment(page_batches)
        page_index = 0
        for chapter_spread_index, (left_count, right_count) in enumerate(allocations):
            left_batch = page_batches[chapter_spread_index * 2]
            right_batch = page_batches[chapter_spread_index * 2 + 1]
            left_count = len(left_batch)
            right_count = len(right_batch)
            opener = chapter_spread_index == 0
            title = chapter_titles.get(chapter, chapter) if opener else None
            left_page_number = page_index + 1
            right_page_number = page_index + 2
            left_id = f"{chapter}__page_{left_page_number:02d}"
            right_id = f"{chapter}__page_{right_page_number:02d}"
            page_index += 2

            left_override = page_overrides.get(str(left_page_number), {}) or {}
            right_override = page_overrides.get(str(right_page_number), {}) or {}
            selected_by_id = {
                photo["asset_id"]: photo
                for photo in (*left_batch, *right_batch)
            }

            def overridden_batch(
                batch: Sequence[dict],
                override: dict,
                page_number: int,
            ) -> list[dict]:
                asset_ids = override.get("assets") or []
                if not asset_ids:
                    return list(batch)
                if len(asset_ids) != len(batch) or set(asset_ids) != {
                    photo["asset_id"] for photo in batch
                }:
                    raise ValueError(
                        f"{chapter} page {page_number} override assets must "
                        "match that page's selected story assets"
                    )
                return [selected_by_id[asset_id] for asset_id in asset_ids]

            left_batch = overridden_batch(
                left_batch, left_override, left_page_number
            )
            right_batch = overridden_batch(
                right_batch, right_override, right_page_number
            )
            left_fixed = bool(left_override.get("assets"))
            right_fixed = bool(right_override.get("assets"))

            left_title = title
            right_title = None
            if left_override.get("show_title") is False:
                left_title = None
            if right_override.get("show_title") is True:
                left_title = None
                right_title = title
            if right_override.get("show_title") is False:
                right_title = None
            title_band = bool(left_title or right_title)

            left_style = left_override.get("layout_style") or _layout_style(
                left_count, side="left", opener=opener
            )
            right_style = right_override.get("layout_style") or _layout_style(
                right_count, side="right", opener=opener
            )
            left_packed, right_packed = _spread_packed_layouts(
                left_batch,
                right_batch,
                title_band=title_band,
                left_style=left_style,
                right_style=right_style,
                left_fixed_order=left_fixed,
                right_fixed_order=right_fixed,
            )
            left = _page(
                left_batch,
                left_id,
                left_title,
                templates,
                title_band=title_band,
                layout_role=_layout_role(left_count),
                layout_style=left_style,
                packed=left_packed,
                fixed_order=left_fixed,
            )
            right = _page(
                right_batch,
                right_id,
                right_title,
                templates,
                title_band=title_band,
                layout_role=_layout_role(right_count),
                layout_style=right_style,
                packed=right_packed,
                fixed_order=right_fixed,
            )
            _align_spread_templates(
                templates[left_id],
                templates[right_id],
                title_band=title_band,
            )
            archetype = _spread_archetype(
                left_batch,
                right_batch,
                chapter_opener=opener,
            )
            spreads.append({
                "id": uuid.uuid4().hex,
                "kind": "chapter_opener" if opener else "content",
                "chapter": chapter,
                "archetype": archetype,
                "archetype_purpose": SPREAD_ARCHETYPES[archetype]["purpose"],
                "left": left,
                "right": right,
            })

    book = {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "book_revision": 0,
            "geometry": geometry_meta(),
            "trim_in": PAGE_IN,
            "bleed_in": 0.125,
            "bg": OFF_WHITE,
            "dpi_min": DPI_MIN,
            "dpi_floor": DPI_OK,
            "default_fit": "contain",
            "implicit_crop_allowed": False,
            "archetype_source": "owner-supplied-2-through-6-photo-picker-screenshots",
            "auto_photo_target": sum(len(page["cells"]) for spread in spreads
                                     for page in (spread["left"], spread["right"])),
        },
        "templates": templates,
        "spreads": spreads,
    }
    TEMPLATES.clear()
    TEMPLATES.update(templates)
    return book


def valid_pair(left_template: str, right_template: str,
               templates: dict[str, dict] | None = None) -> bool:
    source = templates or TEMPLATES
    if left_template not in source or right_template not in source:
        return False
    left_weight = source[left_template].get("weight", 2)
    right_weight = source[right_template].get("weight", 2)
    return not (left_weight == 3 and right_weight == 3)


def validate_book(book: dict, photo_by_id: dict[str, dict]) -> dict:
    """Return adversarial page-level quality failures for a generated draft."""
    blank_pages: list[int] = []
    sparse_pages: list[int] = []
    implicit_crop_cells: list[dict] = []
    aspect_mismatch_cells: list[dict] = []
    low_dpi_cells: list[dict] = []
    duplicate_assets: list[str] = []
    invalid_pairs: list[int] = []
    era_outliers: list[dict] = []
    seen: set[str] = set()

    for spread_index, spread in enumerate(book.get("spreads", [])):
        left_id, right_id = spread["left"]["template"], spread["right"]["template"]
        if not valid_pair(left_id, right_id, book["templates"]):
            invalid_pairs.append(spread_index)
        chapter = spread.get("chapter")
        window = ERA_WINDOWS.get(chapter)
        for side_index, side in enumerate(("left", "right")):
            page_number = spread_index * 2 + side_index + 1
            page = spread[side]
            cells = book["templates"][page["template"]]["cells"]
            assets = page.get("cells", [])
            fits = page.get("fits", [])
            if not assets:
                blank_pages.append(page_number)
                continue
            if len(assets) == 1 and not page.get("intentional_hero"):
                sparse_pages.append(page_number)
            if len(cells) != len(assets):
                aspect_mismatch_cells.append({
                    "page": page_number,
                    "reason": "cell-count",
                    "cells": len(cells),
                    "assets": len(assets),
                })
            for cell_index, (entry, cell) in enumerate(zip(assets, cells)):
                asset_id = cell_asset(entry)
                if asset_id is None:
                    continue
                if asset_id in seen:
                    duplicate_assets.append(asset_id)
                seen.add(asset_id)
                fit = fits[cell_index] if cell_index < len(fits) else None
                if fit != "contain":
                    implicit_crop_cells.append({
                        "page": page_number,
                        "cell": cell_index,
                        "asset_id": asset_id,
                        "fit": fit,
                    })
                photo = photo_by_id[asset_id]
                if window and _date_trust(photo) == "authoritative":
                    day = str(photo.get("best_datetime") or "")[:10]
                    if day and (day < window[0] or day > window[1]):
                        era_outliers.append({
                            "page": page_number,
                            "chapter": chapter,
                            "asset_id": asset_id,
                            "date": day,
                            "date_source": photo.get("date_source"),
                            "date_confidence": photo.get("date_confidence"),
                        })
                dpi = _placement_dpi(photo, cell)
                # Persisted cell geometry is rounded to four decimals, so an
                # exact 240-DPI fit can re-evaluate as 239.999... . Preserve
                # the hard floor while tolerating that serialization noise.
                if dpi + 0.1 < DPI_OK:
                    low_dpi_cells.append({
                        "page": page_number,
                        "cell": cell_index,
                        "asset_id": asset_id,
                        "dpi": round(dpi, 1),
                    })

    return {
        "blank_pages": blank_pages,
        "sparse_pages": sparse_pages,
        "implicit_crop_cells": implicit_crop_cells,
        "aspect_mismatch_cells": aspect_mismatch_cells,
        "low_dpi_cells": low_dpi_cells,
        "duplicate_assets": sorted(set(duplicate_assets)),
        "invalid_pairs": invalid_pairs,
        "era_outliers": era_outliers,
    }


def _chapter_config() -> tuple[list[str], dict[str, str], dict[str, int]]:
    data = yaml.safe_load(paths.read_config(paths.CHAPTERS_YAML)) or {}
    chapters = data.get("chapters", []) or []
    order = [chapter["id"] for chapter in chapters]
    titles = {chapter["id"]: chapter.get("title", chapter["id"]) for chapter in chapters}
    targets = {str(key): int(value) for key, value in (data.get("targets") or {}).items()}
    return order, titles, targets


def _story_config() -> dict:
    if not STORY_YAML.is_file():
        return {}
    return yaml.safe_load(STORY_YAML.read_text(encoding="utf-8")) or {}


def _normal_label(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _date_value(value: object) -> tuple[int, str]:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return 0, parsed.isoformat()
    except ValueError:
        return 1, text or "9999"


def _clamped_sort_dt(photo: dict, window: tuple[str, str] | None) -> str:
    """Trust-aware sort date for within-chapter ordering.

    Authoritative dates (EXIF/sidecar/url/filename-embedded/event) pass through
    and sort chronologically. Synthetic (filename_seq) and fallback (mtime/
    path-year/friend-estimate) dates are ORDINAL-clamped into the chapter's era
    window, so they can never sort ahead of the era/birth anchor (front) nor
    leapfrog a real later beat — the exact "toddler before baby" failure.
    """
    raw = str(photo.get("best_datetime") or "")
    if _date_trust(photo) == "authoritative" or not window:
        return raw or "9999"
    start, end = window
    day = raw[:10]
    if not day:
        return start
    if day < start:
        return start
    if day > end:
        return end
    return raw


def _filename_group(photo: dict) -> str | None:
    """Return a conservative descriptive filename family, never IMG/PXL noise."""
    stem = Path(str(photo.get("orig_path") or "")).stem
    normalized = _normal_label(stem)
    if not normalized or re.match(
        r"^(img|pxl|dsc|signal|whatsapp|image|photo|screenshot|[0-9])",
        normalized,
    ):
        return None
    words = [
        word
        for word in normalized.split("-")
        if word and not word.isdigit()
    ]
    return "-".join(words[:3]) or None


def _apply_story_config(photos: list[dict], config: dict) -> None:
    """Assign a durable story order from explicit pins plus metadata clusters."""
    chapter_settings = config.get("chapters", {}) or {}
    by_chapter: dict[str, list[dict]] = {}
    for photo in photos:
        by_chapter.setdefault(photo["chapter"], []).append(photo)

    for chapter, chapter_photos in by_chapter.items():
        settings = chapter_settings.get(chapter, {}) or {}
        window = ERA_WINDOWS.get(chapter)
        pinned = {
            asset_id: index
            for index, asset_id in enumerate(settings.get("pinned_first", []) or [])
        }
        page_groups = {
            asset_id: f"page:{page_number}"
            for page_number, page in (settings.get("page_overrides", {}) or {}).items()
            for asset_id in (page.get("assets") or [])
        }
        source_order = {
            _normal_label(source): index
            for index, source in enumerate(settings.get("source_order", []) or [])
        }
        aliases: dict[str, str] = {}
        for canonical, values in (settings.get("event_aliases", {}) or {}).items():
            normalized_canonical = _normal_label(canonical)
            aliases[normalized_canonical] = normalized_canonical
            for value in values or []:
                aliases[_normal_label(value)] = normalized_canonical
        priority_events = [
            aliases.get(_normal_label(value), _normal_label(value))
            for value in settings.get("priority_events", []) or []
        ]
        priority_tags = [
            _normal_label(value)
            for value in settings.get("priority_tags", []) or []
        ]

        clustered: dict[str, list[dict]] = {}
        for photo in chapter_photos:
            if photo["asset_id"] in pinned:
                photo["story_order"] = pinned[photo["asset_id"]]
                photo["story_group"] = page_groups.get(
                    photo["asset_id"], "pinned"
                )
                photo["story_pinned"] = True
                continue

            event = _normal_label(photo.get("event"))
            event = aliases.get(event, event)
            tags = [
                _normal_label(tag)
                for tag in str(photo.get("tags") or "").split(",")
                if _normal_label(tag)
            ]
            priority_tag = next(
                (tag for tag in priority_tags if tag in tags),
                None,
            )
            source = _normal_label(photo.get("src"))
            year = str(photo.get("best_datetime") or "unknown")[:4]
            caption = _normal_label(photo.get("caption"))
            filename = _filename_group(photo)

            if event:
                story_group = f"event:{event}"
            elif priority_tag:
                story_group = f"tag:{priority_tag}"
            elif source_order:
                story_group = f"source:{source}"
            elif tags:
                story_group = f"tag:{tags[0]}:{year}"
            elif caption:
                story_group = f"caption:{caption[:36]}:{year}"
            elif filename:
                story_group = f"filename:{filename}:{year}"
            else:
                month = str(photo.get("best_datetime") or "unknown")[:7]
                story_group = f"month:{month}"

            photo["story_group"] = story_group
            clustered.setdefault(story_group, []).append(photo)

        def group_key(item: tuple[str, list[dict]]) -> tuple:
            group, members = item
            kind, _, label = group.partition(":")
            canonical = label.split(":", 1)[0]
            if kind == "event" and canonical in priority_events:
                return 0, priority_events.index(canonical), (0, "")
            if kind == "tag" and canonical in priority_tags:
                return 1, priority_tags.index(canonical), (0, "")
            if kind == "source" and canonical in source_order:
                return 2, source_order[canonical], (0, "")
            earliest = min(
                (_date_value(_clamped_sort_dt(member, window)) for member in members),
                default=(1, "9999"),
            )
            return 3, 0, earliest

        cursor = 1000
        for _, members in sorted(clustered.items(), key=group_key):
            for photo in sorted(
                members,
                key=lambda member: (
                    _date_value(_clamped_sort_dt(member, window)),
                    member["asset_id"],
                ),
            ):
                photo["story_order"] = cursor
                cursor += 1


def _load(db_path: Path | None = None) -> list[dict]:
    with db.session(db_path) as conn:
        rows = conn.execute(
            "SELECT asset_id,chapter,event,tags,is_hero,favorited,score,w,h,"
            "best_datetime,date_source,date_confidence,src,caption,orig_path,phash "
            "FROM assets WHERE review_status='accept' AND chapter IS NOT NULL"
        ).fetchall()
    return [dict(row) for row in rows]


def _guard_overwrite(force: bool) -> None:
    """Refuse to clobber an EDITED book (book_revision > 0) without --force, and
    snapshot the outgoing book into a logs/book_revisions/ ring first."""
    if not BOOK_JSON.is_file():
        return
    try:
        existing = json.loads(BOOK_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    revision = int(existing.get("meta", {}).get("book_revision", 0) or 0)
    # Snapshot every existing book before overwrite (crash/mistake recovery).
    ring = paths.LOGS_DIR / "book_revisions"
    ring.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    (ring / f"book_rev{revision:04d}_{stamp}.json").write_text(
        json.dumps(existing, indent=2), encoding="utf-8"
    )
    if revision > 0 and not force:
        raise ValueError(
            f"Refusing to overwrite an edited book (book_revision={revision}). "
            f"A snapshot was saved to {ring}. Re-run with force=True to replace it."
        )


def compose(db_path: Path | None = None, *, force: bool = False) -> dict:
    _guard_overwrite(force)
    chapter_order, titles, configured_targets = _chapter_config()
    photos = _load(db_path)
    story_config = _story_config()
    _apply_story_config(photos, story_config)
    grouped: dict[str, list[dict]] = {}
    for photo in photos:
        grouped.setdefault(photo["chapter"], []).append(photo)
    targets = configured_targets or _default_targets(grouped, AUTO_PHOTO_TARGET)
    book = build_book(
        photos,
        chapter_order=chapter_order,
        chapter_titles=titles,
        chapter_targets=targets,
        chapter_settings=story_config.get("chapters", {}) or {},
    )
    photo_by_id = {photo["asset_id"]: photo for photo in photos}
    report = validate_book(book, photo_by_id)
    # Mockups may display a low-resolution source at its intended geometry so
    # the owner can judge composition. print_ready is DERIVED (crop-aware, honoring
    # per-cell dpi_override), never hand-set; the exporter recomputes it too.
    from .bookmodel import derive_print_ready

    print_ready, low_dpi = derive_print_ready(book, photo_by_id)
    book["meta"]["print_ready"] = print_ready
    book["meta"]["low_dpi_mockup_cells"] = len(low_dpi)
    if any(report[key] for key in (
        "blank_pages",
        "sparse_pages",
        "implicit_crop_cells",
        "aspect_mismatch_cells",
        "duplicate_assets",
        "invalid_pairs",
    )):
        raise ValueError(f"Generated book failed quality contract: {report}")
    BOOK_JSON.write_text(json.dumps(book, indent=2), encoding="utf-8")
    placed = sum(len(spread[side]["cells"]) for spread in book["spreads"]
                 for side in ("left", "right"))
    return {
        "spreads": len(book["spreads"]),
        "pages": len(book["spreads"]) * 2,
        "eras": len({spread["chapter"] for spread in book["spreads"]}),
        "photos_placed": placed,
        "photos_unplaced": len(photos) - placed,
        "avg_per_page": round(placed / max(1, len(book["spreads"]) * 2), 1),
        "layflat_ok": len(book["spreads"]) * 2 <= 122,
        "print_ready": book["meta"]["print_ready"],
        "targets": targets,
        "quality": report,
        "book_json": str(BOOK_JSON),
    }


if __name__ == "__main__":
    print(json.dumps(compose(), indent=2))
