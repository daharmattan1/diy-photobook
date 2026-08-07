// Pure client-side helpers over the v2 book model. Mirror photobook/bookmodel.py
// so the editor and the server/exporter agree on what a cell holds.

export function cellAsset(cell) {
  if (cell == null) return null;
  if (typeof cell === "string") return cell || null;
  if (typeof cell === "object") return cell.asset_id || null;
  return null;
}

export function cellCrop(cell) {
  if (cell && typeof cell === "object" && cell.crop) return cell.crop;
  return null;
}

export function cellOverride(cell) {
  if (cell && typeof cell === "object" && cell.dpi_override) return cell.dpi_override;
  return null;
}

export function cellId(cell) {
  if (cell && typeof cell === "object") return cell.id || null;
  return null;
}

// Optional per-cell caption. Tolerant like cellCrop: only a non-empty string
// counts; a bare-string / null cell or an all-whitespace caption reads as none.
export function cellCaption(cell) {
  if (cell && typeof cell === "object") {
    const caption = cell.caption;
    if (typeof caption === "string" && caption.trim()) return caption;
  }
  return null;
}

function uuid() {
  if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  return "id-" + Math.random().toString(16).slice(2) + Date.now().toString(16);
}

export function makeCell(assetId, crop = null, dpiOverride = null, id = null, caption = null) {
  return { id: id || uuid(), asset_id: assetId || null, crop, dpi_override: dpiOverride, caption };
}

// Map asset_id -> [{spreadIndex, spreadId, side, cellIndex}]
export function placementIndex(book) {
  const map = new Map();
  const spreads = book.spreads || [];
  for (let si = 0; si < spreads.length; si += 1) {
    const spread = spreads[si];
    for (const side of ["left", "right"]) {
      const page = spread[side] || {};
      const cells = page.cells || [];
      for (let ci = 0; ci < cells.length; ci += 1) {
        const aid = cellAsset(cells[ci]);
        if (!aid) continue;
        if (!map.has(aid)) map.set(aid, []);
        map.get(aid).push({ spreadIndex: si, spreadId: spread.id, side, cellIndex: ci });
      }
    }
  }
  return map;
}

// The ordered list of asset ids on a page.
export function pageAssetList(page) {
  return (page.cells || []).map(cellAsset);
}

// Template geometry for a page.
export function pageGeometry(book, page) {
  const tpl = (book.templates || {})[page.template];
  return (tpl && tpl.cells) || [];
}

export function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}
