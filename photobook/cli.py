"""photobook CLI — the single entrypoint for every pipeline stage.

A click.group() with one subcommand per stage. Run stages in order:

    photobook doctor                                  # env health gate
    photobook ingest --src <dir> --source-name <name> # build manifest
    photobook daterepair                              # manifest-only dates
    photobook dedup                                   # collapse near-dups
    photobook quality                                 # advisory junk flags
    photobook chapter --validate                      # assert contiguity
    photobook chapter                                 # assign chapters
    photobook score                                   # rank shortlist
    photobook review                                  # emit contact sheets
    photobook decisions --import <decisions.json>     # import picks
    photobook export                                  # service bundle
    photobook editor / export_book                    # lay out + render the book
"""

from __future__ import annotations

import sys

import click

# Windows consoles often default to a legacy codepage (cp1252) that cannot
# encode every character a report may contain (e.g. box-drawing in a wrapped
# third-party error message). Degrade to '?' instead of crashing with
# UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass


@click.group()
@click.version_option(package_name=None, message="photobook %(version)s")
def cli() -> None:
    """Family Photo Book Pipeline — local, zero-cloud, manifest-driven."""


# ---------------------------------------------------------------- Phase 1 ----
@cli.command()
@click.option(
    "--no-log",
    is_flag=True,
    default=False,
    help="Do not write logs/doctor.txt (still prints the report).",
)
def doctor(no_log: bool) -> None:
    """Verify every hard dependency works on this interpreter."""
    from . import doctor as doctor_mod

    code = doctor_mod.run_doctor(write_log=not no_log)
    raise SystemExit(code)


# ---------------------------------------------------------------- Phase 2/3 --
@cli.command()
@click.option("--src", required=True, help="Source directory to ingest (recursive).")
@click.option("--source-name", required=True, help="Logical source name (e.g. thumbdrive).")
@click.option("--batch", default=None,
              help="Label stamped on rows this run adds (net-new review via review --batch).")
def ingest(src: str, source_name: str, batch: str) -> None:
    """Copy originals + derivatives into staging and write manifest rows."""
    from pathlib import Path

    from . import funnel
    from . import ingest as ingest_mod

    src_path = Path(src)
    if not src_path.is_dir():
        click.echo(f"[photobook] --src is not a directory: {src_path}", err=True)
        raise SystemExit(2)

    click.echo(f"[photobook] ingesting {src_path} as source '{source_name}'"
               + (f" [batch={batch}]" if batch else "") + " ...")
    stats = ingest_mod.ingest_dir(src_path, source_name, batch=batch)
    click.echo(stats.summary_line())
    funnel.append(f"[ingest]  {stats.summary_line()}")
    if stats.failures:
        click.echo(f"[photobook] {len(stats.failures)} file(s) failed to ingest:", err=True)
        for f in stats.failures[:20]:
            click.echo(f"    - {f}", err=True)
        if len(stats.failures) > 20:
            click.echo(f"    ... and {len(stats.failures) - 20} more", err=True)


# ---------------------------------------------------------------- Phase 4 ----
@cli.command()
def daterepair() -> None:
    """Resolve best_datetime for every asset (manifest-only; no writeback)."""
    from . import daterepair as dr

    summary = dr.repair()
    click.echo("[photobook] date repair complete (manifest-only, originals untouched):")
    click.echo(f"    by source      : {summary['by_source']}")
    click.echo(f"    by confidence  : {summary['by_confidence']}")
    click.echo(f"    low-confidence : {summary['low_confidence_count']}")
    click.echo(f"    exiftool used  : {summary['exiftool_used']}")
    if not summary["exiftool_used"]:
        click.echo("    [warn] exiftool not resolved - EXIF dates skipped, fell back to sidecar/filename/mtime", err=True)


# ---------------------------------------------------------------- Phase 5 ----
@cli.command()
def dedup() -> None:
    """Group near-duplicates and mark one keeper per group (nothing deleted)."""
    from . import dedup as dedup_mod

    summary = dedup_mod.dedup()
    click.echo("[photobook] dedup complete (nothing deleted; duplicates flagged):")
    click.echo(f"    total assets       : {summary['total']}")
    click.echo(f"    kept (is_dup_keep=1): {summary['kept']}")
    click.echo(f"    dup-flagged        : {summary['dup_flagged']}")
    click.echo(f"    multi-member groups: {summary['multi_member_groups']}")


# ---------------------------------------------------------------- Phase 6 ----
@cli.command()
def quality() -> None:
    """Advisory quality flags (blur/dark/screenshot/document)."""
    from . import quality as quality_mod

    summary = quality_mod.quality()
    click.echo("[photobook] quality gate complete (advisory; nothing deleted):")
    click.echo(f"    quality_pass : {summary['quality_pass']}/{summary['total']}")
    click.echo(f"    screenshot   : {summary['screenshot']}")
    click.echo(f"    document     : {summary['document']}")
    click.echo(f"    blurry       : {summary['blurry']}")
    click.echo(f"    dark/blown   : {summary['dark_blown']}")


@cli.command()
@click.option("--validate", "validate_only", is_flag=True, default=False,
              help="Only validate contiguity, then exit (0=ok, 1=problems).")
def chapter(validate_only: bool) -> None:
    """Validate chapter contiguity and/or assign a chapter to each asset."""
    from . import chapter as chapter_mod

    if validate_only:
        ok, problems = chapter_mod.validate()
        if ok:
            click.echo("[photobook] chapters valid: contiguous, non-overlapping.")
            raise SystemExit(0)
        click.echo("[photobook] chapter validation FAILED:", err=True)
        for p in problems:
            click.echo(f"    - {p}", err=True)
        raise SystemExit(1)

    summary = chapter_mod.assign()
    click.echo(f"[photobook] chaptered {summary['assigned']} assets across {summary['chapters']} chapters:")
    for cid, n in summary["per_chapter"].items():
        click.echo(f"    {cid:24} {n}")
    if summary["clamped_low"] or summary["clamped_high"]:
        click.echo(f"    (clamped: {summary['clamped_low']} before first, {summary['clamped_high']} after last)")


# ---------------------------------------------------------------- Phase 7 ----
@cli.command()
@click.option("--target", default=200, show_default=True,
              help="Approx total accepted-photo target across all chapters.")
def score(target: int) -> None:
    """Compute curation scores and mark a per-chapter shortlist."""
    from . import score as score_mod

    summary = score_mod.score(total_target=target)
    click.echo("[photobook] scoring complete; shortlist marked review_status='pending':")
    click.echo(f"    eligible keepers : {summary['eligible_keepers']}")
    click.echo(f"    shortlist total  : {summary['shortlist_total']}")
    click.echo(f"    per chapter      : {summary['per_chapter']}")


@cli.command()
@click.option("--all-keepers", is_flag=True, default=False,
              help="Include ALL dedup keepers, not just the scored shortlist.")
@click.option("--batch", default=None,
              help="NET-NEW review: show ONLY photos introduced by the ingest run with this "
                   "batch label (e.g. 'parts-003-004'). Skips everything you already reviewed.")
@click.option("--batch-all", is_flag=True, default=False,
              help="With --batch: show EVERY quality-passing photo in the batch (not just the "
                   "scored shortlist). Use for a curated source that would otherwise lose "
                   "shortlist slots to a large same-period pile (e.g. blog travel photos).")
def review(all_keepers: bool, batch: str, batch_all: bool) -> None:
    """Generate per-chapter contact-sheet HTML for human review."""
    from . import contactsheet

    summary = contactsheet.generate(only_shortlist=not all_keepers, batch=batch,
                                    batch_all=batch_all)
    if batch:
        click.echo(f"[photobook] NET-NEW review for batch '{batch}':")
    click.echo(f"[photobook] wrote {summary['chapters_written']} contact sheet(s):")
    for f in summary["files"]:
        click.echo(f"    {f}")
    click.echo(f"    index: {summary['index']}")
    click.echo("Open the index in your browser, curate, and Export decisions per chapter.")


# ---------------------------------------------------------------- Phase 9 ----
@cli.command()
@click.option("--import", "import_path", required=True,
              help="Path to a downloaded decisions.json, or a directory of them.")
@click.option("--no-bound", is_flag=True, default=False,
              help="Skip the [150,250] accepted-count guardrail (for small/test sets).")
@click.option("--min-accept", default=150, show_default=True)
@click.option("--max-accept", default=250, show_default=True)
def decisions(import_path: str, no_bound: bool, min_accept: int, max_accept: int) -> None:
    """Import the owner's downloaded decisions.json into the manifest."""
    from pathlib import Path

    from . import decisions as dec

    try:
        summary = dec.import_decisions(
            Path(import_path), min_accept=min_accept, max_accept=max_accept,
            enforce_bound=not no_bound,
        )
    except dec.BoundError as e:
        click.echo(f"[photobook] IMPORT REJECTED: {e}", err=True)
        raise SystemExit(1)
    except FileNotFoundError as e:
        click.echo(f"[photobook] {e}", err=True)
        raise SystemExit(2)

    click.echo("[photobook] decisions imported:")
    click.echo(f"    files          : {summary['files']}")
    click.echo(f"    applied         : {summary['applied']}")
    click.echo(f"    accepted (total): {summary['accept_count']}")
    click.echo(f"    heroes          : {summary['hero_count']}")
    if summary["unknown_ids"]:
        click.echo(f"    [warn] {summary['unknown_ids']} unknown asset_id(s) ignored", err=True)


# ------------------------------------------------------ Photo-book editor ----
@cli.command("export_book")
@click.option("--book-path", type=click.Path(dir_okay=False), default="book.json", show_default=True)
@click.option("--output-dir", type=click.Path(file_okay=False), default="export_book", show_default=True)
@click.option("--spread", "spreads", type=int, multiple=True,
              help="Zero-based spread to export; repeat for a preflight subset.")
def export_book_cmd(book_path: str, output_dir: str, spreads: tuple[int, ...]) -> None:
    """Render book.json as 300-DPI RGB pages, interior PDF, and cover PDF."""
    from . import export_book as exporter

    summary = exporter.export_book(
        book_path=book_path,
        output_dir=output_dir,
        spread_indices=list(spreads) if spreads else None,
    )
    click.echo("[photobook] print-book export complete:")
    click.echo(f"    pages             : {summary['page_count']}")
    click.echo(f"    page raster       : {summary['page_px'][0]}x{summary['page_px'][1]} RGB @ {summary['dpi']} DPI")
    click.echo(f"    full-res sources  : {summary['full_resolution_sources']}")
    click.echo(f"    derivative flags  : {len(summary['derivative_fallback_assets'])}")
    click.echo(f"    placements <300   : {len(summary['dpi_flags'])}")
    click.echo(f"    placements <240   : {len(summary['below_240_dpi'])}")
    click.echo(f"    output            : {output_dir}")
    if summary["derivative_fallback_assets"]:
        click.echo("    [warn] derivative fallbacks are listed in resolution_report.json", err=True)
    if summary["below_240_dpi"]:
        click.echo("    [warn] sub-240-DPI placements are listed in resolution_report.json", err=True)

@cli.command("editor")
@click.option("--book-path", type=click.Path(dir_okay=False), default="book.json", show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
def editor_cmd(book_path: str, host: str, port: int) -> None:
    """Start the local browser editor (assemble the book by clicking)."""
    from pathlib import Path

    from . import editor_server

    editor_server.serve(book_path=Path(book_path), host=host, port=port)


# ---------------------------------------------------------------- Phase 10 ---
@cli.command()
@click.option("--embed-dates", is_flag=True, default=False,
              help="Write REAL best_datetime into EXPORTED COPIES via exiftool (never originals).")
@click.option("--order-dates", is_flag=True, default=False,
              help="Write SYNTHETIC monotonic dates encoding the era spine, so a date-sorting "
                   "layout service reproduces the owner's exact order (each era -> its own year). "
                   "Real dates stay in book_manifest.csv. Overrides --embed-dates.")
@click.option("--label-filenames", is_flag=True, default=False,
              help="Name each exported copy <era>__<event>__<tags>__NNNN_<id> so Mixbook's "
                   "photo-tray search filters by era/event/theme. A-Z sort = era order.")
@click.option("--no-pdf", is_flag=True, default=False, help="Skip proof.pdf rendering.")
def export(embed_dates: bool, order_dates: bool, label_filenames: bool, no_pdf: bool) -> None:
    """Export accepted ORIGINALS as a chaptered, service-agnostic bundle."""
    from . import export as export_mod

    summary = export_mod.export(
        embed_dates=embed_dates, order_dates=order_dates,
        label_filenames=label_filenames, make_pdf=not no_pdf)
    click.echo("[photobook] export complete:")
    click.echo(f"    originals copied : {summary['accepted_copied']}")
    click.echo(f"    manifest rows    : {summary['manifest_rows']}")
    click.echo(f"    proof.pdf        : {'yes' if summary['proof_pdf'] else 'no'}")
    click.echo(f"    export dir       : {summary['export_dir']}")
    missing = summary.get("missing") or []
    if missing:
        click.echo(
            f"    [ERROR] {len(missing)} accepted photo(s) could NOT be materialized "
            "- their source (staged file or ZIP part) is missing. In --lean mode the "
            "Takeout .zip parts must still exist at export time.", err=True)
        for aid in missing[:10]:
            click.echo(f"        - {aid}", err=True)
        raise SystemExit(1)


@cli.command()
@click.option("--import", "import_path", default=None,
              help="Import downloaded eras_*.json sweeps (file or directory).")
@click.option("--page-size", default=300, show_default=True,
              help="Photos per era-sort page.")
def erasort(import_path: str, page_size: int) -> None:
    """Assign accepted photos to eras BY EYE, sweep by sweep.

    Generate the sorter, then in the browser: pick an era, click every photo from
    it, Submit (they vanish + a sweep file downloads). Repeat per era. Then:
    photobook erasort --import <downloads-dir>
    """
    from pathlib import Path

    from . import erasort as es

    if import_path:
        try:
            summary = es.import_eras(Path(import_path))
        except FileNotFoundError as e:
            click.echo(f"[photobook] {e}", err=True)
            raise SystemExit(2)
        click.echo("[photobook] era sweeps imported:")
        click.echo(f"    files   : {summary['files']}")
        click.echo(f"    assigned: {summary['applied']}")
        for era, n in sorted(summary["per_era"].items()):
            click.echo(f"      {era}: {n}")
        return

    summary = es.generate(page_size=page_size)
    click.echo(f"[photobook] era sorter: {summary['photos']} photos across "
               f"{summary['pages']} page(s):")
    for f in summary["files"]:
        click.echo(f"    {f}")
    click.echo("Pick an era, click its photos, Submit. Repeat. Then: "
               "photobook erasort --import <downloads-dir>")


@cli.command()
@click.option("--import", "import_path", default=None,
              help="Import downloaded event_*.json sweeps (file or directory).")
@click.option("--page-size", default=300, show_default=True)
@click.option("--eras", default=None,
              help="Comma-separated chapter ids to include (default: all chapters; "
                   "restrict to the ones where distinct named events cluster).")
def eventsort(import_path: str, page_size: int, eras: str) -> None:
    """Group photos into named, dated EVENTS by eye (fixes order within chapters)."""
    from pathlib import Path

    from . import eventsort as ev

    if import_path:
        try:
            s = ev.import_events(Path(import_path))
        except FileNotFoundError as e:
            click.echo(f"[photobook] {e}", err=True)
            raise SystemExit(2)
        click.echo("[photobook] event sweeps imported:")
        click.echo(f"    files    : {s['files']}")
        click.echo(f"    grouped  : {s['applied']}")
        click.echo(f"    re-dated : {s['dated']} (events with a date)")
        for name, n in sorted(s["per_event"].items()):
            click.echo(f"      {name}: {n}")
        return

    era_list = ([e.strip() for e in eras.split(",") if e.strip()] if eras
                else None)
    s = ev.generate(page_size=page_size, eras=era_list)
    click.echo(f"[photobook] event sorter: {s['photos']} photos across {s['pages']} page(s) "
               f"[eras: {', '.join(era_list) if era_list else 'all chapters'}]:")
    for f in s["files"]:
        click.echo(f"    {f}")
    click.echo("Add an event (name + date), click its photos, Submit. Then: "
               "photobook eventsort --import <downloads-dir>")


@cli.command()
@click.option("--import", "import_path", default=None,
              help="Import downloaded tag_*.json sweeps (file or directory).")
@click.option("--page-size", default=300, show_default=True)
def tagsort(import_path: str, page_size: int) -> None:
    """Apply OVERLAPPING multi-tags to photos (themes: holidays, kids, a place...)."""
    from pathlib import Path

    from . import tagsort as tg

    if import_path:
        try:
            s = tg.import_tags(Path(import_path))
        except FileNotFoundError as e:
            click.echo(f"[photobook] {e}", err=True)
            raise SystemExit(2)
        click.echo("[photobook] tag sweeps imported:")
        click.echo(f"    files   : {s['files']}")
        click.echo(f"    applied : {s['applied']} (tag additions)")
        for tag, n in sorted(s["per_tag"].items()):
            click.echo(f"      {tag}: {n}")
        return

    s = tg.generate(page_size=page_size)
    click.echo(f"[photobook] theme tagger: {s['photos']} photos across {s['pages']} page(s):")
    for f in s["files"]:
        click.echo(f"    {f}")
    click.echo("Add a tag, pick it, click photos, Add. A photo can have several. "
               "Then: photobook tagsort --import <downloads-dir>")


@cli.command()
@click.option("--import", "import_path", default=None,
              help="Import downloaded cut_*.json files (file or directory).")
def cull(import_path: str) -> None:
    """Subtractive trim of the accepted set, era by era (same-moment bursts boxed)."""
    from pathlib import Path

    from . import cull as cl

    if import_path:
        try:
            s = cl.import_cuts(Path(import_path))
        except FileNotFoundError as e:
            click.echo(f"[photobook] {e}", err=True)
            raise SystemExit(2)
        click.echo("[photobook] cuts imported:")
        click.echo(f"    files : {s['files']}")
        click.echo(f"    cut   : {s['cut']} photos -> reject")
        for era, n in sorted(s["per_era"].items()):
            click.echo(f"      {era}: {n}")
        remaining = None
        from . import db
        with db.session() as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM assets WHERE review_status='accept'").fetchone()[0]
        click.echo(f"    accepts remaining: {remaining}")
        return

    s = cl.generate()
    click.echo(f"[photobook] cull surface: {s['photos']} photos across {s['eras']} eras "
               f"({s['clustered']} in same-moment clusters):")
    click.echo(f"    index: {s['index']}")
    click.echo("Cull each era (subtractive), export cuts, then: photobook cull --import <dir>")


@cli.command()
@click.option("--page-size", default=100000, show_default=True)
def theme(page_size: int) -> None:
    """Group photos by theme, then NARROW within each theme (beach 18 -> 5).

    Single-page by default so a theme's photos aggregate in one filtered view.
    """
    from . import theme as th
    s = th.generate(page_size=page_size)
    click.echo(f"[photobook] theme-aggregator: {s['photos']} photos across {s['pages']} page(s):")
    for f in s["files"]:
        click.echo(f"    {f}")
    click.echo("Tag photos into themes, filter to a theme, cut the extras. Then import:")
    click.echo("  photobook tagsort --import <dir>   (the theme memberships)")
    click.echo("  photobook cull    --import <dir>   (the cuts)")


@cli.command("verify-export")
def verify_export() -> None:
    """Verify the export bundle + confirm originals were never mutated (Phase 11)."""
    from . import verifyexport

    summary = verifyexport.verify()
    click.echo("[photobook] export verification:")
    for name, ok in summary["results"]:
        click.echo(f"    [{'PASS' if ok else 'FAIL'}] {name}")
    click.echo("    -> logs/export_verify.txt")
    raise SystemExit(0 if summary["all_pass"] else 1)


def main() -> None:
    cli()


if __name__ == "__main__":
    cli()
