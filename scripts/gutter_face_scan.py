"""Gutter face-scan: find FACES that sit right at the binding (gutter) edge on
inner pages of the rendered book, so we can nudge only those crops away from the
spine. Full-bleed photos crossing the gutter are fine; a FACE swallowed by the
binding curve is not.

Runs cv2 Haar detection on each rendered page (export_book/pages/page-NNN.png),
measures each face's distance to that page's INNER trim edge, and flags faces
within GUTTER_DANGER_IN of it. For each flagged page it writes a zoomed
gutter-strip crop to 09_temp/gutter/ for human/visual confirmation.

Page geometry (matches export_book.py): DOC_PX 3675 = 12.25in @300dpi, BLEED_PX 38.
Imposition: page-001 = front blank leaf, the middle pages are the designed pages
(even = verso/left → gutter on RIGHT; odd = recto/right → gutter on LEFT), and the
last page is the back blank leaf. The designed-page range is derived from the files.
"""
import cv2, os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # repo root (scripts/ is one level down)
PAGES = ROOT / "export_book" / "pages"
OUT = ROOT / "review" / "gutter"
OUT.mkdir(parents=True, exist_ok=True)

DOC_PX = 3675
BLEED_PX = 38
DPI = 300
GUTTER_DANGER_IN = 0.35            # flag a face whose nearest edge is within this of the inner trim edge
DANGER_PX = round(GUTTER_DANGER_IN * DPI)
DET_SCALE = 3                       # detect at 1/3 res for speed, scale coords back

face_cascade = cv2.CascadeClassifier(
    os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
profile_cascade = cv2.CascadeClassifier(
    os.path.join(cv2.data.haarcascades, "haarcascade_profileface.xml"))

inner_trim_right = DOC_PX - BLEED_PX   # 3637  (verso: gutter on the right)
inner_trim_left = BLEED_PX             # 38    (recto: gutter on the left)

_page_files = sorted(PAGES.glob("page-*.png"))
_last_page = int(_page_files[-1].stem.split("-")[1]) if _page_files else 1
flagged = []
for p in range(2, _last_page):         # designed pages only (skip front+back blank leaves)
    img_path = PAGES / f"page-{p:03d}.png"
    if not img_path.is_file():
        continue
    img = cv2.imread(str(img_path))
    if img is None:
        continue
    h, w = img.shape[:2]
    small = cv2.resize(img, (w // DET_SCALE, h // DET_SCALE))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = list(face_cascade.detectMultiScale(gray, 1.1, 6, minSize=(40, 40)))
    faces += list(profile_cascade.detectMultiScale(gray, 1.1, 6, minSize=(40, 40)))
    # also mirror for right-facing profiles
    faces += [(w // DET_SCALE - x - fw, y, fw, fh)
              for (x, y, fw, fh) in profile_cascade.detectMultiScale(
                  cv2.flip(gray, 1), 1.1, 6, minSize=(40, 40))]
    verso = (p % 2 == 0)               # even = left/verso → gutter right
    hits = []
    for (x, y, fw, fh) in faces:
        X, Y, FW, FH = x * DET_SCALE, y * DET_SCALE, fw * DET_SCALE, fh * DET_SCALE
        if verso:
            # face right edge distance INSIDE from the right gutter trim
            dist = inner_trim_right - (X + FW)
        else:
            dist = X - inner_trim_left
        if dist < DANGER_PX:
            hits.append((X, Y, FW, FH, round(dist / DPI, 2)))
    if hits:
        flagged.append((p, verso, hits))
        # write a zoomed gutter strip (inner 2.2in) with face boxes drawn
        strip_in = 2.2
        strip_px = round(strip_in * DPI)
        if verso:
            x0 = DOC_PX - strip_px
            crop = img[:, x0:].copy()
            xoff = x0
        else:
            crop = img[:, :strip_px].copy()
            xoff = 0
        for (X, Y, FW, FH, d) in hits:
            cv2.rectangle(crop, (X - xoff, Y), (X - xoff + FW, Y + FH), (0, 0, 255), 8)
        # draw the inner trim line + the danger boundary
        if verso:
            cv2.line(crop, (inner_trim_right - xoff, 0), (inner_trim_right - xoff, DOC_PX), (0, 255, 0), 4)
            cv2.line(crop, (inner_trim_right - DANGER_PX - xoff, 0), (inner_trim_right - DANGER_PX - xoff, DOC_PX), (0, 180, 255), 3)
        else:
            cv2.line(crop, (inner_trim_left - xoff, 0), (inner_trim_left - xoff, DOC_PX), (0, 255, 0), 4)
            cv2.line(crop, (inner_trim_left + DANGER_PX - xoff, 0), (inner_trim_left + DANGER_PX - xoff, DOC_PX), (0, 180, 255), 3)
        small_out = cv2.resize(crop, (crop.shape[1] // 3, crop.shape[0] // 3))
        cv2.imwrite(str(OUT / f"page-{p:03d}_gutter.png"), small_out)

spread_of = lambda p: (p - 2) // 2      # designed page -> zero-based spread index
print(f"{len(flagged)} pages with a face inside the {GUTTER_DANGER_IN}in gutter zone:\n")
for p, verso, hits in flagged:
    side = "L/verso(gutter=R)" if verso else "R/recto(gutter=L)"
    nearest = min(h[4] for h in hits)
    print(f"  page {p:3d}  spread {spread_of(p):2d}.{ 'left' if verso else 'right'}  {side}  "
          f"{len(hits)} face(s), nearest {nearest}in from gutter trim")
print(f"\nGutter strip crops -> {OUT}")
