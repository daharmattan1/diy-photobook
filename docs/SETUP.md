# Setup

Everything runs **locally** — ingest, dedup, curation, and the print export all
happen on your own machine. No cloud, no account. This doc gets you to a green
`photobook doctor`.

---

## Python

- **Built and tested on Python 3.14** (the pinned C-extension wheels resolve to
  `cp314` builds).
- **`pyproject.toml` requires ≥ 3.12.** Any 3.12+ interpreter is supported.

### If a C-extension wheel won't build

`rawpy`, `opencv-python-headless`, and `pillow-heif` ship compiled wheels. On a
**very new** interpreter, a wheel may not exist yet and pip will try to build
from source (and fail without a compiler toolchain).

**Do not run on a half-broken environment.** If any of those wheels won't install
on your newest interpreter, fall the **whole** environment back to a **3.12
virtualenv** rather than limping along with a missing dependency:

```bash
# Fallback: a clean 3.12 environment where all wheels are prebuilt
py -3.12 -m venv .venv312          # Windows (py launcher)
# python3.12 -m venv .venv312      # macOS / Linux
.venv312/Scripts/activate          # Windows
# source .venv312/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

The install gate is all-or-nothing on purpose: every pinned dependency must
import on the target interpreter before any pipeline stage runs.

---

## Install steps

```bash
# 1. Python dependencies
pip install -r requirements.txt

# 2. Chromium for the browser render path (contact sheets + proof PDF)
playwright install chromium

# 3. Verify the environment end-to-end
python -m photobook doctor
```

`photobook doctor` is the health gate. It checks every hard dependency on **this**
interpreter and writes a green/red report to `logs/doctor.txt`. It exits non-zero
if any mandatory check fails — get it fully green before running any real stage.

Mandatory checks: Pillow imports, HEIC decodes, imagehash imports, exiftool
available, Playwright Chromium launches. RAW decode (`rawpy`) is
**mandatory-if-present** — it is skipped with a warning when no RAW files exist,
but becomes a hard failure the moment RAW files are ingested and can't decode.

---

## Dependencies

From `requirements.txt`:

| Package | Why it's here |
|---------|---------------|
| **Pillow** | Core image decode / resize / save. |
| **pillow-heif** | HEIC/HEIF decode — the default format from most phones. |
| **rawpy** | RAW decode — only needed if you shoot RAW. |
| **imagehash** | Perceptual hashing (pHash) for near-duplicate detection. |
| **piexif** | EXIF read/parse helper (never used to write originals). |
| **numpy** | Array math backing the image/scoring work. |
| **pandas** | Tabular bookkeeping across the pipeline stages. |
| **opencv-python-headless** | Variance-of-Laplacian blur detection. **Headless** to avoid pulling in Qt/GUI dependencies on a server or bare machine. |
| **playwright** (+ **chromium**) | Renders the contact-sheet / proof pages via headless Chromium. |
| **click** | CLI framework. |
| **PyYAML** | Reads the `config/*.yaml` files. |

---

## exiftool (external binary — NOT a pip package)

`exiftool` is a standalone executable, not a Python package, so
`pip install -r requirements.txt` does **not** install it. Install it separately
and confirm it runs:

```bash
exiftool -ver
```

The toolkit uses exiftool **read-only** on your originals — it extracts capture
dates and metadata but never writes back to your source files.

- **Env override:** set `PHOTOBOOK_EXIFTOOL` to a specific binary path to point
  the toolkit at an exact install (it takes precedence over PATH).
- **Windows note:** installers often drop it in
  `%LOCALAPPDATA%\Programs\ExifTool\` and only update the *persistent* PATH — a
  shell opened **before** the install won't see it. Open a fresh shell, or set
  `PHOTOBOOK_EXIFTOOL` directly.

```bash
# Example: pin exiftool explicitly if it isn't on PATH
export PHOTOBOOK_EXIFTOOL="/usr/local/bin/exiftool"     # macOS / Linux
# set PHOTOBOOK_EXIFTOOL=%LOCALAPPDATA%\Programs\ExifTool\ExifTool.exe   # Windows (cmd)
```

---

## Editor stack

The review/curation editor is deliberately dependency-free:

- A **Python standard-library HTTP server** (nothing to install beyond the
  Python deps above).
- A **vanilla single-page app** — no build step, no bundler, no CDN. Plain
  HTML/CSS/JS served straight off disk.

There is nothing to compile and no `node_modules`. Start it, open the local URL
in a browser, curate.

---

## Windows-isms (and what mac/Linux users should check first)

**Be aware:** this toolkit was built and run on Windows. The cross-platform paths
below are implemented and intended to work, but macOS/Linux are **largely
untested** — treat a first run on those platforms as something to verify, not
assume.

What's already cross-platform:

- **Repo paths** resolve relative to the source files, so the pipeline works
  regardless of your current working directory.
- **Fonts** are resolved cross-platform, with env overrides:
  `PHOTOBOOK_FONT_SERIF` and `PHOTOBOOK_FONT_SANS` let you name exact font files.
- **exiftool** is resolved via PATH → known install dirs → the
  `PHOTOBOOK_EXIFTOOL` override (see above).
- **Bulk data location** is redirectable via `PHOTOBOOK_DATA_ROOT` — point it at
  an external/secondary drive so a large photo library (a full library can run to
  ~140 GB) doesn't fill your system drive:

  ```bash
  export PHOTOBOOK_DATA_ROOT="/Volumes/Photos/photobook-data"   # macOS
  # export PHOTOBOOK_DATA_ROOT=/mnt/photos/photobook-data       # Linux
  # set PHOTOBOOK_DATA_ROOT=E:\photobook-data                   # Windows
  ```

One more **Windows** pip gotcha: if you clone into a deeply nested folder,
`pip install` of Playwright can fail with `[WinError 206] The filename or
extension is too long` (its driver package has very deep paths). Clone to a
short path (e.g. `C:\src\diy-photobook`) or enable Windows long paths
(`LongPathsEnabled`).

Specific things a **macOS/Linux** user should sanity-check first:

1. **Fonts.** Font file names and availability differ per OS. If the serif/sans
   the export expects isn't installed, set `PHOTOBOOK_FONT_SERIF` /
   `PHOTOBOOK_FONT_SANS` to real font files on your system and re-run.
2. **Console encoding / mojibake.** On Windows the console can be `cp1252`, which
   garbles non-ASCII output. If you see mojibake (or a `UnicodeEncodeError`),
   force UTF-8:

   ```bash
   export PYTHONUTF8=1
   export PYTHONIOENCODING=utf-8
   ```

3. **Atomic writes.** The toolkit uses `os.replace` for atomic file swaps — this
   is cross-platform, but it requires source and destination to be on the **same
   filesystem/volume**. If you redirect `PHOTOBOOK_DATA_ROOT` to another drive,
   keep each stage's temp files on that same drive.
4. **Data root on the right volume.** Set `PHOTOBOOK_DATA_ROOT` to a drive with
   enough headroom for your library before the first ingest — `photobook doctor`
   reports free space at the data root so you catch a too-small target early.
5. **exiftool + Chromium.** Confirm `exiftool -ver` runs and
   `playwright install chromium` succeeded — both are the checks most likely to
   differ from a Windows setup.
