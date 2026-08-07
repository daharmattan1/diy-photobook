#!/usr/bin/env python3
"""Decontamination gate for this PUBLIC repo.

Scans every git-TRACKED text file for markers that must never ship, and
verifies git is not tracking any image / video / database / archive / binary
file. Exits 0 (CLEAN) or 1 (hits found).

Two layers of checks:

1. GENERIC checks (always on, no configuration needed):
   - no tracked file with a binary/image/db/archive extension
   - no tracked file whose CONTENT is binary (NUL byte sniff — catches a binary
     renamed to a text extension)
   - no 40-hex content-addressed asset ids in source (photo ids are personal)
   - no email addresses (except @example.com/org/net placeholders)
   - no machine-specific home-directory paths (C:/Users/<name>, /home/<name>,
     /Users/<name>) — placeholder names like "you"/"username" are allowed

2. PRIVATE blocklist (personal names / strings / dates). These tokens are
   themselves personal data, so they are NOT stored in this file (which ships
   publicly). They load from an UNTRACKED local file at the repo root:

       scrub_private_blocklist.txt

   one entry per line:  name:<token> | string:<substring> | date:<YYYY-MM-DD>
   ('#' comments and blank lines ignored). `name:` tokens match at LETTER
   boundaries, so "anna" inside "savannah" does not trip, but "06_anna" or
   "anna-2" DOES (underscores/digits are not a hiding place).
   If the file is absent, the gate still runs the generic checks but prints a
   loud warning — treat a CLEAN without the private blocklist as provisional.
   The blocklist file itself must never be git-tracked (hard failure if it is).

This is a TRIPWIRE, not a proof: it raises the floor and makes an accidental
leak loud. Human review of the tree before publishing remains the terminal gate.

Usage:
    python scripts/scrub_check.py            # scan the tracked tree
    python scripts/scrub_check.py --verbose  # also print advisory (non-failing) flags
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_BLOCKLIST = ROOT / "scrub_private_blocklist.txt"

# Extensions that must NEVER be git-tracked in a code+docs repo.
DENY_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic",
    ".heif", ".avif", ".raw", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf",
    ".orf", ".rw2", ".pef", ".srw", ".mp4", ".mov", ".avi", ".mkv", ".m4v",
    ".3gp", ".webm", ".db", ".sqlite", ".sqlite3", ".zip", ".exe", ".dll",
    ".so", ".dylib", ".pdf", ".mp3", ".wav",
}

# Home-dir path usernames that are placeholders, not real people.
PATH_PLACEHOLDER_USERS = {"you", "yourname", "username", "user", "name", "me"}

# Email domains allowed as documentation placeholders.
EMAIL_OK_DOMAINS = ("example.com", "example.org", "example.net")

# --- ADVISORY (printed with --verbose; never fails the gate) ------------------
# Generic ISO dates that are known example dates get suppressed from the
# advisory listing; anything else is surfaced for a human to eyeball.
EXAMPLE_DATES_OK = {
    "2001-01-01", "2003-12-31", "2004-01-01", "2006-12-31", "2007-01-01",
    "2009-12-31", "2010-01-01", "2012-12-31", "2005-06-15", "2010-09-01",
    "2005-06-01", "2005-07-01",
    "2000-06-15", "2001-06-15", "2002-06-15", "2003-06-15", "2004-06-15",
}


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True, text=True, check=True,
    )
    return [ROOT / line for line in out.stdout.splitlines() if line.strip()]


def load_private_blocklist() -> tuple[list[str], list[str], list[str], bool]:
    """Returns (name_tokens, string_tokens, dates, present)."""
    names: list[str] = []
    strings: list[str] = []
    dates: list[str] = []
    if not PRIVATE_BLOCKLIST.exists():
        return names, strings, dates, False
    for raw in PRIVATE_BLOCKLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, value = line.partition(":")
        value = value.strip().lower()
        if not value:
            continue
        if kind == "name":
            names.append(value)
        elif kind == "string":
            strings.append(value)
        elif kind == "date":
            dates.append(value)
    return names, strings, dates, True


def main() -> int:
    verbose = "--verbose" in sys.argv
    files = tracked_files()
    hits: list[tuple[str, str, int, str]] = []
    advisories: list[tuple[str, str, int, str]] = []

    name_tokens, string_tokens, personal_dates, private_present = load_private_blocklist()

    # The private blocklist must never itself be tracked.
    if any(f.name == PRIVATE_BLOCKLIST.name for f in files):
        hits.append(("TRACKED-BLOCKLIST", PRIVATE_BLOCKLIST.name, 0,
                     "the private blocklist is git-tracked — it must stay local-only"))

    # Personal names match at LETTER boundaries (not \b): catches "06_anna",
    # "anna-2", "_anna" while still ignoring "savannah", "susanna", "victory".
    name_re = (re.compile(r"(?<![a-z])(" + "|".join(map(re.escape, name_tokens)) + r")(?![a-z])", re.I)
               if name_tokens else None)
    date_re = (re.compile("|".join(re.escape(d) for d in personal_dates))
               if personal_dates else None)
    generic_date_re = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
    hex40_re = re.compile(r"\b[0-9a-f]{40}\b")
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    homepath_re = re.compile(r"(?i)(?:c:[/\\]+users[/\\]+|/home/|/users/)([a-z0-9_.-]+)")

    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        # 1) no tracked binaries / images / databases (by extension)
        if f.suffix.lower() in DENY_EXT:
            hits.append(("TRACKED-BINARY", rel, 0, f.suffix))
            continue
        # 2) no tracked binaries by CONTENT (catches a binary renamed .txt)
        try:
            head = f.read_bytes()[:8192]
        except Exception:
            continue
        if b"\x00" in head:
            hits.append(("BINARY-CONTENT", rel, 0, "NUL byte in file"))
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            # The author's OWN name is allowed as intentional attribution on a
            # copyright line in the license/notice/readme — that is deliberate
            # published authorship, not a leak. Everywhere else, names hard-fail.
            attribution_line = (rel in {"LICENSE", "NOTICE", "README.md"}
                                and "copyright" in low)
            for tok in string_tokens:
                if tok in low:
                    hits.append(("STRING", rel, i, tok))
            if name_re and not attribution_line:
                for m in name_re.finditer(line):
                    hits.append(("NAME", rel, i, m.group(0)))
            if date_re:
                for m in date_re.finditer(line):
                    hits.append(("PERSONAL-DATE", rel, i, m.group(0)))
            # HARD FAIL: a real 40-hex asset_id in tracked SOURCE. Content-addressed
            # photo ids are personal; there should be NONE in the shipped code. The
            # only allowed 40-hex forms are obvious zero-padded placeholders
            # (e.g. 0000...0001 in the example configs).
            for m in hex40_re.finditer(line):
                if not re.fullmatch(r"0{30,}\d+", m.group(0)):
                    hits.append(("ASSET-HASH", rel, i, m.group(0)))
            # HARD FAIL: email addresses (placeholder domains excepted).
            for m in email_re.finditer(line):
                addr = m.group(0).lower()
                if not addr.endswith(EMAIL_OK_DOMAINS):
                    hits.append(("EMAIL", rel, i, m.group(0)))
            # HARD FAIL: machine-specific home-directory paths.
            for m in homepath_re.finditer(line):
                user = m.group(1).lower().rstrip(".")
                if user not in PATH_PLACEHOLDER_USERS:
                    hits.append(("HOME-PATH", rel, i, m.group(0)))
            # advisory: any other ISO date (surfaced for a human eyeball, non-failing)
            for m in generic_date_re.finditer(line):
                d = m.group(0)
                if d in EXAMPLE_DATES_OK:
                    continue
                if date_re and date_re.fullmatch(d):
                    continue
                advisories.append(("date?", rel, i, d))

    # --- report ---
    print(f"scrub_check: scanned {len(files)} tracked files")
    if not private_present:
        print(
            "\n  WARNING: scrub_private_blocklist.txt not found — personal "
            "name/string/date checks were SKIPPED.\n  A CLEAN result without it is "
            "provisional. (Publisher: restore your local blocklist before trusting "
            "this gate.)"
        )
    if hits:
        print(f"\n  FAIL — {len(hits)} blocklist hit(s):")
        for kind, rel, line, tok in hits[:200]:
            loc = rel if line == 0 else f"{rel}:{line}"
            print(f"    [{kind}] {loc}  ->  {tok!r}")
        if len(hits) > 200:
            print(f"    ... and {len(hits) - 200} more")
    if advisories and (verbose or not hits):
        shown = advisories if verbose else advisories[:15]
        print(f"\n  advisory ({len(advisories)} — review, not failing):")
        for kind, rel, line, tok in shown:
            print(f"    ({kind}) {rel}:{line}  ->  {tok!r}")
        if not verbose and len(advisories) > 15:
            print(f"    ... and {len(advisories) - 15} more (run with --verbose)")

    if hits:
        print("\nRESULT: CONTAMINATED (exit 1)")
        return 1
    print("\nRESULT: CLEAN (exit 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
