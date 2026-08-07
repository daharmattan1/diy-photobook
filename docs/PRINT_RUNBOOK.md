# Print Runbook — Getting a Book That Actually Prints Right

This is the fiddly-math half of `diy-photobook`. Layout is the fun part; **print correctness is where a
weekend disappears** — wrong bleed, a guessed spine, a cover that doesn't wrap, faces eaten by the
binding. This runbook teaches the geometry so you don't have to rediscover it the hard way.

The bundled exporter is a **reference implementation for exactly one shop and one size**: Blurb's
"Large Square" 12×12 in hardcover with a dust jacket. It is **not** a size-agnostic tool — it raises a
loud error if you point it at any other page size. Everything below uses that 12×12 book as the worked
example, and the last section ("Adapting to Another Shop or Size") shows you the math to re-derive the
numbers for a different product.

Blurb is used here because it publishes exact per-product templates and a live calculator. The same
method applies to Mixbook or any other PDF-to-Book shop — only the constants change.

---

> ## ⚠️ READ THIS BEFORE YOU ORDER
>
> **Print-shop specs change without notice.** Panel widths, flap sizes, spine tables, upload caps, and
> even color handling get revised silently. Every number in this document is a *snapshot*, not a
> contract.
>
> 1. **Re-verify the CURRENT template** with your shop's live PDF-to-Book calculator *for your exact
>    product, page count, and paper* before you export a final.
> 2. **Order ONE physical proof first.** Always. A one-copy keepsake with a permanent error is the whole
>    failure mode this toolkit exists to prevent.
> 3. **Date-stamp every spec you record.** When you write down "spine = 0.75 in," write down the date and
>    the paper stock next to it. A number with no date is a trap for future-you.

---

## 1. Interior Page Geometry

Each interior page is a **single** page (not a spread). The exporter renders it full-bleed, then trims
it to the shop's asymmetric interior template.

**Trim, bleed, and safe zones (Blurb Large-Square 12×12):**

| Quantity | Value | Notes |
|---|---|---|
| Trim (finished page) | 11.75 × 11.75 in | what the reader sees after cutting |
| Bleed | 0.125 in | on **top, bottom, and OUTSIDE edge ONLY** |
| Gutter/binding edge | **NO bleed** | the binding edge is not cut, so it gets no bleed |
| Final exported page | **11.875 × 12.0 in** | trim + bleed (see math below) |
| Safe margin (outer) | ~0.25 in | keep faces/text at least this far inside the trim |
| Gutter-safe zone | **≥ 0.5 in** | keep important content this far off the binding |

**Why the final page is 11.875 × 12.0 and not 12.25 square:**

- **Width** = 11.75 trim + 0.125 bleed on the *outside* edge only (the gutter edge has no bleed) = **11.875 in**
- **Height** = 11.75 trim + 0.125 bleed top + 0.125 bleed bottom = **12.0 in**

That asymmetry — bleed on three sides, none on the fourth — is the entire trick. A photo that runs to the
outside/top/bottom edges must extend 0.125 in past the trim so the cut never exposes a white sliver. The
gutter edge, by contrast, disappears into the binding and needs no bleed.

The **gutter-safe zone is stricter than the outer safe zone** (0.5 in vs 0.25 in) because perfect binding
physically curls paper into the spine. Anything closer than ~0.5 in to the binding is at risk of being
swallowed by the curve. See `docs/GUTTER_AND_FACES.md` for the faces-at-the-gutter problem specifically.

---

## 2. Spine Width — Never Estimate It (Cautionary Tale)

**This one nearly shipped a broken cover.**

The naive approach is to estimate spine width from page count with a per-page caliper:

```
spine ≈ pages × 0.0025 in/page
```

For this book (~152 designed pages) that gives **≈ 0.385 in**. It felt reasonable. It was **wrong.**

The print shop's own live calculator, fed the real product + paper stock + page count (~154 pages on
premium paper), returned **0.75 in** — nearly *double* the estimate. A cover built around 0.385 in would
have put the spine text off-center and thrown the entire back/spine/front wrap out of alignment. On a
one-copy keepsake, that's unrecoverable.

**Rule: always take the spine width from your shop's LIVE calculator, for your exact page count AND paper
stock.** Paper caliper varies dramatically by stock (a premium/thick paper can be 2× a standard one), so
`pages × constant` is never trustworthy. The per-page estimate is only useful as a rough sanity bound —
if the calculator and your gut differ by 2×, trust the calculator and re-check the paper.

The reference exporter hard-codes **0.75 in** for this specific 154-page premium build. Change the page
count or paper and that constant is stale — re-run the calculator and edit it.

---

## 3. Full-Wrap Cover Math

A hardcover dust jacket is **one wraparound PDF**: it runs flap → back panel → spine → front panel →
flap, left to right, as a single flat sheet.

**Trim layout (Blurb Large-Square, left → right):**

```
[ flap 3.792 ][ back panel 12.208 ][ spine 0.75 ][ front panel 12.208 ][ flap 3.792 ]
```

- Trim width = 3.792 + 12.208 + 0.75 + 12.208 + 3.792 = **32.75 in**
- Trim height = **12.0 in**
- Add **0.125 in bleed on all four sides** (unlike the interior, the cover bleeds on every edge):
  - Width = 32.75 + 0.125 + 0.125 = **33.0 in**
  - Height = 12.0 + 0.125 + 0.125 = **12.25 in**
- **Final cover PDF = 33.0 × 12.25 in**, TrimBox 32.75 × 12.0.

Note the spine (0.75 in) drops straight in from Section 2 — the cover width literally cannot be computed
until you have the calculator's spine number.

### Flap edge-extension (the fold-tolerance trick)

Folds are never placed to the micron. If your cover art stops exactly at a fold line and the bindery
folds even slightly off, a **white seam** appears at the crease. Prevent it:

- **Extend the cover art past each fold** by ~0.5 in into the flap. A slightly shifted fold then lands on
  *more of the same image*, never on an edge.
- **If the art is too narrow to fill the flaps** (a panorama sized for back+spine+front usually is), fill
  the flaps with a **solid color sampled from the art** (e.g. a dark tone pulled from the edge of the
  photo). The reference exporter does this by edge-replicating the outermost columns of the cover image
  out to the flap edges, which reads as a seamless continuation.

---

## 4. Two-File Upload

Upload the interior and the cover as **two separate PDFs**:

| File | What | Constraint |
|---|---|---|
| `cover.pdf` | the wraparound dust jacket | must stay under the shop's cover cap (~**90 MB**) |
| `book.pdf` | the interior pages | separate file, larger cap (multi-GB) |

**The cover cap is the reason to use JPEG, not lossless, for the cover.** A lossless (FlateDecode) cover
at 33 × 12.25 in / 300 DPI can blow past 90 MB. Encode the cover as high-quality JPEG (e.g. quality 96,
no chroma subsampling) — that lands around a few MB while staying visually indistinguishable at print
size. The reference exporter emits a JPEG cover for exactly this reason and hard-errors if the resulting
PDF exceeds 90 MB.

---

## 5. Page-Count Parity and Blank End-Leaves

A perfect-bound book **opens on a recto** (a right-hand page) and **closes on a verso** (a left-hand
page). To make every designed left|right spread land on a true facing pair — instead of straddling a
leaf — the exporter brackets the content with **one blank leaf at the front and one at the back**:

```
designed pages (e.g. 152)  +  2 blank leaves (front + back)  =  ordered page count (154)
```

That ordered count is what you enter on the order form, and it must respect the shop's min/max (Blurb
Large-Square: 24–480).

**Asymmetric TrimBox by parity.** Because the binding edge alternates sides, each interior page carries a
*different* TrimBox depending on whether it's a recto or a verso. This is the piece most homemade
exporters get wrong. The verified demo values (inches; PDF stores these as points = inches × 72):

| Page parity | Binding edge | MediaBox (in) | TrimBox (in) | TrimBox (pt) |
|---|---|---|---|---|
| **Recto** (odd, right-hand) | LEFT | 11.875 × 12.0 | `[0, 0.125, 11.75, 11.875]` | `[0, 9, 846, 855]` |
| **Verso** (even, left-hand) | RIGHT | 11.875 × 12.0 | `[0.125, 0.125, 11.875, 11.875]` | `[9, 9, 855, 855]` |

Read the recto TrimBox: x runs 0 → 11.75 (binding at x=0 with **no** bleed, outside edge at x=11.75 with
0.125 bleed out to the 11.875 media edge); y runs 0.125 → 11.875 (0.125 bleed top and bottom). The verso
is the mirror image: the un-bled binding edge is on the *right*, so the TrimBox starts at x=0.125.

PDF page 1 is the recto (the first designed page after the front blank leaf), and parity alternates from
there.

---

## 6. Color: sRGB, Never CMYK

- Export everything as **sRGB**. Do **not** convert to CMYK.
- Blurb (and most consumer PDF-to-Book shops) print from sRGB and handle the CMYK conversion on-press. If
  you convert to CMYK yourself, you double-convert and shift saturated tones (blues, greens, skin).
- If your source photos carry a wide-gamut profile (e.g. Display-P3 from a phone), convert them to sRGB
  *through their embedded ICC profile* — don't just reinterpret the numbers as sRGB, or saturated colors
  shift. The reference exporter honors each image's embedded profile and converts to sRGB.

---

## 7. Adapting to Another Shop or Size

The exporter targets **12×12 Blurb only** and refuses other sizes on purpose. Moving to a different shop
or size means **re-deriving the geometry and editing the code's geometry constants** — not flipping a
config flag. Here's the math you re-run:

**Interior page:**
```
final_width  = trim_width  + (bleed if outside edge bleeds)          # gutter edge: no bleed
final_height = trim_height + bleed_top + bleed_bottom
outer_safe   = shop's safe margin (often ~0.25 in)
gutter_safe  = shop's gutter margin (often ≥ 0.5 in on perfect binding)
```
Then build the **parity-aware TrimBox**: the binding edge (LEFT on recto, RIGHT on verso) gets no bleed,
so the TrimBox hugs the media edge on that side and insets by one bleed on the other three.

**Cover (full wrap):**
```
trim_width  = flap + back_panel + spine + front_panel + flap
cover_width = trim_width  + 2 × bleed
cover_height = trim_height + 2 × bleed        # cover bleeds on all 4 sides
```
Pull `spine` from the shop's **live calculator** for your page count and paper (Section 2). Pull
`flap` and `panel` widths from the shop's cover template for your exact product — do not reuse the
numbers above.

**Imposition:**
```
ordered_pages = designed_pages + 2 blank leaves (front + back)
```
Confirm the shop's open-on-recto / close-on-verso convention and its min/max page count.

**Where the constants live in the reference code:**

- `photobook/export_book.py` → `_blurb_page_transform` and the `export_book` interior loop (interior
  trim scale, bleed, and per-parity TrimBox construction)
- `photobook/export_book.py` → `_write_cover` (flap/panel/spine/bleed constants, JPEG encoding, 90 MB cap)
- `photobook/export_book.py` → `_write_image_pdf` (MediaBox / TrimBox / BleedBox emission, DeviceRGB)

Change the size and you are editing those constants and re-verifying against the new shop's template —
that is the intended workflow, and it's why the exporter errors loudly rather than pretending to be
size-agnostic.
