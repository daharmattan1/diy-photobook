// Photo-book editor SPA. No external dependencies; ES module served from
// http://127.0.0.1. Renders two views (grid book-map <-> spread detail), a
// full-455 photo tray, pointer drag-drop + click-to-place with relayout-on-drop,
// live DPI warn-on-override, undo/redo, and debounced autosave.

import { getJSON, postJSON } from "/js/api.js";
import {
  cellAsset, cellCaption, cellCrop, cellOverride, makeCell,
  placementIndex, pageAssetList, pageGeometry, deepClone,
} from "/js/book.js";
const CB = String(Date.now());  // per-load thumb cache-buster

const PX_DETAIL = 34; // px per inch in spread detail
const PX_GRID = 8;    // px per inch in the grid book-map

const S = {
  book: null,
  revision: 0,
  constants: null,
  photos: [],
  photoById: new Map(),
  view: "detail",
  currentSpread: 0,
  selected: null,        // {assetId, from:{type:'tray'} | {type:'cell',spreadIndex,side,cellIndex}}
  hideUsed: false,
  filters: { era: null, event: null, tag: null, month: null, favorited: false, usage: null },
  placement: new Map(),
  undo: [],
  redo: [],
  saveTimer: null,
  savePromise: null,
  saving: false,
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, attrs) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

// Last path segment of an original file path (handles both \\ and /).
function basename(path) {
  if (!path) return "";
  const parts = String(path).split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

// ---------------------------------------------------------------- boot -------
async function boot() {
  S.constants = await getJSON("/api/constants");
  const bookResp = await getJSON("/api/book");
  S.book = bookResp.book;
  S.revision = bookResp.book_revision;
  const photoResp = await getJSON("/api/photos");
  S.photos = photoResp.photos;
  S.photoById = new Map(S.photos.map((p) => [p.asset_id, p]));
  S.facets = photoResp.facets;
  recomputePlacement();
  wireToolbar();
  wirePointerDrag();
  renderChips();
  renderTray();
  render();
  updateRevisionLabel();
  document.body.dataset.ready = "true";
}

function recomputePlacement() {
  S.placement = placementIndex(S.book);
}

// A book-wide asset_id -> caption map, snapshotted BEFORE a relayout rebuilds
// its cells (relayout/swap/change-layout returns only asset ids, so captions
// are re-attached by asset_id — they survive rearrangement, they only leave
// with a photo that leaves the book).
function collectCaptions(book) {
  const map = new Map();
  for (const spread of book.spreads || []) {
    for (const side of ["left", "right"]) {
      for (const cell of ((spread[side] || {}).cells || [])) {
        const aid = cellAsset(cell);
        const caption = cellCaption(cell);
        if (aid && caption) map.set(aid, caption);
      }
    }
  }
  return map;
}

// ------------------------------------------------------------- rendering -----
function render() {
  $("view-grid").classList.toggle("active", S.view === "grid");
  $("view-detail").classList.toggle("active", S.view === "detail");
  const canvas = $("canvas");
  canvas.innerHTML = "";
  if (S.view === "grid") renderGrid(canvas);
  else renderDetail(canvas);
}

function miniPage(book, page, widthPx) {
  const scale = widthPx / S.constants.geometry.page_in;
  const wrap = el("div", "mini");
  wrap.style.width = widthPx + "px";
  wrap.style.height = widthPx + "px";
  const geom = pageGeometry(book, page);
  const cells = page.cells || [];
  for (let i = 0; i < geom.length; i += 1) {
    const aid = cellAsset(cells[i]);
    if (!aid) continue;
    const g = geom[i];
    const c = el("div", "mini-cell");
    c.style.left = g[0] * scale + "px";
    c.style.top = g[1] * scale + "px";
    c.style.width = g[2] * scale + "px";
    c.style.height = g[3] * scale + "px";
    const img = el("img", null, { src: `/api/thumb/${aid}?v=${CB}`, loading: "lazy", alt: "" });
    applyCrop(img, cellCrop(cells[i]));
    c.appendChild(img);
    wrap.appendChild(c);
  }
  return wrap;
}

function renderGrid(canvas) {
  const map = el("div", "grid-map");
  const pageW = PX_GRID * S.constants.geometry.page_in;
  S.book.spreads.forEach((spread, si) => {
    const thumb = el("div", "spread-thumb", { "data-spread-index": String(si) });
    if (si === S.currentSpread) thumb.classList.add("current");
    const holder = el("div");
    holder.style.display = "flex";
    holder.appendChild(miniPage(S.book, spread.left, pageW));
    holder.appendChild(miniPage(S.book, spread.right, pageW));
    thumb.appendChild(holder);
    const cap = el("div", "cap");
    const left = el("span"); left.textContent = `#${si + 1} ${spread.chapter || ""}`;
    const right = el("span");
    right.textContent = `${(spread.left.cells || []).length + (spread.right.cells || []).length}p`;
    cap.append(left, right);
    thumb.appendChild(cap);
    thumb.addEventListener("click", () => { S.currentSpread = si; S.view = "detail"; render(); });
    // drag-to-reorder spreads
    thumb.dataset.reorder = "1";
    map.appendChild(thumb);
  });
  canvas.appendChild(map);
}

function renderDetail(canvas) {
  const nav = el("div", "detail-nav");
  const prev = el("button"); prev.textContent = "‹ Prev";
  prev.disabled = S.currentSpread <= 0;
  prev.addEventListener("click", () => { S.currentSpread = Math.max(0, S.currentSpread - 1); render(); });
  const next = el("button"); next.textContent = "Next ›";
  next.disabled = S.currentSpread >= S.book.spreads.length - 1;
  next.addEventListener("click", () => {
    S.currentSpread = Math.min(S.book.spreads.length - 1, S.currentSpread + 1); render();
  });
  const title = el("span", "title");
  const spread = S.book.spreads[S.currentSpread];
  title.textContent = `Spread ${S.currentSpread + 1} / ${S.book.spreads.length} — ${spread.chapter || ""}`;
  nav.append(prev, next, title);
  canvas.appendChild(nav);

  const pageIn = S.constants.geometry.page_in;
  const spreadEl = el("div", "spread");
  spreadEl.style.width = pageIn * 2 * PX_DETAIL + "px";
  spreadEl.style.height = pageIn * PX_DETAIL + "px";
  spreadEl.appendChild(renderPage(spread, "left", 0));
  spreadEl.appendChild(renderPage(spread, "right", pageIn));
  const gutter = el("div", "gutter");
  gutter.style.left = pageIn * PX_DETAIL + "px";
  spreadEl.appendChild(gutter);

  // Cross-page move targets: edge strips flanking the spread. Drop a dragged
  // photo (or click with one selected) to send it to the adjacent spread —
  // repeatable, so related shots can be walked/gathered onto one page fast.
  const row = el("div", "detail-row");
  row.style.height = pageIn * PX_DETAIL + "px";
  for (const dir of [-1, +1]) {
    const ti = S.currentSpread + dir;
    const zone = el("div", "edge-dropzone", { "data-dir": String(dir) });
    if (ti < 0 || ti >= S.book.spreads.length) {
      zone.classList.add("disabled");
      zone.textContent = "";
    } else {
      zone.innerHTML = dir < 0
        ? `<span>◀</span><span>to<br>${ti + 1}</span>`
        : `<span>▶</span><span>to<br>${ti + 1}</span>`;
      zone.title = `Move the dragged/selected photo to spread ${ti + 1}`;
      zone.addEventListener("click", () => {
        if (!S.selected) return;
        const src = S.selected;
        S.selected = null;
        moveToAdjacentSpread(dir, src).catch(reportError);
      });
    }
    if (dir < 0) row.appendChild(zone);
    row.appendChild(dir < 0 ? spreadEl : zone);
  }
  canvas.appendChild(row);
}

function renderPage(spread, side, xOffsetIn) {
  const page = spread[side];
  const pageIn = S.constants.geometry.page_in;
  const pageEl = el("div", "page", { "data-side": side });
  pageEl.style.left = xOffsetIn * PX_DETAIL + "px";
  pageEl.style.width = pageIn * PX_DETAIL + "px";

  const tpl = (S.book.templates || {})[page.template] || {};
  if (tpl.title && page.title) {
    const band = el("div", "title-band");
    band.style.left = 0.18 * PX_DETAIL + "px";
    band.style.top = 0.14 * PX_DETAIL + "px";
    // Cap the band to the reserved title strip (cells start at ~0.77in) so the
    // header can never spill onto the top photos at the editor's page scale.
    band.style.maxHeight = 0.60 * PX_DETAIL + "px";
    const ch = el("span", "ch"); ch.textContent = "Chapter";
    const nm = el("span", "nm"); nm.textContent = page.title;
    band.append(ch, nm);
    pageEl.appendChild(band);
  }

  const geom = pageGeometry(S.book, page);
  const cells = page.cells || [];
  if (geom.length === 0) {
    const hint = el("div", "empty-page-hint");
    hint.textContent = "Empty page — drop or click a photo here";
    pageEl.appendChild(hint);
  }
  for (let i = 0; i < geom.length; i += 1) {
    const aid = cellAsset(cells[i]);
    const g = geom[i];
    const cellEl = el("div", "cell", {
      "data-spread-index": String(S.currentSpread),
      "data-side": side,
      "data-cell-index": String(i),
    });
    if (aid) cellEl.setAttribute("data-asset-id", aid);
    cellEl.style.left = g[0] * PX_DETAIL + "px";
    cellEl.style.top = g[1] * PX_DETAIL + "px";
    cellEl.style.width = g[2] * PX_DETAIL + "px";
    cellEl.style.height = g[3] * PX_DETAIL + "px";
    if (aid) {
      const img = el("img", null, { src: `/api/thumb/${aid}?v=${CB}`, alt: "" });
      applyCrop(img, cellCrop(cells[i]));
      cellEl.appendChild(img);
      const photo = S.photoById.get(aid);
      const dpi = photo ? effectiveDpi(photo, g, cellCrop(cells[i])) : 999;
      const overridden = !!cellOverride(cells[i]);
      if (dpi + 0.1 < S.constants.dpi_ok) {
        cellEl.classList.add("low-dpi");
        if (overridden) cellEl.classList.add("dpi-approved");
        const badge = el("div", "dpi-badge" + (overridden ? " approved" : ""));
        badge.textContent = Math.round(dpi) + (overridden ? " ✓" : " !");
        badge.title = overridden
          ? "Low-res, approved to keep at this size — click to un-approve"
          : `Prints at ${Math.round(dpi)} DPI (below ${S.constants.dpi_ok}). Click to keep anyway (approve override).`;
        badge.addEventListener("pointerdown", (e) => e.stopPropagation());
        badge.addEventListener("click", (e) => {
          e.stopPropagation();
          toggleDpiOverride(S.currentSpread, side, i, dpi);
        });
        cellEl.appendChild(badge);
      }
      // favorite this photo right on the page (no trip to the tray)
      const favBtn = el("div", "cell-fav" + (photo && photo.favorited ? " on" : ""));
      favBtn.textContent = "★";
      favBtn.title = (photo && photo.favorited) ? "Unfavorite" : "Favorite";
      favBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
      favBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleFavorite(aid); });
      cellEl.appendChild(favBtn);
      // optional caption line at the bottom edge (subtle; never covers faces)
      const caption = cellCaption(cells[i]);
      if (caption) {
        const cap = el("div", "cell-caption");
        cap.textContent = caption;
        cellEl.appendChild(cap);
      }
    }
    if (isSelectedCell(side, i)) cellEl.classList.add("selected");
    cellEl.addEventListener("click", () => onCellClick(S.currentSpread, side, i));
    pageEl.appendChild(cellEl);
  }

  // page-level add drop-zone (append a photo to this page)
  if (geom.length > 0 && geom.length < 7) {
    const dz = el("div", "page-dropzone", { "data-side": side });
    dz.textContent = "+ add to page";
    dz.addEventListener("click", () => onPageDropzoneClick(S.currentSpread, side));
    pageEl.appendChild(dz);
  }
  return pageEl;
}

function applyCrop(img, crop) {
  if (!crop) return;
  // Pixel-exact crop-window preview: scale the image so the crop window
  // (x,y,w,h fractions) fills the cell, then offset it — matching the print
  // exporter exactly, instead of the old approximate object-position reposition.
  const w = Number(crop.w) || 1, h = Number(crop.h) || 1;
  const x = Number(crop.x) || 0, y = Number(crop.y) || 0;
  img.style.position = "absolute";
  img.style.objectFit = "fill";
  img.style.width = (100 / w).toFixed(3) + "%";
  img.style.height = (100 / h).toFixed(3) + "%";
  img.style.left = (-100 * x / w).toFixed(3) + "%";
  img.style.top = (-100 * y / h).toFixed(3) + "%";
  img.style.maxWidth = "none";
  img.style.maxHeight = "none";
}

function effectiveDpi(photo, cell, crop) {
  const w = photo.w || 0, h = photo.h || 0;
  const cw = cell[2], ch = cell[3];
  if (!w || !h || !cw || !ch) return 0;
  if (crop) {
    return Math.min((w * crop.w) / cw, (h * crop.h) / ch);
  }
  const sa = w / h, ca = cw / ch;
  let pw, ph;
  if (sa >= ca) { pw = cw; ph = cw / sa; } else { pw = ch * sa; ph = ch; }
  return Math.min(w / pw, h / ph);
}

// ------------------------------------------------------------- tray ----------
function renderChips() {
  const wrap = $("chips");
  wrap.innerHTML = "";
  const groups = [
    ["era", "Era", S.facets.eras],
    ["event", "Event", S.facets.events],
    ["tag", "Tag", S.facets.tags],
    ["month", "Date", S.facets.months],
  ];
  const favLabel = el("div", "chip-group-label"); favLabel.textContent = "Quick";
  wrap.append(favLabel);
  const fav = el("div", "chip" + (S.filters.favorited ? " active" : ""));
  fav.textContent = "★ favorited";
  fav.addEventListener("click", () => { S.filters.favorited = !S.filters.favorited; renderChips(); renderTray(); });
  wrap.append(fav);
  for (const u of ["unused", "used"]) {
    const chip = el("div", "chip" + (S.filters.usage === u ? " active" : ""));
    chip.textContent = u;
    chip.addEventListener("click", () => {
      S.filters.usage = S.filters.usage === u ? null : u;
      renderChips(); renderTray();
    });
    wrap.append(chip);
  }
  for (const [key, label, values] of groups) {
    if (!values || !values.length) continue;
    const lab = el("div", "chip-group-label"); lab.textContent = label;
    wrap.appendChild(lab);
    for (const v of values) {
      const chip = el("div", "chip" + (S.filters[key] === v ? " active" : ""));
      chip.textContent = v;
      chip.addEventListener("click", () => {
        S.filters[key] = S.filters[key] === v ? null : v;
        renderChips(); renderTray();
      });
      wrap.appendChild(chip);
    }
  }
}

function photoMatches(p) {
  const f = S.filters;
  if (f.favorited && !p.favorited) return false;
  if (f.usage) {
    const placed = (S.placement.get(p.asset_id) || []).length > 0;
    if (f.usage === "used" && !placed) return false;
    if (f.usage === "unused" && placed) return false;
  }
  if (f.era && p.era !== f.era) return false;
  if (f.event && p.event !== f.event) return false;
  if (f.tag && !(p.tags || []).includes(f.tag)) return false;
  if (f.month && p.month !== f.month) return false;
  return true;
}

function renderTray() {
  const grid = $("tray-grid");
  grid.innerHTML = "";
  let shown = 0;
  for (const p of S.photos) {
    const uses = S.placement.get(p.asset_id) || [];
    const placed = uses.length > 0;
    if (S.hideUsed && placed) continue;
    if (!photoMatches(p)) continue;
    shown += 1;
    const cell = el("div", "tray-photo" + (placed ? " placed" : ""), {
      "data-asset-id": p.asset_id,
      "data-placed": placed ? "true" : "false",
    });
    if (S.selected && S.selected.assetId === p.asset_id && S.selected.from.type === "tray") {
      cell.classList.add("selected");
    }
    const img = el("img", null, { src: `/api/thumb/${p.asset_id}`, loading: "lazy", alt: "" });
    cell.appendChild(img);
    const favBtn = el("div", "fav-btn" + (p.favorited ? " on" : ""));
    favBtn.textContent = "★";
    favBtn.title = p.favorited ? "Unfavorite" : "Favorite";
    favBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
    favBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleFavorite(p.asset_id); });
    cell.appendChild(favBtn);
    if (p.is_hero) { const h = el("div", "hero"); h.textContent = "hero"; cell.appendChild(h); }
    if (placed) {
      const badge = el("div", "use-badge");
      badge.textContent = "#" + uses.map((u) => u.spreadIndex + 1).join(",");
      badge.title = `placed on spread ${uses.map((u) => u.spreadIndex + 1).join(", ")}`;
      cell.appendChild(badge);
    }
    cell.addEventListener("click", () => onTrayClick(p.asset_id));
    grid.appendChild(cell);
  }
  $("tray-count").textContent = String(shown);
  $("tray-total").textContent = String(S.photos.length);
  renderInspector();
}

async function toggleFavorite(assetId) {
  const p = S.photoById.get(assetId);
  if (!p) return;
  const next = !p.favorited;
  p.favorited = next; // optimistic
  renderTray(); render();
  const { status } = await postJSON("/api/favorite", { asset_id: assetId, favorited: next });
  if (status !== 200) {
    p.favorited = !next; // revert on failure
    renderTray(); render();
    flashStatus("favorite failed");
  }
}

// ------------------------------------------------------- selection / place ---
function isSelectedCell(side, cellIndex) {
  return S.selected && S.selected.from.type === "cell"
    && S.selected.from.spreadIndex === S.currentSpread
    && S.selected.from.side === side && S.selected.from.cellIndex === cellIndex;
}

function clearSelection() {
  S.selected = null;
  refreshSelectionUI();
}

function refreshSelectionUI() {
  document.querySelectorAll(".cell.selected").forEach((n) => n.classList.remove("selected"));
  document.querySelectorAll(".tray-photo.selected").forEach((n) => n.classList.remove("selected"));
  renderInspector();
  if (!S.selected) return;
  if (S.selected.from.type === "tray") {
    const node = document.querySelector(`.tray-photo[data-asset-id="${S.selected.assetId}"]`);
    if (node) node.classList.add("selected");
  } else {
    const f = S.selected.from;
    if (f.spreadIndex === S.currentSpread) {
      const node = document.querySelector(
        `.cell[data-side="${f.side}"][data-cell-index="${f.cellIndex}"]`);
      if (node) node.classList.add("selected");
    }
  }
}

// ------------------------------------------------------- inspector (A1) ------
// The "which photo is selected + what can I do to it" surface, at the top of
// the tray. Re-rendered on every selection change (refreshSelectionUI) and on
// every book mutation (renderTray), so it always mirrors S.selected.
function renderInspector() {
  const box = $("inspector");
  if (!box) return;
  box.innerHTML = "";
  if (!S.selected) { box.hidden = true; return; }
  box.hidden = false;
  const assetId = S.selected.assetId;
  const from = S.selected.from;
  const photo = S.photoById.get(assetId);

  const thumb = el("div", "insp-thumb");
  thumb.appendChild(el("img", null, { src: `/api/thumb/${assetId}`, alt: "" }));
  box.appendChild(thumb);

  const meta = el("div", "insp-meta");
  const name = el("div", "insp-name");
  name.textContent = basename(photo && photo.orig_path) || assetId.slice(0, 12);
  name.title = (photo && photo.orig_path) || assetId;
  meta.appendChild(name);

  const loc = el("div", "insp-loc");
  loc.textContent = from.type === "cell"
    ? `spread ${from.spreadIndex + 1} · ${from.side} · cell ${from.cellIndex + 1}`
    : "in tray";
  meta.appendChild(loc);

  if (from.type === "cell") {
    const page = S.book.spreads[from.spreadIndex][from.side];
    const g = pageGeometry(S.book, page)[from.cellIndex];
    if (g && photo) {
      const dpi = effectiveDpi(photo, g, cellCrop((page.cells || [])[from.cellIndex]));
      const low = dpi + 0.1 < S.constants.dpi_ok;
      const d = el("div", "insp-dpi" + (low ? " warn" : ""));
      d.textContent = `${Math.round(dpi)} DPI` + (low ? " · low-res" : "");
      meta.appendChild(d);
    }
  }

  const actions = el("div", "insp-actions");
  const isFav = !!(photo && photo.favorited);
  const favBtn = el("button", "insp-btn" + (isFav ? " active" : ""));
  favBtn.textContent = isFav ? "★ Favorited" : "★ Favorite";
  favBtn.addEventListener("click", () => toggleFavorite(assetId));
  actions.appendChild(favBtn);

  // Promote to hero: ONE action — mark is_hero in the manifest AND give the
  // photo its own page (heroes are prominent, period). Demote just clears the flag.
  const isHero = !!(photo && photo.is_hero);
  const heroBtn = el("button", "insp-btn insp-hero" + (isHero ? " active" : ""));
  heroBtn.textContent = isHero ? "◆ Hero (undo)" : "◆ Promote to hero";
  heroBtn.title = isHero
    ? "Clear the hero mark (the photo stays where it is)"
    : "Mark as hero and give it its own page — heroes stay big through every relayout";
  heroBtn.addEventListener("click", () =>
    (isHero ? demoteHero(assetId) : promoteHero(assetId, from)).catch(reportError));
  actions.appendChild(heroBtn);

  if (from.type === "cell") {
    const bigBtn = el("button", "insp-btn");
    bigBtn.textContent = "⤢ Make big";
    bigBtn.title = "Re-pack this page so this photo gets the biggest cell";
    bigBtn.addEventListener("click", () =>
      makeBig(from.spreadIndex, from.side, from.cellIndex, assetId).catch(reportError));
    actions.appendChild(bigBtn);

    const layoutBtn = el("button", "insp-btn");
    layoutBtn.textContent = "▦ Change layout";
    layoutBtn.title = "Pick a different arrangement for this page";
    layoutBtn.addEventListener("click", () =>
      openLayoutModal(from.spreadIndex, from.side).catch(reportError));
    actions.appendChild(layoutBtn);

    const removeBtn = el("button", "insp-btn insp-remove");
    removeBtn.textContent = "− Remove from page";
    removeBtn.title = "Take this photo off the page — it returns to the tray, still available";
    removeBtn.addEventListener("click", () =>
      removeFromPage(from.spreadIndex, from.side, from.cellIndex).catch(reportError));
    actions.appendChild(removeBtn);
  }

  const purgeBtn = el("button", "insp-btn insp-purge");
  purgeBtn.textContent = "⊘ Remove from book";
  purgeBtn.title = "Remove from the ENTIRE book and never show it again — flags it in the manifest so a recompose can't bring it back";
  purgeBtn.addEventListener("click", () => removeFromBook(assetId, from).catch(reportError));
  actions.appendChild(purgeBtn);

  const deselect = el("button", "insp-btn");
  deselect.textContent = "✕ Deselect";
  deselect.addEventListener("click", () => clearSelection());
  actions.appendChild(deselect);

  meta.appendChild(actions);

  // Optional caption, bound to the selected cell (placed photos only). Typing
  // sets cell.caption + schedules a save, and live-patches the on-page caption
  // line without a full re-render (so the input keeps focus while you type).
  if (from.type === "cell") {
    const page = S.book.spreads[from.spreadIndex][from.side];
    const cellObj = (page.cells || [])[from.cellIndex];
    if (cellObj && typeof cellObj === "object") {
      const capWrap = el("div", "insp-caption");
      const capInput = el("input", "insp-caption-input",
        { type: "text", placeholder: "Caption…", maxlength: "140" });
      capInput.value = cellCaption(cellObj) || "";
      capInput.addEventListener("input", () => {
        const value = capInput.value;
        cellObj.caption = value.trim() ? value : null;
        updateLiveCaption(from, cellObj.caption);
        scheduleSave();
      });
      capWrap.appendChild(capInput);
      meta.appendChild(capWrap);
    }
  }

  box.appendChild(meta);
}

// Patch (or add/remove) the caption line on the currently rendered cell without
// re-rendering the whole spread — keeps the inspector input focused while typing.
function updateLiveCaption(from, caption) {
  if (from.spreadIndex !== S.currentSpread) return;
  const cellNode = document.querySelector(
    `.cell[data-side="${from.side}"][data-cell-index="${from.cellIndex}"]`);
  if (!cellNode) return;
  let capNode = cellNode.querySelector(".cell-caption");
  if (caption) {
    if (!capNode) { capNode = el("div", "cell-caption"); cellNode.appendChild(capNode); }
    capNode.textContent = caption;
  } else if (capNode) {
    capNode.remove();
  }
}

function onTrayClick(assetId) {
  if (S.selected && S.selected.assetId === assetId && S.selected.from.type === "tray") {
    clearSelection();
  } else {
    S.selected = { assetId, from: { type: "tray" } };
    refreshSelectionUI();
  }
}

function onCellClick(spreadIndex, side, cellIndex) {
  const page = S.book.spreads[spreadIndex][side];
  const occupant = cellAsset((page.cells || [])[cellIndex]);
  if (S.selected) {
    const src = S.selected;
    S.selected = null;
    dropOnto({ spreadIndex, side, cellIndex }, src).catch(reportError);
  } else if (occupant) {
    S.selected = { assetId: occupant, from: { type: "cell", spreadIndex, side, cellIndex } };
    refreshSelectionUI();
  }
}

function onPageDropzoneClick(spreadIndex, side) {
  if (!S.selected) return;
  const src = S.selected;
  S.selected = null;
  appendToPage(spreadIndex, side, src).catch(reportError);
}

// Find where an asset is currently placed (first use), or null.
function findPlacement(assetId) {
  const uses = S.placement.get(assetId);
  return uses && uses.length ? uses[0] : null;
}

// Core drop: place/replace/swap/evict, then relayout affected spread(s).
async function dropOnto(target, source) {
  const assetId = source.assetId;
  const existing = findPlacement(assetId); // where the dragged asset currently sits
  const targetPage = S.book.spreads[target.spreadIndex][target.side];
  const occupant = cellAsset((targetPage.cells || [])[target.cellIndex]);

  if (existing && existing.spreadIndex === target.spreadIndex
      && existing.side === target.side && existing.cellIndex === target.cellIndex) {
    return; // dropped onto itself
  }

  pushUndo();

  const affected = new Set();
  const listOf = (si, side) => pageAssetList(S.book.spreads[si][side]);
  const setList = (si, side, list) => { S.book.spreads[si][side]._pendingList = list; };

  // Snapshot lists we will mutate.
  const tList = listOf(target.spreadIndex, target.side);

  if (existing) {
    const sList = (existing.spreadIndex === target.spreadIndex && existing.side === target.side)
      ? tList : listOf(existing.spreadIndex, existing.side);
    // Swap the dragged asset (at existing) with the occupant (at target).
    if (existing.spreadIndex === target.spreadIndex && existing.side === target.side) {
      const a = existing.cellIndex, b = target.cellIndex;
      [sList[a], sList[b]] = [sList[b], sList[a]];
      setList(target.spreadIndex, target.side, sList);
    } else {
      tList[target.cellIndex] = assetId;
      if (occupant) sList[existing.cellIndex] = occupant;
      else sList.splice(existing.cellIndex, 1);
      setList(target.spreadIndex, target.side, tList);
      setList(existing.spreadIndex, existing.side, sList);
    }
    affected.add(existing.spreadIndex);
    affected.add(target.spreadIndex);
  } else {
    // Source came from the tray and is unplaced: place, evicting the occupant.
    tList[target.cellIndex] = assetId;
    setList(target.spreadIndex, target.side, tList);
    affected.add(target.spreadIndex);
  }

  await relayoutAffected(affected);
}

async function appendToPage(spreadIndex, side, source) {
  const assetId = source.assetId;
  const page = S.book.spreads[spreadIndex][side];
  const list = pageAssetList(page);
  if (list.length >= 7) { flashStatus("page is full (7 max)"); return; }
  pushUndo();
  const existing = findPlacement(assetId);
  const affected = new Set([spreadIndex]);
  // Remove from prior placement if any.
  if (existing) {
    const sList = pageAssetList(S.book.spreads[existing.spreadIndex][existing.side]);
    sList.splice(existing.cellIndex, 1);
    S.book.spreads[existing.spreadIndex][existing.side]._pendingList = sList;
    affected.add(existing.spreadIndex);
  }
  const tList = (spreadIndex === (existing && existing.spreadIndex) && side === (existing && existing.side))
    ? S.book.spreads[spreadIndex][side]._pendingList
    : pageAssetList(page);
  tList.push(assetId);
  S.book.spreads[spreadIndex][side]._pendingList = tList;
  await relayoutAffected(affected);
}

// Remove a photo from its page (evict to the tray — it stays in the interface,
// just off the page). The page re-packs with the remaining photos.
async function removeFromPage(spreadIndex, side, cellIndex) {
  const page = S.book.spreads[spreadIndex][side];
  const list = pageAssetList(page);
  if (cellIndex < 0 || cellIndex >= list.length) return;
  pushUndo();
  list.splice(cellIndex, 1);
  page._pendingList = list;
  clearSelection();
  await relayoutAffected(new Set([spreadIndex]));
  flashStatus("removed from page (still in tray)");
}

// Remove a photo from the ENTIRE book and never show it again: take it off the
// page (if placed), drop it from the tray, and flag it rejected in the manifest
// (review_status='reject') so a future recompose can never bring it back.
async function removeFromBook(assetId, from) {
  if (!window.confirm("Remove this photo from the entire book and never show it again?")) return;
  pushUndo();
  // 1) take it off its page if it's currently placed
  const affected = new Set();
  if (from && from.type === "cell") {
    const page = S.book.spreads[from.spreadIndex][from.side];
    const list = pageAssetList(page);
    if (from.cellIndex >= 0 && from.cellIndex < list.length) {
      list.splice(from.cellIndex, 1);
      page._pendingList = list;
      affected.add(from.spreadIndex);
    }
  }
  // 2) persist the permanent "don't show again" flag
  const { status } = await postJSON("/api/reject", { asset_id: assetId });
  if (status !== 200) { reportError(new Error("reject failed (" + status + ")")); return; }
  // 3) drop it from the tray / photo pool so it disappears from the interface
  S.photos = S.photos.filter((p) => p.asset_id !== assetId);
  S.photoById.delete(assetId);
  clearSelection();
  if (affected.size) await relayoutAffected(affected);
  renderTray();
  render();
  flashStatus("removed from the whole book (won't show again)");
}

// ------------------------------------------------------------- hero ----------
async function setHero(assetId, val) {
  const { status } = await postJSON("/api/hero", { asset_id: assetId, is_hero: val });
  if (status !== 200) throw new Error("hero flag failed (" + status + ")");
  const p = S.photoById.get(assetId);
  if (p) p.is_hero = val;
}

// Build an empty spread (both pages blank) after `at`, inheriting its chapter.
// Returns the new spread's index. Mirrors addSpread() but without undo/save —
// callers compose it into a larger undoable action.
function insertEmptySpreadAfter(at) {
  const chapter = (S.book.spreads[at] && S.book.spreads[at].chapter) || null;
  const id = (window.crypto && window.crypto.randomUUID)
    ? window.crypto.randomUUID() : "sp-" + Date.now().toString(16);
  const mk = (suffix) => {
    const tplId = `manual_${id}_${suffix}`;
    S.book.templates[tplId] = { cells: [], weight: 1, title: false, title_band: false,
      default_fit: "contain", page_bg: S.constants.off_white, cell_bg: S.constants.off_white };
    return { template: tplId, cells: [], story_assets: [], fits: [], title: null,
      intentional_hero: false, layout_style: "empty" };
  };
  S.book.spreads.splice(at + 1, 0,
    { id, kind: "content", chapter, left: mk("L"), right: mk("R") });
  return at + 1;
}

// A2/the owner's "make big, for real": mark hero + move to its OWN page (a fresh
// spread right after the current one). A tray (unplaced) photo just gets the
// flag — place it anywhere and it stays dominant through relayouts.
async function promoteHero(assetId, from) {
  await setHero(assetId, true);
  if (from && from.type === "cell") {
    const page = S.book.spreads[from.spreadIndex][from.side];
    const list = pageAssetList(page);
    if (list.length > 1 && list[from.cellIndex] === assetId) {
      pushUndo();
      list.splice(from.cellIndex, 1);
      page._pendingList = list;
      const ni = insertEmptySpreadAfter(from.spreadIndex);
      S.book.spreads[ni].left._pendingList = [assetId];
      clearSelection();
      await relayoutAffected(new Set([from.spreadIndex, ni]));
      S.currentSpread = ni;
      render();
      flashStatus("hero — moved to its own page");
      return;
    }
  }
  renderTray(); render();
  flashStatus("marked as hero");
}

async function demoteHero(assetId) {
  await setHero(assetId, false);
  renderTray(); render();
  flashStatus("hero mark cleared");
}

// ------------------------------------------- cross-page move (gather shots) --
// Move a photo to an ADJACENT spread from detail view (edge drop zones / edge
// click with a selection). dir = -1 (prev) or +1 (next). Lands on the nearest
// page: prev spread's right page, next spread's left page; falls over to the
// other page when the nearest is full.
async function moveToAdjacentSpread(dir, source) {
  const ti = S.currentSpread + dir;
  if (ti < 0 || ti >= S.book.spreads.length) { flashStatus("no spread there"); return; }
  const spread = S.book.spreads[ti];
  const near = dir < 0 ? "right" : "left";
  const far = dir < 0 ? "left" : "right";
  let side = near;
  if (pageAssetList(spread[near]).length >= 7) {
    if (pageAssetList(spread[far]).length >= 7) { flashStatus("that spread is full"); return; }
    side = far;
  }
  await appendToPage(ti, side, source);
  flashStatus(`moved to spread ${ti + 1}`);
}

// Relayout each affected spread using the pending per-page asset lists.
async function relayoutAffected(spreadIndices) {
  const lowCells = [];
  // Snapshot captions up front (by asset_id) so a swap that moves a photo across
  // pages carries its caption with it, not just a same-page re-pack.
  const captionMap = collectCaptions(S.book);
  for (const si of spreadIndices) {
    const spread = S.book.spreads[si];
    const leftList = spread.left._pendingList || pageAssetList(spread.left);
    const rightList = spread.right._pendingList || pageAssetList(spread.right);
    delete spread.left._pendingList;
    delete spread.right._pendingList;
    const leftTitle = !!(templateTitle(spread.left));
    const rightTitle = !!(templateTitle(spread.right));
    const { status, data } = await postJSON("/api/relayout", {
      spread_index: si,
      left_assets: leftList.filter(Boolean),
      right_assets: rightList.filter(Boolean),
      left_title: leftTitle,
      right_title: rightTitle,
    });
    if (status !== 200) { flashStatus("relayout failed"); throw new Error("relayout " + status); }
    applyRelayout(si, "left", data.left, lowCells, captionMap);
    applyRelayout(si, "right", data.right, lowCells, captionMap);
  }
  recomputePlacement();
  render();
  renderTray();

  if (lowCells.length) {
    const keep = await confirmLowDpi(lowCells);
    if (!keep) { undo(); return; }
    // record an override on each low cell and re-render marks
    for (const lc of lowCells) markOverride(lc);
    render(); renderTray();
  }
  scheduleSave();
}

function templateTitle(page) {
  const tpl = (S.book.templates || {})[page.template] || {};
  return tpl.title && page.title;
}

function applyRelayout(spreadIndex, side, sideData, lowCells, captionMap) {
  const spread = S.book.spreads[spreadIndex];
  const page = spread[side];
  const tplId = page.template;
  if (!sideData || !sideData.cells.length) {
    // Emptied page: no template cells, no assets.
    (S.book.templates[tplId] = S.book.templates[tplId] || {}).cells = [];
    page.cells = [];
    page.fits = [];
    return;
  }
  const tpl = (S.book.templates[tplId] = S.book.templates[tplId] || {});
  tpl.cells = sideData.cells;
  tpl.weight = sideData.weight;
  tpl.layout_style = sideData.layout_style;
  tpl.bleed = !!sideData.bleed;                       // keep FULL-BLEED on edit (E2 fix)
  tpl.smart_fill = sideData.layout_style === "smart_fill";
  tpl.page_bg = S.constants.off_white;
  tpl.cell_bg = S.constants.off_white;
  // Apply the smart_fill cover-crops — do NOT reset them (that was the un-fill bug
  // that reverted edited pages to wide margins). Re-attach captions by asset_id.
  const crops = sideData.crops || [];
  page.cells = sideData.assets.map(
    (aid, i) => makeCell(aid, crops[i] || null, null, null, (captionMap && captionMap.get(aid)) || null));
  page.fits = sideData.fits || sideData.assets.map(() => "contain");
  page.manual_crop = crops.some((c) => c);
  page.layout_style = sideData.layout_style;
  sideData.dpi.forEach((dpi, i) => {
    if (dpi + 0.1 < S.constants.dpi_ok) {
      lowCells.push({ spreadIndex, side, cellIndex: i, assetId: sideData.assets[i], dpi });
    }
  });
}

function markOverride(lc) {
  const page = S.book.spreads[lc.spreadIndex][lc.side];
  const cell = page.cells[lc.cellIndex];
  if (cell && typeof cell === "object") {
    cell.dpi_override = {
      approved_by: "owner",
      dpi: Math.round(lc.dpi * 10) / 10,
      date: new Date().toISOString(),
    };
  }
}

// Click a low-DPI badge to approve keeping the photo at this size (or un-approve).
function toggleDpiOverride(spreadIndex, side, cellIndex, dpi) {
  const page = S.book.spreads[spreadIndex] && S.book.spreads[spreadIndex][side];
  const cell = page && page.cells[cellIndex];
  if (!cell || typeof cell !== "object") return;
  pushUndo();
  if (cellOverride(cell)) {
    cell.dpi_override = null;
  } else {
    cell.dpi_override = {
      approved_by: "owner",
      dpi: Math.round(dpi * 10) / 10,
      date: new Date().toISOString(),
    };
  }
  render();
  scheduleSave();
}

// Bulk-approve every low-DPI cell the export gate reported (keep-forever escape
// hatch). Cells arrive as {spread, side, cell, dpi, asset_id} from /api/export.
function approveAllLowDpi(cells) {
  pushUndo();
  let n = 0;
  for (const lc of cells || []) {
    const page = S.book.spreads[lc.spread] && S.book.spreads[lc.spread][lc.side];
    const cell = page && page.cells[lc.cell];
    if (cell && typeof cell === "object") {
      cell.dpi_override = {
        approved_by: "owner",
        dpi: Math.round(lc.dpi * 10) / 10,
        date: new Date().toISOString(),
      };
      n += 1;
    }
  }
  render();
  renderTray();
  return n;
}

// ---------------------------------------------- layout picker (A3) + big (A2) -
// A mini preview of ONE candidate layout: draw each cell rectangle to scale with
// its photo thumbnail (object-fit: contain). Mirrors miniPage() but takes raw
// composer cells + assets rather than a book page.
function miniLayout(cells, assets, widthPx) {
  const pageIn = S.constants.geometry.page_in;
  const scale = widthPx / pageIn;
  const wrap = el("div", "mini");
  wrap.style.width = widthPx + "px";
  wrap.style.height = widthPx + "px";
  for (let i = 0; i < cells.length; i += 1) {
    const g = cells[i];
    const c = el("div", "mini-cell");
    c.style.left = g[0] * scale + "px";
    c.style.top = g[1] * scale + "px";
    c.style.width = g[2] * scale + "px";
    c.style.height = g[3] * scale + "px";
    const aid = assets[i];
    if (aid) c.appendChild(el("img", null, { src: `/api/thumb/${aid}?v=${CB}`, loading: "lazy", alt: "" }));
    wrap.appendChild(c);
  }
  return wrap;
}

async function fetchLayouts(spreadIndex, side) {
  const { status, data } = await postJSON("/api/layouts", { spread_index: spreadIndex, side });
  if (status !== 200) { flashStatus("layout fetch failed"); return null; }
  return data.layouts || [];
}

// A3: open the picker, render each candidate as a clickable mini, apply on click.
async function openLayoutModal(spreadIndex, side) {
  const layouts = await fetchLayouts(spreadIndex, side);
  if (layouts === null) return;
  const grid = $("layout-grid");
  grid.innerHTML = "";
  $("layout-title").textContent = `Change layout — spread ${spreadIndex + 1} ${side}`;
  $("layout-msg").textContent = layouts.length
    ? "Pick a layout to apply. Higher coverage means less white space."
    : "No alternate layouts for this page.";
  for (const layout of layouts) {
    const opt = el("div", "layout-option");
    opt.appendChild(miniLayout(layout.cells, layout.assets, 150));
    const cap = el("div", "layout-cap");
    cap.textContent = `${layout.assets.length}-up · ${Math.round(layout.coverage * 100)}% fill`;
    opt.appendChild(cap);
    opt.addEventListener("click", () => {
      $("layout-modal").classList.remove("open");
      applyLayoutCandidate(spreadIndex, side, layout).catch(reportError);
    });
    grid.appendChild(opt);
  }
  $("layout-modal").classList.add("open");
}

// A2: re-pack the page so the selected photo lands in the biggest cell.
async function makeBig(spreadIndex, side, cellIndex, assetId) {
  const page = S.book.spreads[spreadIndex][side];
  const geom = pageGeometry(S.book, page);
  // Already occupying this page's largest cell? Nothing bigger to give it.
  if (geom.length) {
    let maxIdx = 0, maxArea = -1;
    for (let i = 0; i < geom.length; i += 1) {
      const a = geom[i][2] * geom[i][3];
      if (a > maxArea) { maxArea = a; maxIdx = i; }
    }
    if (cellAsset((page.cells || [])[maxIdx]) === assetId) {
      flashStatus("already the biggest here");
      return;
    }
  }
  const layouts = await fetchLayouts(spreadIndex, side);
  if (layouts === null) return;
  if (!layouts.length) { flashStatus("no other layout"); return; }
  // Prefer a layout that puts this photo in the largest cell (highest coverage
  // wins the tie); else the one that maximizes its printed area.
  const inBiggest = layouts.filter((l) => l.max_cell_asset === assetId);
  let choice;
  if (inBiggest.length) {
    choice = inBiggest.reduce((a, b) => (b.coverage > a.coverage ? b : a));
  } else {
    choice = layouts.reduce((a, b) =>
      ((b.feature_area[assetId] || 0) > (a.feature_area[assetId] || 0) ? b : a));
  }
  await applyLayoutCandidate(spreadIndex, side, choice);
}

// Apply a chosen candidate to one page via the SAME machinery as relayout:
// applyRelayout re-fits cells + records low-DPI, then the low-DPI confirm gate.
async function applyLayoutCandidate(spreadIndex, side, candidate) {
  pushUndo();
  const lowCells = [];
  const captionMap = collectCaptions(S.book);
  applyRelayout(spreadIndex, side, {
    assets: candidate.assets,
    cells: candidate.cells,
    dpi: candidate.dpi,
    weight: candidate.weight,
    layout_style: candidate.layout_style,
  }, lowCells, captionMap);
  recomputePlacement();
  render();
  renderTray();
  if (lowCells.length) {
    const keep = await confirmLowDpi(lowCells);
    if (!keep) { undo(); return; }
    for (const lc of lowCells) markOverride(lc);
    render(); renderTray();
  }
  scheduleSave();
}

// ------------------------------------------------------------- add/delete ----
function addSpread() {
  pushUndo();
  const at = S.currentSpread;
  const chapter = (S.book.spreads[at] && S.book.spreads[at].chapter) || null;
  const id = (window.crypto && window.crypto.randomUUID)
    ? window.crypto.randomUUID() : "sp-" + Date.now().toString(16);
  const mk = (suffix) => {
    const tplId = `manual_${id}_${suffix}`;
    S.book.templates[tplId] = { cells: [], weight: 1, title: false, title_band: false,
      default_fit: "contain", page_bg: S.constants.off_white, cell_bg: S.constants.off_white };
    return { template: tplId, cells: [], story_assets: [], fits: [], title: null,
      intentional_hero: false, layout_style: "empty" };
  };
  const spread = { id, kind: "content", chapter, left: mk("L"), right: mk("R") };
  S.book.spreads.splice(at + 1, 0, spread);
  S.currentSpread = at + 1;
  recomputePlacement();
  render(); renderTray();
  scheduleSave();
}

function deleteSpread() {
  if (S.book.spreads.length <= 1) { flashStatus("cannot delete the last spread"); return; }
  pushUndo();
  const removed = S.book.spreads.splice(S.currentSpread, 1)[0];
  if (removed) {
    for (const side of ["left", "right"]) {
      const tplId = removed[side] && removed[side].template;
      if (tplId && S.book.templates[tplId]) delete S.book.templates[tplId];
    }
  }
  S.currentSpread = Math.min(S.currentSpread, S.book.spreads.length - 1);
  recomputePlacement();
  render(); renderTray();
  scheduleSave();
}

// reorder spreads (used by drag-reorder in grid)
function moveSpread(from, to) {
  if (from === to) return;
  pushUndo();
  const [sp] = S.book.spreads.splice(from, 1);
  S.book.spreads.splice(to, 0, sp);
  S.currentSpread = to;
  recomputePlacement();
  render(); renderTray();
  scheduleSave();
}

// ------------------------------------------------------------- undo/redo -----
function pushUndo() {
  S.undo.push(JSON.stringify(S.book));
  if (S.undo.length > 100) S.undo.shift();
  S.redo.length = 0;
  updateUndoButtons();
}

function undo() {
  if (!S.undo.length) return;
  S.redo.push(JSON.stringify(S.book));
  S.book = JSON.parse(S.undo.pop());
  S.currentSpread = Math.min(S.currentSpread, S.book.spreads.length - 1);
  S.selected = null;
  recomputePlacement();
  render(); renderTray(); updateUndoButtons();
  scheduleSave();
}

function redo() {
  if (!S.redo.length) return;
  S.undo.push(JSON.stringify(S.book));
  S.book = JSON.parse(S.redo.pop());
  S.currentSpread = Math.min(S.currentSpread, S.book.spreads.length - 1);
  S.selected = null;
  recomputePlacement();
  render(); renderTray(); updateUndoButtons();
  scheduleSave();
}

function updateUndoButtons() {
  $("undo").disabled = S.undo.length === 0;
  $("redo").disabled = S.redo.length === 0;
}

// ------------------------------------------------------------- autosave ------
function scheduleSave() {
  if (S.saveTimer) clearTimeout(S.saveTimer);
  setSaveStatus("saving", "saving…");
  S.saveTimer = setTimeout(() => { flushSave().catch(reportError); }, 500);
}

async function flushSave() {
  if (S.saveTimer) { clearTimeout(S.saveTimer); S.saveTimer = null; }
  // serialize saves
  if (S.savePromise) { await S.savePromise.catch(() => {}); }
  S.savePromise = (async () => {
    S.saving = true;
    const { status, data } = await postJSON("/api/book", {
      base_revision: S.revision, book: S.book,
    });
    if (status === 200) {
      S.revision = data.book_revision;
      S.book.meta = S.book.meta || {};
      S.book.meta.book_revision = S.revision;
      S.book.meta.print_ready = data.print_ready;
      updateRevisionLabel();
      setSaveStatus("", `saved · rev ${S.revision}`);
    } else if (status === 409) {
      // stale — reload from server (single client should not hit this)
      const fresh = await getJSON("/api/book");
      S.book = fresh.book; S.revision = fresh.book_revision;
      recomputePlacement(); render(); renderTray(); updateRevisionLabel();
      setSaveStatus("error", "reloaded (was stale)");
    } else {
      setSaveStatus("error", "save failed");
    }
    S.saving = false;
  })();
  return S.savePromise;
}

function updateRevisionLabel() {
  const node = $("book-revision");
  if (node) node.textContent = String(S.revision);
}

function setSaveStatus(cls, text) {
  const node = $("save-status");
  if (!node) return;
  node.className = "save-status" + (cls ? " " + cls : "");
  node.textContent = text;
}

function flashStatus(text) {
  setSaveStatus("error", text);
  setTimeout(() => setSaveStatus("", `rev ${S.revision}`), 1600);
}

// ------------------------------------------------------------- DPI modal -----
function confirmLowDpi(lowCells) {
  return new Promise((resolve) => {
    const modal = $("dpi-modal");
    $("dpi-modal-msg").textContent =
      `${lowCells.length} placement(s) print below ${S.constants.dpi_ok} DPI. ` +
      "Place anyway (records an override) or undo?";
    const list = $("dpi-modal-list");
    list.innerHTML = "";
    for (const lc of lowCells) {
      const li = el("li");
      li.textContent = `spread ${lc.spreadIndex + 1} ${lc.side} — ${Math.round(lc.dpi)} DPI`;
      list.appendChild(li);
    }
    modal.classList.add("open");
    const done = (keep) => {
      modal.classList.remove("open");
      $("dpi-confirm").onclick = null;
      $("dpi-cancel").onclick = null;
      resolve(keep);
    };
    $("dpi-confirm").onclick = () => done(true);
    $("dpi-cancel").onclick = () => done(false);
  });
}

// ------------------------------------------------------------- export --------
async function doExport() {
  setSaveStatus("saving", "exporting…");
  await flushSave();
  const { status, data } = await postJSON("/api/export", {});
  const modal = $("export-modal");
  const list = $("export-list");
  const approveBtn = $("export-approve-all");
  list.innerHTML = "";
  approveBtn.style.display = "none";
  S._pendingLowDpi = null;
  if (status === 200) {
    $("export-title").textContent = "Export complete";
    $("export-msg").textContent =
      `${data.page_count} pages at ${data.dpi} DPI. Output in export_book/.`;
  } else if (status === 409) {
    const cells = data.low_dpi_cells || [];
    $("export-title").textContent = "Not print-ready";
    $("export-msg").textContent =
      `${cells.length} cell(s) print below ${S.constants.dpi_ok} DPI. Approve the ones ` +
      "you want to keep soft (click each ✓ badge on the page), or approve them all here.";
    for (const lc of cells.slice(0, 40)) {
      const li = el("li");
      li.textContent = `spread ${lc.spread + 1} ${lc.side} cell ${lc.cell} — ${lc.dpi} DPI (${lc.asset_id.slice(0, 8)})`;
      list.appendChild(li);
    }
    if (cells.length > 40) {
      const li = el("li");
      li.textContent = `…and ${cells.length - 40} more`;
      list.appendChild(li);
    }
    if (cells.length) {
      S._pendingLowDpi = cells;
      approveBtn.style.display = "";
      approveBtn.textContent = `Approve all ${cells.length} & export`;
    }
  } else {
    $("export-title").textContent = "Export error";
    $("export-msg").textContent = data.error || "Unknown error.";
  }
  modal.classList.add("open");
  setSaveStatus("", `rev ${S.revision}`);
}

// ------------------------------------------------------------- toolbar -------
function wireToolbar() {
  $("view-grid").addEventListener("click", () => { S.view = "grid"; render(); });
  $("view-detail").addEventListener("click", () => { S.view = "detail"; render(); });
  $("undo").addEventListener("click", () => undo());
  $("redo").addEventListener("click", () => redo());
  $("add-spread").addEventListener("click", () => addSpread());
  $("delete-spread").addEventListener("click", () => deleteSpread());
  $("hide-used").addEventListener("click", () => {
    S.hideUsed = !S.hideUsed;
    $("hide-used").classList.toggle("active", S.hideUsed);
    renderTray();
  });
  $("clear-filters").addEventListener("click", () => {
    S.filters = { era: null, event: null, tag: null, month: null, favorited: false, usage: null };
    renderChips(); renderTray();
  });
  $("export").addEventListener("click", () => doExport().catch(reportError));
  $("export-close").addEventListener("click", () => $("export-modal").classList.remove("open"));
  $("layout-close").addEventListener("click", () => $("layout-modal").classList.remove("open"));
  // A6: click empty canvas background (not a cell/photo/control) -> cancel selection.
  $("canvas").addEventListener("click", (e) => {
    if (!S.selected) return;
    if (e.target.closest(".cell, button, .spread-thumb, .page-dropzone")) return;
    clearSelection();
  });
  $("export-approve-all").addEventListener("click", async () => {
    const cells = S._pendingLowDpi;
    if (!cells || !cells.length) return;
    const n = approveAllLowDpi(cells);
    flashStatus(`approved ${n} low-DPI cell(s)`);
    $("export-modal").classList.remove("open");
    await flushSave();
    await doExport().catch(reportError);
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); }
    // A6: Escape cancels a pending selection (before it gets dropped somewhere).
    else if (e.key === "Escape" && S.selected) { e.preventDefault(); clearSelection(); }
  });
}

function reportError(err) {
  console.warn("[editor] action failed:", err && err.message ? err.message : err);
  flashStatus("action failed");
}

// ------------------------------------------------------- pointer drag --------
let drag = null;
function wirePointerDrag() {
  const ghost = $("drag-ghost");
  document.addEventListener("pointerdown", (e) => {
    const trayNode = e.target.closest(".tray-photo");
    const cellNode = e.target.closest(".cell[data-asset-id]");
    const reorderNode = e.target.closest(".spread-thumb[data-reorder]");
    if (trayNode) {
      drag = { kind: "photo", assetId: trayNode.dataset.assetId, from: { type: "tray" },
        startX: e.clientX, startY: e.clientY, moved: false };
    } else if (cellNode) {
      drag = { kind: "photo", assetId: cellNode.dataset.assetId,
        from: { type: "cell", spreadIndex: Number(cellNode.dataset.spreadIndex),
          side: cellNode.dataset.side, cellIndex: Number(cellNode.dataset.cellIndex) },
        startX: e.clientX, startY: e.clientY, moved: false };
    } else if (reorderNode) {
      drag = { kind: "spread", from: Number(reorderNode.dataset.spreadIndex),
        startX: e.clientX, startY: e.clientY, moved: false };
    }
    // Capture the pointer to the draggable surface so a trackpad/touch/pen
    // gesture that leaves the element (or the window) still delivers its
    // move/up here — otherwise the browser reclaims it as a scroll and fires
    // pointercancel, and the drop never runs. Best-effort: a synthetic or
    // already-released pointer makes setPointerCapture throw; ignore it.
    if (drag) {
      const captureEl = trayNode || cellNode || reorderNode;
      if (captureEl) {
        drag.captureEl = captureEl;
        drag.pointerId = e.pointerId;
        try { captureEl.setPointerCapture?.(e.pointerId); } catch (_) { /* best-effort */ }
      }
    }
  });
  document.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) < 6) return;
    drag.moved = true;
    if (drag.kind === "photo") {
      ghost.querySelector("img").src = `/api/thumb/${drag.assetId}`;
      ghost.style.display = "block";
      ghost.style.left = e.clientX - 45 + "px";
      ghost.style.top = e.clientY - 45 + "px";
      highlightDrop(e);
    } else {
      highlightReorder(e);
    }
  });
  document.addEventListener("pointerup", (e) => {
    if (!drag) return;
    const d = drag; drag = null;
    releaseCapture(d);
    ghost.style.display = "none";
    clearDropHighlight();
    if (!d.moved) return; // a plain click — handled by click listeners
    if (d.kind === "photo") {
      const at = document.elementFromPoint(e.clientX, e.clientY);
      const cell = at?.closest?.(".cell");
      const dz = at?.closest?.(".page-dropzone");
      const edge = at?.closest?.(".edge-dropzone:not(.disabled)");
      const thumb = at?.closest?.(".spread-thumb");
      if (cell) {
        const target = { spreadIndex: Number(cell.dataset.spreadIndex),
          side: cell.dataset.side, cellIndex: Number(cell.dataset.cellIndex) };
        dropOnto(target, { assetId: d.assetId, from: d.from }).catch(reportError);
      } else if (dz) {
        appendToPage(S.currentSpread, dz.dataset.side, { assetId: d.assetId, from: d.from })
          .catch(reportError);
      } else if (edge) {
        // drop on an edge strip -> move to the adjacent spread
        moveToAdjacentSpread(Number(edge.dataset.dir), { assetId: d.assetId, from: d.from })
          .catch(reportError);
      } else if (thumb) {
        // drop on a grid-map spread thumbnail -> append to that spread's
        // lighter page (any page -> any page, the "gather shots" move)
        const ti = Number(thumb.dataset.spreadIndex);
        const spread = S.book.spreads[ti];
        const side = pageAssetList(spread.left).length <= pageAssetList(spread.right).length
          ? "left" : "right";
        if (pageAssetList(spread[side]).length >= 7) { flashStatus("that spread is full"); return; }
        appendToPage(ti, side, { assetId: d.assetId, from: d.from })
          .then(() => flashStatus(`moved to spread ${ti + 1}`))
          .catch(reportError);
      }
    } else if (d.kind === "spread") {
      const thumb = document.elementFromPoint(e.clientX, e.clientY)?.closest?.(".spread-thumb");
      if (thumb) moveSpread(d.from, Number(thumb.dataset.spreadIndex));
    }
  });
  // A cancelled gesture (the browser reclaimed the pointer as a scroll/pan, the
  // element was removed mid-drag, etc.) must reset exactly like pointerup — minus
  // the drop — so it can never corrupt the NEXT drag.
  document.addEventListener("pointercancel", () => {
    if (!drag) return;
    const d = drag; drag = null;
    releaseCapture(d);
    ghost.style.display = "none";
    clearDropHighlight();
  });
}

// Release a pointer capture, if one was taken. Idempotent + defensive: the
// browser auto-releases on up/cancel, so a double-release is a harmless no-op.
function releaseCapture(d) {
  if (d && d.captureEl && d.pointerId != null) {
    try { d.captureEl.releasePointerCapture?.(d.pointerId); } catch (_) { /* already released */ }
  }
}

function highlightDrop(e) {
  clearDropHighlight();
  const node = document.elementFromPoint(e.clientX, e.clientY);
  const cell = node?.closest?.(".cell");
  const dz = node?.closest?.(".page-dropzone");
  const edge = node?.closest?.(".edge-dropzone:not(.disabled)");
  const thumb = node?.closest?.(".spread-thumb");
  if (cell) cell.classList.add("dragover");
  else if (dz) dz.classList.add("dragover");
  else if (edge) edge.classList.add("dragover");
  else if (thumb) thumb.classList.add("dragover");
}
function highlightReorder(e) {
  clearDropHighlight();
  const thumb = document.elementFromPoint(e.clientX, e.clientY)?.closest?.(".spread-thumb");
  if (thumb) thumb.classList.add("dragover");
}
function clearDropHighlight() {
  document.querySelectorAll(".dragover").forEach((n) => n.classList.remove("dragover"));
}

// ---------------------------------------------------------- test surface -----
window.__pb = {
  get state() { return S; },
  flushSave,
  place: (target, assetId, from) => dropOnto(target, { assetId, from: from || { type: "tray" } }),
  removeFromPage,
  removeFromBook,
  undo, redo, addSpread, deleteSpread, render,
  openLayoutModal, makeBig, applyLayoutCandidate, clearSelection,
  promoteHero, demoteHero, moveToAdjacentSpread, appendToPage,
};

boot().catch((err) => {
  console.error("[editor] boot failed:", err);
  document.body.dataset.ready = "error";
});
