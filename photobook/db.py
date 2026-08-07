"""SQLite manifest — the single source of truth for the whole pipeline.

One row per unique file (`asset_id` = sha1 of the file's bytes). Every stage
reads and writes columns on this table; nothing about a photo lives outside it
except the image bytes themselves (in staging/).

Design rules baked in here:
  - `asset_id` is content-addressed (sha1 of bytes) → exact-byte dups collapse
    for free (same bytes → same id → one row).
  - `staged_original_path` and `derivative_path` are SEPARATE columns. Scoring,
    dedup, quality, and review read the 2560px *derivative*; export copies the
    full-res *original*. Conflating them was a cross-model-review finding.
  - `sidecar_taken_time` is captured AT INGEST (Phase 2), before by-hash
    flattening severs the file↔Takeout-JSON link.
  - Upsert is idempotent by `asset_id` (INSERT OR IGNORE + targeted UPDATE),
    so re-running ingest adds zero rows.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import paths

# Column set for the assets table. Keep in sync with SCHEMA below.
SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id             TEXT PRIMARY KEY,   -- sha1 of file bytes
    src                  TEXT,               -- logical source name (thumbdrive, google, ...)
    orig_path            TEXT,               -- absolute path in the ORIGINAL source tree
    staged_original_path TEXT,               -- copy of the original under staging/originals (NULL in --lean mode)
    derivative_path      TEXT,               -- <=2560px sRGB JPEG under staging/derivatives
    zip_source           TEXT,               -- (--lean) the ZIP part this original lives in
    zip_entry            TEXT,               -- (--lean) the entry name inside that ZIP
    sidecar_taken_time   TEXT,               -- ISO datetime from Takeout JSON, captured at ingest
    favorited            INTEGER,            -- 1 = starred in Google Photos (from sidecar); strong curation signal
    people_count         INTEGER,            -- # of face-tagged people in the sidecar (>=2 => group photo)
    ingest_batch         TEXT,               -- label of the ingest run that first added this row (net-new review)
    event                TEXT,               -- named event/moment (e.g. "A Friend's Wedding, 2019") - narrative grouping + date anchor
    tags                 TEXT,               -- comma-separated MULTI-tags (e.g. holidays,kids,a-place) - overlapping themes; a photo can have several
    mime                 TEXT,
    w                    INTEGER,            -- original pixel width
    h                    INTEGER,            -- original pixel height
    bytes                INTEGER,            -- original file size in bytes
    best_datetime        TEXT,               -- resolved ISO datetime (Phase 4)
    date_source          TEXT,               -- exif|sidecar|filename|mtime (Phase 4)
    date_confidence      TEXT,               -- high|medium|low (Phase 4)
    phash                TEXT,               -- 64-bit perceptual hash hex (Phase 5)
    dup_group            INTEGER,            -- dedup group id (Phase 5)
    is_dup_keep          INTEGER,            -- 1 = keeper of its dup_group, 0 = duplicate (Phase 5)
    blur_var             REAL,               -- variance-of-Laplacian (Phase 6)
    is_screenshot        INTEGER,            -- advisory flag (Phase 6)
    is_document          INTEGER,            -- advisory flag (Phase 6)
    quality_pass         INTEGER,            -- advisory 1/0 (Phase 6)
    score                REAL,               -- curation score (Phase 7)
    chapter              TEXT,               -- assigned chapter id (Phase 6)
    review_status        TEXT,               -- pending|accept|reject (Phase 7/9)
    is_hero              INTEGER,            -- 1 = hero pick (Phase 9)
    caption              TEXT,               -- optional per-photo caption (Phase 9)
    ingested_at          TEXT DEFAULT (datetime('now'))
);
"""

# Helpful indexes for the query patterns each phase uses.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_assets_src        ON assets(src);",
    "CREATE INDEX IF NOT EXISTS idx_assets_best_dt     ON assets(best_datetime);",
    "CREATE INDEX IF NOT EXISTS idx_assets_dup_group   ON assets(dup_group);",
    "CREATE INDEX IF NOT EXISTS idx_assets_chapter     ON assets(chapter);",
    "CREATE INDEX IF NOT EXISTS idx_assets_review      ON assets(review_status);",
)

# Columns an ingest row may set. Used to build the idempotent upsert.
_INGEST_COLUMNS = (
    "asset_id", "src", "orig_path", "staged_original_path", "derivative_path",
    "zip_source", "zip_entry", "sidecar_taken_time", "favorited", "people_count",
    "ingest_batch", "mime", "w", "h", "bytes",
)

# Columns added after the initial schema shipped. init_db() ADDs any that are
# missing, so an existing manifest.db upgrades in place (SQLite has no
# ADD COLUMN IF NOT EXISTS, so we check PRAGMA table_info first).
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("favorited", "ALTER TABLE assets ADD COLUMN favorited INTEGER"),
    ("people_count", "ALTER TABLE assets ADD COLUMN people_count INTEGER"),
    ("ingest_batch", "ALTER TABLE assets ADD COLUMN ingest_batch TEXT"),
    ("event", "ALTER TABLE assets ADD COLUMN event TEXT"),
    ("tags", "ALTER TABLE assets ADD COLUMN tags TEXT"),
)


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (creating if needed) the manifest DB with sane pragmas."""
    db_path = db_path or paths.MANIFEST_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Create the schema + indexes if they do not exist, and apply any pending
    additive column migrations. Idempotent."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        for stmt in INDEXES:
            conn.execute(stmt)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(assets)")}
        for col, ddl in _MIGRATIONS:
            if col not in existing:
                conn.execute(ddl)
        conn.commit()


@contextmanager
def session(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection that commits on success, closes always."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_asset(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """Insert an ingest row if the asset_id is new; otherwise leave it be.

    Returns True if a NEW row was inserted, False if the asset_id already
    existed (idempotency). We deliberately do NOT overwrite processing columns
    (best_datetime, phash, review_status, ...) on re-ingest — those belong to
    later stages and must survive a re-run of ingest.
    """
    cols = [c for c in _INGEST_COLUMNS if c in row]
    placeholders = ", ".join("?" for _ in cols)
    collist = ", ".join(cols)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO assets ({collist}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )
    return cur.rowcount == 1


def count_assets(conn: sqlite3.Connection, where: str = "") -> int:
    sql = "SELECT count(*) AS n FROM assets"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql).fetchone()["n"])


def counts_by_source(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT src, count(*) AS n FROM assets GROUP BY src ORDER BY src"
    ).fetchall()
    return [(r["src"], r["n"]) for r in rows]
