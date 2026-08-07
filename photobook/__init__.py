"""Family Photo Book Pipeline.

A local, zero-cloud pipeline that aggregates 9 years of scattered family
photos, repairs timestamps (manifest-only), de-duplicates, quality-filters,
chapters chronologically, and produces browser contact sheets for human
curation — exporting a service-agnostic print bundle.

Non-negotiables:
  - Manifest-driven (SQLite is the single source of truth).
  - Non-destructive: nothing is deleted; dup/reject are flags.
  - Idempotent / resumable per stage.
  - Originals are NEVER mutated. EXIF is only ever written onto exported COPIES.
"""

__version__ = "0.1.0"
