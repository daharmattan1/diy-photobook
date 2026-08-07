# Upscaling & Print DPI

How the toolkit measures print resolution, when it upscales a photo, and why the
default upscaler is plain CPU Lanczos rather than a GPU super-resolution model.

If you read nothing else: **Lanczos on CPU is the reliable default.** GPU
Real-ESRGAN is optional, download-it-yourself, and can silently corrupt output
under load. The rest of this doc explains why.

---

## DPI thresholds

Print resolution is measured in **effective DPI** — the number of real image
pixels that land in one printed inch of a page cell.

| Threshold | Meaning |
|-----------|---------|
| **240 DPI** | Hard print floor. Treat anything below this as a defect to fix. |
| **300 DPI** | Ideal. Aim here for every near- or full-page photo. |

Some print shops accept as low as ~150 DPI and only *warn* below 300. Do not
lean on that tolerance — **240 is your floor.** Between 240 and 300 you are in
acceptable-but-soft territory; a matte/lustre interior paper hides that softness
better than a glossy one.

---

## The effective-DPI formula

For a photo placed in a cell, the toolkit computes:

```
effective_dpi = min(px_w / cell_w_inches, px_h / cell_h_inches)
```

- `px_w`, `px_h` — the usable pixel dimensions of the source image.
- `cell_w_inches`, `cell_h_inches` — the printed size of the cell on the page.
- `min(...)` — the tighter of the two axes wins, because the *worst* axis is what
  the eye (and the print shop's preflight) catches.

### It is crop-aware

A crop window shrinks the usable pixels. If a cell only shows 60% of the image
width, only that 60% of the pixels count toward DPI:

```
px_w = crop_w_fraction * full_width_px
px_h = crop_h_fraction * full_height_px
```

So a 4000 px-wide photo cropped to 50% delivers the print resolution of a
2000 px photo. Tight crops on already-small sources are the most common way a
cell silently drops under the 240 floor.

---

## When to upscale

Upscale a photo when — **at its placed size and crop** — its effective DPI
renders below the **240 floor**. In practice that is:

- A small or old photo placed near-full-page or full-page.
- A heavily cropped photo where the crop throws away most of the pixels.
- A short-side portrait stretched across a tall cell.

A photo that clears 240 at its placed size does **not** need upscaling. Upscaling
a photo that is already large enough just adds file bloat with no visible gain.

---

## The Lanczos-vs-ESRGAN lesson (read this)

This is the reason this document exists.

**Plain Lanczos on the CPU is the reliable default.** It is deterministic, has no
GPU dependency, and cannot corrupt its output. `scripts/fix_lowdpi_reliable.py`
is that path and is the **recommended tool** for fixing sub-floor cells.

**Real-ESRGAN / Vulkan can CORRUPT output under concurrent GPU load.** When two
super-resolution jobs hit the GPU at once, the Vulkan queue can fail
(`vkQueueSubmit failed`) and the model writes **black or near-black frames**
instead of an upscaled image. The failure is silent — the file exists, it is just
ruined. **Never run two ESRGAN jobs at the same time**, and never run one
alongside another heavy GPU task.

**On a resolution-limited photo, Lanczos is equivalent quality anyway.** A
super-resolution model can only *hallucinate* detail; it cannot recover detail
the camera never captured. For a photo that is soft because the original is small
or slightly out of focus, a clean Lanczos up-res gives you the same usable result
without the corruption risk. ESRGAN earns its keep only on borderline sources
where invented micro-detail is genuinely worth the GPU gamble — and even then,
run it serially and verify every frame.

### How `fix_lowdpi_reliable.py` works

It resolves the **actual file the exporter will render** for each placement, and
for every cell below the DPI floor it:

1. Picks the **largest clean source** available — the render file itself, or a
   prior clean ESRGAN derivative if that is bigger.
2. **Skips corrupt/near-black sources** — a source whose mean brightness is near
   zero, or whose pixel variance is near zero (a uniform frame), is rejected as
   garbage rather than upscaled.
3. Upscales via **Lanczos** to the pixel count the cell needs for target DPI.
4. **Caps the long side** (to avoid multi-hundred-MB files) and writes the result
   back over the file the exporter reads.

Because it never touches the GPU, it cannot produce the black-frame corruption
the ESRGAN path can.

```bash
# Reliable, GPU-free low-DPI fix (the default — run this)
python scripts/fix_lowdpi_reliable.py
```

---

## Real-ESRGAN is optional and download-it-yourself

Real-ESRGAN is **not bundled** with this toolkit. The upscaler binary
(`realesrgan-ncnn-vulkan`) is a ~45 MB third-party executable with its own
license, so you must download it yourself if you want the GPU path.

1. Get `realesrgan-ncnn-vulkan` from the upstream **Real-ESRGAN** project (search
   for the official `xinntao/Real-ESRGAN` releases, which ship the
   `realesrgan-ncnn-vulkan` prebuilt binaries).
2. Unpack the executable and its model files into `tools/realesrgan/` at the repo
   root, so `scripts/upscale_batch.py` can find `realesrgan-ncnn-vulkan(.exe)`.
3. `scripts/upscale_batch.py` drives it from a worklist you build at
   `samples/upscale_worklist.json` — a JSON map of the photos to upscale,
   `{"<asset_id>": {"src": "<path-to-source-image>"}, ...}` (use the low-DPI
   placements listed in `export_book/resolution_report.json` to pick them). It
   upscales with the `realesrgan-x4plus` model and downsizes to the factor each
   photo actually needs.

```bash
# Optional GPU path — ONLY after you have installed the binary yourself.
# Run it SERIALLY. Never launch a second ESRGAN job while this is running.
python scripts/upscale_batch.py --limit 20
```

> **Third-party attribution.** Real-ESRGAN and `realesrgan-ncnn-vulkan` are the
> work of their respective authors and are distributed under their own license,
> separate from this toolkit. Downloading, using, and complying with that license
> is your responsibility. Nothing in this repository redistributes that binary.

---

## The "verify the REAL rendered source, not a proxy" gotcha (read this too)

The single most expensive DPI bug in this pipeline was a **measurement that
lied.**

An early audit measured each photo's width/height from the **database record**
and reported every cell as clean — "0 below 240." But the exporter does **not**
render from the database dimensions. Its resolver walks a chain of possible
sources (staged original → archive/zip extract → derivative, then any
crop/watermark override) and renders from whichever file that chain lands on —
which was frequently a **different, smaller file** than the DB row described.

Result: the audit read green while the exporter was actually rendering **dozens
of sub-240-DPI cells** — 54 of them in the original build, clustered in the
photos with the smallest sources.

**Lesson: measure the DPI of the exact file the render path resolves to, not a
proxy record.** `fix_lowdpi_reliable.py` does exactly this — it calls the
exporter's own source resolver (`_full_res(...).render_path`) and measures the
file that resolves to, so its DPI numbers match what actually gets printed. Any
DPI gate that trusts a manifest/DB dimension is checking a proxy and will miss
the cells that matter.

---

## Rescuing old or soft photos

Practical guidance for the hard cases — decades-old scans, tiny web-sized
images, slightly-soft originals:

- **Start from the largest clean source you have.** If you have an original and a
  derivative, upscale from whichever has more real pixels. More starting pixels
  always beats a bigger scale factor.
- **Skip corrupt and near-black sources.** A black or uniform frame is garbage,
  not a low-res photo — upscaling it just produces a bigger garbage frame. Reject
  it and fall back to the next-best source (or leave the cell for manual
  attention).
- **Cap the upscale factor.** Do not chase 300 DPI on a source that would need a
  6x blow-up; the result looks worse than accepting the softness. A modest factor
  that clears the 240 floor is the goal, not perfection.
- **A mild Lanczos up-res is usually the right answer.** Accept that some softness
  is inherent to the original — no upscaler invents detail that was never
  captured. Getting cleanly over the 240 floor, on a forgiving matte paper, is a
  better outcome than a crunchy over-sharpened ESRGAN frame.
