# diy-photobook

**Build your own print-ready, keep-forever photo book on your own machine — a local, zero-cloud toolkit that does the fiddly print-correctness math (bleed, gutter-safe zones, spine width, DPI, full-wrap cover) so you don't lose a weekend to it.**

You bring the photos. The toolkit does the tedious, error-prone parts — sorting years of scattered images into a chronology, killing duplicates, catching the ones that are too low-resolution to print, and turning your laid-out spreads into a print shop's exact page geometry. Then you order one physical proof, check it, and order the finals.

Everything runs locally. Nothing is uploaded. Your photos never leave your machine.

---

## What it is / who it's for

`diy-photobook` is a Python command-line pipeline plus a small local editor for making a real, physical, printed photo book from your own photo library. It's for anyone who has hundreds (or thousands) of photos scattered across phones, exports, and drives, and wants to turn a chosen slice of them into a book that will sit on a shelf for decades — without paying a subscription service, uploading everything to someone's cloud, or losing a weekend to the finicky print math that print shops assume you already know.

You should be comfortable running a few commands in a terminal. You do **not** need to know anything about bleed, DPI, ICC color, or spine width — that's exactly the part the toolkit owns.

### License in one breath: source-available, non-commercial

This project is released under **PolyForm Noncommercial 1.0.0**. That means the source is public and you're free to read it, run it, and modify it for **personal and other non-commercial use**. It is **not** for commercial use.

Being honest about what that is and isn't:

- This is **"source-available," not OSI "open source."** The non-commercial restriction disqualifies it from the formal Open Source Definition. Please don't call it "open source" — call it source-available.
- Non-commercial terms are **inherently soft to enforce.** The license states the intent clearly; it relies mostly on good faith. If you want to use this commercially, that's a conversation, not something the license silently grants you.

See the [License](#license) section at the bottom.

---

## The end-to-end journey

This is the spine of the whole toolkit. Everything else is a detail hanging off one of these steps.

```
  1. BRING          2. CURATE            3. LAY OUT            4. PROCESS
  your own    ->    sort + dedup    ->   spreads in     ->    pipeline upscales,
  photos            + quality-filter     the editor           fixes DPI, computes
                    + chapter                                 print geometry
                                                                     |
                                                                     v
  7. ORDER          6. PROOF             5. EXPORT
  the finals   <-   order ONE       <-   print-ready interior PDF
  (with            physical proof        + full-wrap cover PDF
   confidence)     at a print shop
```

1. **Bring your own photos.** Point the toolkit at wherever your photos live. It copies them into a working staging area and records everything in a local manifest — it never touches your originals.
2. **Sort & curate.** The pipeline repairs missing/wrong timestamps, groups near-duplicates (keeping one), flags junk (blurry, too dark, screenshots, documents), sorts everything into chronological chapters, and scores a shortlist so you're reviewing the best candidates first — not scrolling ten thousand images.
3. **Lay out spreads in the editor.** A small local web editor lets you drag chosen photos into page cells, pick a layout, reorder, promote a standout to a full-page hero, and crop — with live warnings the instant a photo is too low-res for the size you've placed it at.
4. **The pipeline processes + upscales.** When you export, the pipeline does the print-correctness work: computes bleed and gutter-safe zones, checks every placement against the DPI floor, optionally upscales the handful of photos that need it, and lays out the full-wrap cover with the correct spine width for your page count.
5. **Produce the PDFs.** Out come two files: a **print-ready interior PDF** and a **full-wrap cover PDF**, sized to the print shop's exact spec.
6. **Order ONE physical proof.** Before you commit to the finals, order a single low-cost proof. Some things — how white space actually feels, whether the color is right, whether a hero is as striking on paper as on screen — can only be settled by holding the book.
7. **Order the finals.** With the proof in hand and any last tweaks made, order the real thing.

**A note on curation: the book is a curated _subset_, and your full set always stays reachable.** Nothing is ever deleted. "Reject," "duplicate," and "not in the book" are all just flags in the manifest — every photo you brought in stays available, and the editor always shows you the complete accepted pool, marking what's placed and what isn't. You can always pull more in.

---

## The four organizing axes

Every photo is organized along four axes, and you can sort, filter, and group by any of them — in the pipeline and in the editor's photo tray:

| Axis | What it means |
|---|---|
| **Date** | When the photo was taken (repaired from EXIF → sidecar metadata → filename → file time). |
| **Era / chapter** | The chronological section of the book a photo belongs to. |
| **Event** | A specific happening — a trip, a birthday, a move — that groups photos tighter than a whole chapter. |
| **Tags** | Free-form labels you attach to slice the library any way you want. |

These are what keep a story together. When you're filling a spread, you sort the tray by event or era so you're pulling from **related** photos — not mixing unrelated trips or occasions just to fill a geometric hole on the page.

---

## The mental model: four layers

The single most important idea in this toolkit — the thing that keeps it from thrashing — is that making a photo book splits cleanly into **four layers**, and each layer has a different owner. We do **not** ask one layer to do another layer's job.

| Layer | Nature | Who owns it |
|---|---|---|
| **A. Data** — chronology, chapters | Deterministic; a correctness problem | **Code** (with your eye to catch mis-files) |
| **B. Layout baseline** — density, sizing, hero prominence | Rules, tunable; a good 80% default | **Code** |
| **C. Editorial taste** — pacing, which photo is the hero, facing-page cohesion, the last 20% | Taste; not a correctness problem | **You**, in the editor |
| **D. Print** — bleed, DPI, color, geometry, the proof | Deterministic (plus one physical proof) | **Code**, plus a physical proof |

The reason this matters, stated plainly:

> **The engine optimizes constraint-satisfaction (coverage, faces-safe crops, DPI, tiling) — not visual taste (rhythm, hierarchy, which photo is the hero, whether a spread is beautiful). So instead of pretending to have taste, it gives the human good tools and gets out of the way.**

Every attempt to make the *algorithm* produce editorial taste turns into whack-a-mole: tune density and the heroes shrink; fix the heroes and the chronology breaks. The fix is to nail layers A, B, and D in code — deterministically, correctly, repeatably — and to build layer C into the **editor**, where your edits are fast to make and they *stick*. The code never tries to code taste.

---

## Try it in 2 minutes

No real photos required. A one-command demo generates synthetic photos, a demo manifest, and a demo book so you can watch the whole thing work end to end. You need **Python 3.12+** and a terminal; clone the repo, `cd` into it, then:

```bash
pip install -r requirements.txt
```

```bash
playwright install chromium
```

(The chromium download is needed for the contact-sheet/proof render path and a fully green `doctor` — the demo below renders its PDFs without it, so you can defer this step.)

```bash
python scripts/make_demo.py
```

```bash
python -m photobook export_book
```

That writes `export_book/book.pdf` (the interior) and `export_book/cover.pdf` (the full-wrap cover). Then open the editor:

```bash
python -m photobook editor
```

...and browse to **http://127.0.0.1:8765/**.

(If you installed the package with pip, `photobook <command>` works too — it's the same entry point as `python -m photobook <command>`.)

For the full step-by-step — including how to move from the demo to **your own** photos — see **[QUICKSTART.md](QUICKSTART.md)**.

---

## The editor's scope (deliberately narrow)

The editor does exactly one job: **visual layout and initial photo interaction.** That's it, on purpose.

It lets you:

- filter and sort the photo tray by **date, era, event, and tags**;
- **drag and drop** photos into page cells;
- **change the layout** of a page and **reorder** pages/spreads;
- **promote** a photo to a full-page hero;
- **crop** a photo;
- see **live low-DPI warnings** the moment a placement is too low-res to print at that size;
- **undo / redo**;
- **autosave** as you work.

What the editor deliberately does **not** do: export, and any print-correctness. The **pipeline** owns the export and **all** print correctness — bleed, gutter-safe zones, DPI enforcement, spine width, cover wrap. The editor is where taste happens; the pipeline is where print math happens. Keeping that boundary sharp is what keeps both halves reliable. (The editor also never deletes a source photo — removing a photo from a page only returns it to the tray.)

---

## Commands

The pipeline is a sequence of small commands, run in order. Each is safe to re-run (idempotent).

```
doctor  ->  ingest  ->  daterepair  ->  dedup  ->  quality  ->  chapter  ->  score
        ->  review  ->  decisions  ->  editor  ->  export_book  ->  export
```

Plus curation/sorting helpers you can reach for as needed: `erasort`, `eventsort`, `tagsort`, `cull`, `theme`, and `verify-export`.

| Command | What it does |
|---|---|
| `doctor` | Health gate — verifies every dependency imports and the tools are present. Run first. |
| `ingest` | Copies your photos into staging + writes manifest rows. Originals are never mutated. |
| `daterepair` | Resolves the best timestamp per photo (EXIF → sidecar → filename → file time), in the manifest only. |
| `dedup` | Groups near-duplicates by perceptual hash; keeps one per group. |
| `quality` | Advisory flags for blurry / dark / screenshot / document images. |
| `chapter` | Sorts photos into your chronological chapter spine. |
| `score` | Ranks a per-chapter shortlist so you review the strongest candidates first. |
| `review` | Emits browser contact sheets for accept / reject / hero decisions. |
| `decisions` | Imports your review picks back into the manifest. |
| `export_book` | Renders the laid-out book to a print-ready interior PDF + full-wrap cover PDF. |
| `editor` | Serves the local layout editor at http://127.0.0.1:8765/. |
| `export` | Exports the chaptered bundle of accepted originals (byte-verified) + a proof. |
| `erasort` / `eventsort` / `tagsort` | Sort/group review surfaces by era, event, or tags. |
| `cull` | Trim the accepted pool down. |
| `theme` | Group photos by visual theme, then narrow within each theme (curation helper). |
| `verify-export` | Asserts bundle integrity and that originals were never mutated. |

---

## Print size: one supported size, and how to change it

The exporter targets **Blurb Large-Square, 12×12 in**, as its **one supported size**. This is deliberate: print correctness (bleed, spine width, safe zones, DPI thresholds) is exact math tied to a specific physical size, and supporting one size well beats supporting many sizes badly. The exporter includes a **loud guard** that stops you if the book geometry doesn't match the supported size — it will not silently ship a mis-sized book.

This does **not** mean the toolkit is "size-agnostic" — it isn't, and it never claims to be. If you want to target a different shop or a different size, **[docs/PRINT_RUNBOOK.md](docs/PRINT_RUNBOOK.md)** teaches you the math to adapt it.

---

## Docs

| Doc | What's in it |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | The tight, numbered getting-started — demo first, then your own photos. |
| [docs/SETUP.md](docs/SETUP.md) | Full environment setup, Python version fallback, and dependency gotchas. |
| [docs/PRINT_RUNBOOK.md](docs/PRINT_RUNBOOK.md) | The print math — and how to adapt it to another shop or size. |
| [docs/UPSCALING.md](docs/UPSCALING.md) | When and how to upscale the handful of photos that need it for full-page placement. |
| [docs/GUTTER_AND_FACES.md](docs/GUTTER_AND_FACES.md) | Gutter-safe zones and face-safe cropping — keeping faces and important content out of the spine and off the trim. |

Config templates live in **`config/*.example.yaml`** — copy each to its non-`example` name and edit.

---

## License

Released under **PolyForm Noncommercial 1.0.0**. Source-available and free for **personal and non-commercial** use; **not** licensed for commercial use.

To be clear and honest about it: this is **source-available, not OSI "open source"** (the non-commercial clause is what makes the difference), and non-commercial terms are **inherently soft to enforce** — the license states the intent, and mostly trusts you to honor it. If you'd like to use `diy-photobook` commercially, reach out rather than assuming the license covers it.
