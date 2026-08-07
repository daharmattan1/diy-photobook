# Gutter & Faces — Keeping People Out of the Binding

The single most common way a photo book disappoints in the hand: **a face gets swallowed by the spine.**
The layout looked fine on screen — the page is flat there — but a printed perfect-bound book curls paper
into the binding, and a face that sat near the gutter vanishes into the curve. This doc explains the
problem, the scan tool that catches it, and how to fix a flagged page.

---

## 1. Why Faces Near the Binding Get Swallowed

- **Perfect binding glues the inner edge of every page into the spine.** Near the binding, the paper
  curves down toward the glue instead of lying flat.
- On screen, a two-page spread is a flat rectangle, so content sitting right at the center gutter looks
  perfectly visible. **The screen lies** — it can't show the physical curl.
- In the finished book, the ~0.5 in nearest the binding is partially lost into the curve. A **face**
  parked there loses an eye, a cheek, or the whole thing — and unlike a cropped landscape, a half-eaten
  face is immediately, permanently wrong.

This is why the interior geometry reserves a **gutter-safe zone of ≥ 0.5 in** (stricter than the ~0.25 in
outer safe margin — see `docs/PRINT_RUNBOOK.md`). The scan below is the automated check that nothing
important violates it.

---

## 2. The Scan Tool — `scripts/gutter_face_scan.py`

Automated gutter check over the **rendered** pages (it scans the actual page rasters the exporter
produced, so it sees exactly what will print):

- **Face detection.** Runs OpenCV Haar cascade detection on each rendered page — frontal faces, profile
  faces, and mirrored-profile faces (to catch profiles facing either direction).
- **Distance to the gutter.** For each detected face, it measures the distance from the face to that
  page's **INNER (gutter) trim edge**. The inner edge depends on parity: a recto (right-hand) page binds
  on the **left**, a verso (left-hand) page binds on the **right**, so the tool picks the correct edge
  per page.
- **Danger threshold.** Any face whose nearest edge lands within **~0.35 in (≈105 px at 300 DPI)** of the
  inner trim edge is **flagged**. Page geometry matches the exporter: 3675 px page canvas at 300 DPI,
  0.125 in (38 px) bleed.
- **Visual confirmation crops.** For every flagged page it writes a **zoomed gutter-strip crop** (the
  inner ~2.2 in of the page) with the detected face boxed, the inner trim line drawn, and the danger
  boundary drawn. You eyeball these crops to confirm — the detector is a *filter*, not a verdict.

**Expect false positives.** Haar detection is cheap and noisy: it will occasionally box a knee, a patch
of background, a mast, a plate of food, or a face that's actually behind a photo's white margin. That's
fine — the tool's job is to narrow hundreds of pages down to a handful of strips a human can review in a
minute. Always look at the crops before nudging anything.

---

## 3. Guidance — What's Actually a Problem

Not every face near the gutter needs fixing. Draw the distinction by **what the binding eats**:

- ✅ **A full-bleed photo crossing the gutter is fine.** Photos are *meant* to run across spreads; the
  image continuing into the binding is normal and looks intentional.
- ✅ **The binding eating a bit of hair, an ear, or the edge of a cheek is normal and invisible.** As
  long as the actual facial *features* — eyes, nose, mouth — sit inboard of the danger zone, a flagged
  face is usually fine. A face can trip the 0.35 in threshold by a few hundredths of an inch on its
  outer hairline and still print perfectly.
- ❌ **A FACE sitting AT the gutter is not fine.** If the features themselves fall inside the danger zone,
  the binding will take part of the face. That's the case to fix.

The test after you open the crop: **are the eyes/nose/mouth clearly inboard of the danger boundary?** If
yes, leave it. If the features straddle or cross the line, fix it.

---

## 4. How to Fix a Flagged Page

The fix lives in the **editor**, not the exporter — **the editor owns the crop.**

1. Open the flagged page/cell in the editor.
2. **Nudge the cell's crop away from the spine** — shift the crop window so the subject moves toward the
   outer edge and the face clears the gutter-safe zone. (The photo itself doesn't move; you're re-framing
   which part of it fills the cell.)
3. **Re-render / re-export** the affected page.
4. **Re-run `scripts/gutter_face_scan.py`** to confirm the face now clears the danger threshold (and that
   you didn't push a different subject into trouble).

Loop until the scan comes back clean — or until every remaining flag is a confirmed false positive or a
harmless hair/ear clip. Then move on to the final proof.
