# Quickstart

Get from zero to two PDFs in a few minutes — first with a built-in demo (no real photos needed), then with your own library.

Everything runs locally. Nothing is uploaded.

> **Two ways to call the CLI.** Every command below uses `python -m photobook <command>`. If you `pip install` the package, the shorter `photobook <command>` works identically — pick whichever you like.

---

## Part 1 — the 2-minute demo

### 1. Install

```bash
pip install -r requirements.txt
```

```bash
playwright install chromium
```

(Chromium powers the contact-sheet/proof render path and the `doctor` check below.
The demo itself renders its PDFs without it, so you can defer this download.)

### 2. Check your environment

```bash
python -m photobook doctor
```

`doctor` is the health gate: it confirms every dependency imports and the required tools are present. Get this green before anything else. (If it flags a Python-version problem, see [docs/SETUP.md](docs/SETUP.md) for the fallback.)

### 3. Build the demo

```bash
python scripts/make_demo.py
```

This generates a handful of synthetic photos, a demo `manifest.db`, and a demo `book.json` — a complete, fully-local book with nothing to download and no real photos involved. Everything it writes is regenerable.

### 4. Open the editor

```bash
python -m photobook editor
```

Then browse to **http://127.0.0.1:8765/**. Drag photos from the tray into cells, change the layout, promote a photo to a hero, crop — and watch the live low-DPI warnings. This is where **your taste** goes. (Stop the editor with `Ctrl+C` when you're done.)

### 5. Export the PDFs

```bash
python -m photobook export_book
```

This writes two files:

- `export_book/book.pdf` — the print-ready interior
- `export_book/cover.pdf` — the full-wrap cover

Open them. That's the whole loop, end to end.

---

## Part 2 — use your own photos

Once the demo makes sense, point the toolkit at your real library. Run these in order; every command is safe to re-run.

### 0. Create your config files

The pipeline reads three config files that ship as examples. Copy each and edit
for your book (chapter spine, photo sources, scoring weights):

```bash
cp config/chapters.example.yaml config/chapters.yaml
cp config/sources.example.yaml  config/sources.yaml
cp config/scoring.example.yaml  config/scoring.yaml
```

(On Windows without `cp`: `copy config\chapters.example.yaml config\chapters.yaml`, etc.
`config/story.example.yaml` is optional — only copy it when you want editorial
ordering overrides.)

### 1. Ingest your photos

```bash
python -m photobook ingest --src /path/to/your/photos --source-name my-library
```

Copies your photos into staging and records them in the manifest. **Your originals are never modified.**

### 2. Repair timestamps

```bash
python -m photobook daterepair
```

### 3. De-duplicate

```bash
python -m photobook dedup
```

### 4. Quality-filter

```bash
python -m photobook quality
```

### 5. Sort into chapters

```bash
python -m photobook chapter
```

Prefer to organize by era instead? Use the era-sorting review surface:

```bash
python -m photobook erasort
```

### 6. Score a shortlist

```bash
python -m photobook score
```

Ranks the strongest candidates per chapter so you review the best first — not the whole pile.

### 7. Review and decide

```bash
python -m photobook review
```

Opens browser contact sheets. Mark photos accept / reject / hero, then import your picks:

```bash
python -m photobook decisions --import /path/to/your/decisions.json
```

Nothing is deleted — rejects are just flags, and every photo stays reachable in the editor tray.

### 8. Lay it out

```bash
python -m photobook editor
```

Browse to **http://127.0.0.1:8765/** and build your spreads: filter the tray by date, era, event, or tags; drag photos into cells; pick layouts; promote heroes; crop. Autosave keeps your work.

### 9. Export

```bash
python -m photobook export_book
```

Out come `export_book/book.pdf` and `export_book/cover.pdf`, sized to the print shop's exact spec.

---

## Next

- **Order one physical proof** before the finals — some calls (white space, color, whether a hero lands on paper) can only be settled in your hands.
- Adapting to a different shop or size? Read [docs/PRINT_RUNBOOK.md](docs/PRINT_RUNBOOK.md).
- Need to upscale a full-page hero? See [docs/UPSCALING.md](docs/UPSCALING.md).
- Keeping faces and content clear of the spine and trim: [docs/GUTTER_AND_FACES.md](docs/GUTTER_AND_FACES.md).
- Fuller setup notes and dependency gotchas: [docs/SETUP.md](docs/SETUP.md).
