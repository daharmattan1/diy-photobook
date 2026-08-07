"""Self-contained tests for the print-geometry core — no manifest, no photos.

These cover the pieces that make a book print correctly: the DPI formula, the
print-ready gate, the aspect-preserving page layout, and the tolerant cell
accessors. They need no data, so they run anywhere (`pytest -q`).
"""
from __future__ import annotations

from photobook.bookmodel import (
    DPI_OK, PAGE_IN, cell_asset, cell_crop, derive_print_ready,
    effective_dpi, make_cell,
)
from photobook.bookdef import aspect_layout


def test_geometry_constants_are_sane():
    assert PAGE_IN == 12.0
    assert DPI_OK == 240


def test_effective_dpi_full_page_square():
    # a 3600px square photo in a ~11.76in square cell -> ~306 DPI (clears floor)
    photo = {"w": 3600, "h": 3600}
    cell = [0.12, 0.12, 11.76, 11.76]
    dpi = effective_dpi(photo, cell)
    assert 290 < dpi < 320
    assert dpi >= DPI_OK


def test_effective_dpi_crop_reduces_resolution():
    photo = {"w": 3600, "h": 3600}
    cell = [0.12, 0.12, 11.76, 11.76]
    full = effective_dpi(photo, cell)
    half = effective_dpi(photo, cell, {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5})
    assert half < full  # cropping to half throws away half the pixels


def test_effective_dpi_zero_for_missing_dims():
    assert effective_dpi({"w": 0, "h": 0}, [0, 0, 6, 6]) == 0.0


def test_derive_print_ready_flags_low_dpi_cell():
    # a tiny 200px photo placed full-page is far below the 240 floor
    book = {
        "templates": {"full": {"cells": [[0.12, 0.12, 11.76, 11.76]]}},
        "spreads": [{
            "left": {"template": "full", "cells": [make_cell("a1")]},
            "right": {"template": "full", "cells": [make_cell("a2")]},
        }],
    }
    photos = {"a1": {"w": 200, "h": 200}, "a2": {"w": 3600, "h": 3600}}
    ready, low = derive_print_ready(book, photos)
    assert ready is False
    assert any(c["asset_id"] == "a1" for c in low)
    assert all(c["asset_id"] != "a2" for c in low)


def test_derive_print_ready_passes_when_all_high_dpi():
    book = {
        "templates": {"full": {"cells": [[0.12, 0.12, 11.76, 11.76]]}},
        "spreads": [{
            "left": {"template": "full", "cells": [make_cell("a1")]},
            "right": {"template": "full", "cells": [make_cell("a2")]},
        }],
    }
    photos = {"a1": {"w": 3600, "h": 3600}, "a2": {"w": 3600, "h": 3600}}
    ready, low = derive_print_ready(book, photos)
    assert ready is True
    assert low == []


def test_cell_accessors_are_tolerant():
    assert cell_asset("bare-id") == "bare-id"
    assert cell_asset(None) is None
    assert cell_asset({"asset_id": "x"}) == "x"
    assert cell_crop({"asset_id": "x", "crop": {"x": 0, "y": 0, "w": 1, "h": 1}}) == \
        {"x": 0, "y": 0, "w": 1, "h": 1}
    assert cell_crop("bare-id") is None


def test_aspect_layout_returns_non_overlapping_cells():
    cells = aspect_layout([1.5, 0.66, 1.0])   # three photos, mixed aspects
    assert len(cells) == 3
    for x, y, w, h in cells:
        assert w > 0 and h > 0
        assert 0 <= x and 0 <= y
        assert x + w <= PAGE_IN + 1e-6
        assert y + h <= PAGE_IN + 1e-6


def test_aspect_layout_rejects_too_many_photos():
    import pytest
    with pytest.raises(ValueError):
        aspect_layout([1.0] * 6)   # max 5 per page
