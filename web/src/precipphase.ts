/**
 * The phase the precipitation falls in, as the model itself reports it, so one
 * chart shows rain, snow and what lies between.
 *
 * Pure arithmetic on decoded planes: main.ts feeds it what a run publishes,
 * the categorical type and the percent frozen, and hands the resulting weights
 * to the precipitation's `ForecastLayer` (`setPhase`). Nothing is inferred from
 * a temperature: a run with neither field has no phase to show, and one without
 * a field shows only the classes that field can name.
 *
 * NCEP's four flags name one type a cell and never overlap, so on GFS the mix
 * comes from the percent frozen, and the two classes it cannot name, freezing
 * rain and ice pellets, are the flags'. ECMWF's type names a mixture itself.
 */

import { decodeValue } from "./palettes";
import type { BundleVariable } from "./manifest";
import type { GeoGrid } from "./probe";
import type { TileGeometry, TileRect } from "./tiles";

/** The classes, as the rows of the phase palette (`buildPhasePalette`). Each
 * class but rain is a channel of the weights; rain is what they leave over,
 * so a cell nobody classified paints the ordinary precipitation ramp. */
export const PHASE_RAIN = 0;
export const PHASE_SNOW = 1;
export const PHASE_MIX = 2;
export const PHASE_FREEZING_RAIN = 3;
export const PHASE_ICE_PELLETS = 4;
export const PHASE_ROWS = 5;

/** The percent frozen between which a cell is a mix. Past these the model is
 * all but sure of one or the other: in GFS a wet cell at a percent or two either
 * side of its flag is the interpolation from 0.25° neighbours, not a mix. */
export const PHASE_MIX_BAND: readonly [number, number] = [10, 90];

/** WMO code table 4.201, the values `ptype` and `ptypeec` carry. A wet snow is
 * still the model's snow; freezing drizzle falls under the freezing rain. */
const PTYPE_FREEZING_RAIN = 3;
const PTYPE_SNOW = 5;
const PTYPE_WET_SNOW = 6;
const PTYPE_MIXTURE = 7;
const PTYPE_ICE_PELLETS = 8;
const PTYPE_FREEZING_DRIZZLE = 12;

/** A cell's class from the model's type and its percent frozen, each null where
 * the run does not have it. A cell neither names is on the plain ramp: GFS
 * leaves the rim of a wet area unflagged with nothing frozen, and at f024 of
 * the run this was measured on, 99 % of those cells were above 0 °C. */
export function classifyPhase(type: number | null, frozen: number | null): number {
  if (type === PTYPE_FREEZING_RAIN || type === PTYPE_FREEZING_DRIZZLE) return PHASE_FREEZING_RAIN;
  if (type === PTYPE_ICE_PELLETS) return PHASE_ICE_PELLETS;
  if (type === PTYPE_MIXTURE) return PHASE_MIX;
  if (frozen !== null) {
    if (frozen >= PHASE_MIX_BAND[0] && frozen <= PHASE_MIX_BAND[1]) return PHASE_MIX;
    if (type === null || type === 0) return frozen > PHASE_MIX_BAND[1] ? PHASE_SNOW : PHASE_RAIN;
  }
  return type === PTYPE_SNOW || type === PTYPE_WET_SNOW ? PHASE_SNOW : PHASE_RAIN;
}

/** One decoded plane, with the tiles of it the decoder actually wrote. */
export interface PhasePlane {
  grid: GeoGrid;
  variable: BundleVariable;
  plane: Uint8Array;
  geometry: TileGeometry | null;
  tiles: readonly TileRect[] | null;
}

/** Where the chart writes a word: a zone's name or the freezing line's value. */
export interface PhaseAnchor {
  kind: "freezing-zone" | "frost-line";
  longitude: number;
  latitude: number;
}

/** The weights over the base plane's grid. RGBA, one byte each: snow, mix,
 * freezing rain, ice pellets. Sampled bilinearly, so a class boundary is one
 * cell's blend rather than a staircase. */
export interface PhaseField {
  weights: Uint8Array;
  grid: GeoGrid;
}

const decodeTables = new WeakMap<BundleVariable, Float32Array>();

/** Every code of a variable decoded once: NaN for the reserved codes. */
function decodeTable(variable: BundleVariable): Float32Array {
  let table = decodeTables.get(variable);
  if (!table) {
    table = new Float32Array(256);
    for (let code = 0; code < 256; code += 1) table[code] = decodeValue(variable, code) ?? Number.NaN;
    decodeTables.set(variable, table);
  }
  return table;
}

/** The cells a plane's tiles hold as inclusive ranges; the whole plane when
 * they are not narrowed. */
function cellRects(source: PhasePlane): { firstColumn: number; lastColumn: number; firstRow: number; lastRow: number }[] {
  const { width, height } = source.grid;
  if (!source.tiles || !source.geometry) return [{ firstColumn: 0, lastColumn: width - 1, firstRow: 0, lastRow: height - 1 }];
  const { tileWidth, tileHeight } = source.geometry;
  return source.tiles.map((rect) => ({
    firstColumn: rect.firstColumn * tileWidth,
    lastColumn: Math.min(width - 1, (rect.lastColumn + 1) * tileWidth - 1),
    firstRow: rect.firstRow * tileHeight,
    lastRow: Math.min(height - 1, (rect.lastRow + 1) * tileHeight - 1),
  }));
}

function wrapIndex(index: number, size: number): number {
  return ((index % size) + size) % size;
}

/** For each cell of `from` along an axis, the nearest cell of `to`, or -1. */
function axisMap(
  size: number,
  first: number,
  step: number,
  toSize: number,
  toFirst: number,
  toStep: number,
  wraps: boolean,
): Int32Array {
  const map = new Int32Array(size);
  for (let index = 0; index < size; index += 1) {
    const nearest = Math.round((first + index * step - toFirst) / toStep);
    if (wraps) map[index] = wrapIndex(nearest, toSize);
    else map[index] = nearest >= 0 && nearest < toSize ? nearest : -1;
  }
  return map;
}

/** A plane laid over the base grid: for each base cell its row and column in
 * the plane, and, as one bit per decoded rectangle, which rectangles hold that
 * row and that column. A cell is held when the two share a bit, so the test
 * costs one AND however the view's tiles are cut. */
interface PlaneOverlay {
  plane: Uint8Array;
  width: number;
  rows: Int32Array;
  columns: Int32Array;
  rowBits: Int32Array;
  columnBits: Int32Array;
}

function overlayOn(source: PhasePlane, grid: GeoGrid): PlaneOverlay {
  const rows = axisMap(grid.height, grid.firstLatitude, grid.latitudeStep, source.grid.height, source.grid.firstLatitude, source.grid.latitudeStep, false);
  const columns = axisMap(grid.width, grid.firstLongitude, grid.longitudeStep, source.grid.width, source.grid.firstLongitude, source.grid.longitudeStep, source.grid.wraps);
  const boxes = cellRects(source);
  const bits = (cells: Int32Array, first: (box: (typeof boxes)[number]) => number, last: (box: (typeof boxes)[number]) => number) => {
    const out = new Int32Array(cells.length);
    for (const [at, box] of boxes.entries()) {
      for (let index = 0; index < cells.length; index += 1) {
        const cell = cells[index]!;
        if (cell >= first(box) && cell <= last(box)) out[index]! |= 1 << at;
      }
    }
    return out;
  };
  return {
    plane: source.plane,
    width: source.grid.width,
    rows,
    columns,
    rowBits: bits(rows, (box) => box.firstRow, (box) => box.lastRow),
    columnBits: bits(columns, (box) => box.firstColumn, (box) => box.lastColumn),
  };
}

/** The code standing for "this plane holds nothing here" in the table below. */
const NOT_HELD = 256;
const TABLE_SIDE = NOT_HELD + 1;
/** The memo's key for a run without one of the two fields. */
const NO_VARIABLE = {} as BundleVariable;
const phaseTables = new WeakMap<BundleVariable, WeakMap<BundleVariable, Uint32Array>>();

/** Every pair of codes as the four weight bytes its class writes. Two codebooks
 * of a byte each have 65 k pairs, and a frame has a million cells. */
function phaseTable(type: BundleVariable | null, frozen: BundleVariable | null): Uint32Array {
  const typeKey = type ?? NO_VARIABLE;
  const frozenKey = frozen ?? NO_VARIABLE;
  let byFrozen = phaseTables.get(typeKey);
  if (!byFrozen) {
    byFrozen = new WeakMap();
    phaseTables.set(typeKey, byFrozen);
  }
  const known = byFrozen.get(frozenKey);
  if (known) return known;
  const table = new Uint32Array(TABLE_SIDE * TABLE_SIDE);
  const types = type ? decodeTable(type) : null;
  const frozens = frozen ? decodeTable(frozen) : null;
  const value = (codes: Float32Array | null, code: number) => {
    if (!codes || code === NOT_HELD) return null;
    const decoded = codes[code]!;
    return Number.isNaN(decoded) ? null : decoded;
  };
  // Bytes in memory are channels in order on every little-endian GPU host, the
  // only kind a browser runs on.
  for (let typeCode = 0; typeCode < TABLE_SIDE; typeCode += 1) {
    for (let frozenCode = 0; frozenCode < TABLE_SIDE; frozenCode += 1) {
      const phase = classifyPhase(value(types, typeCode), value(frozens, frozenCode));
      table[typeCode * TABLE_SIDE + frozenCode] = phase === PHASE_RAIN ? 0 : (255 << ((phase - 1) * 8)) >>> 0;
    }
  }
  byFrozen.set(frozenKey, table);
  return table;
}

/** Classify every cell the planes hold, on the type's grid or, without one, the
 * percent frozen's. A cell neither holds keeps zero weights, the plain ramp.
 * `reuse` is a previous field's buffer, taken when it is the right size. This
 * runs on the main thread at every frame step, so the cell loop is table reads
 * and nothing else. */
export function buildPhaseField(type: PhasePlane | null, frozen: PhasePlane | null, reuse?: Uint8Array): PhaseField {
  const base = type ?? frozen;
  if (!base) throw new Error("a phase needs a type or a percent frozen");
  const { grid } = base;
  const size = grid.width * grid.height * 4;
  const weights = reuse?.length === size ? reuse : new Uint8Array(size);
  weights.fill(0);
  const words = new Uint32Array(weights.buffer, weights.byteOffset, grid.width * grid.height);
  const table = phaseTable(type?.variable ?? null, frozen?.variable ?? null);
  const typeOn = type ? overlayOn(type, grid) : null;
  const frozenOn = frozen ? overlayOn(frozen, grid) : null;
  for (const rect of cellRects(base)) {
    for (let row = rect.firstRow; row <= rect.lastRow; row += 1) {
      const start = row * grid.width;
      const typeBits = typeOn ? typeOn.rowBits[row]! : 0;
      const typeStart = typeOn ? typeOn.rows[row]! * typeOn.width : 0;
      const frozenBits = frozenOn ? frozenOn.rowBits[row]! : 0;
      const frozenStart = frozenOn ? frozenOn.rows[row]! * frozenOn.width : 0;
      for (let column = rect.firstColumn; column <= rect.lastColumn; column += 1) {
        const typeCode = typeOn && (typeBits & typeOn.columnBits[column]!) !== 0 ? typeOn.plane[typeStart + typeOn.columns[column]!]! : NOT_HELD;
        const frozenCode = frozenOn && (frozenBits & frozenOn.columnBits[column]!) !== 0 ? frozenOn.plane[frozenStart + frozenOn.columns[column]!]! : NOT_HELD;
        words[start + column] = table[typeCode * TABLE_SIDE + frozenCode]!;
      }
    }
  }
  return { weights, grid };
}

/** The freezing line's plane: the 0 °C isotherm's height above the ground, as
 * codes of this variable, so the contour path can draw the line at 0. The range
 * only has to hold the crossing, and a stray kilometre above or below is as good
 * as any other. */
export const FROST_PLANE_VARIABLE: BundleVariable = {
  numericId: 1,
  id: "frost",
  label: "0 °C isotherm above ground",
  unit: "m",
  quantization: { type: "linear", offset: -1016, scale: 8, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
};
/** Where the whole column is below freezing the model writes a height of 0, on
 * land and sea alike. That is below the ground by definition, and is written as
 * this much below it: over the sea the height and the ground are both 0, and a
 * line at exactly 0 would trace every coast. */
const COLUMN_FROZEN_METRES = -200;

/** Height above sea level minus the ground, as `FROST_PLANE_VARIABLE`'s codes,
 * on the isotherm's grid. A new buffer every time: the layer tells frames apart
 * by the plane's identity. A cell whose ground is not held reads as frozen, and
 * draws no line of its own past the coverage of what is. */
export function buildFrostPlane(height: PhasePlane, ground: PhasePlane): Uint8Array {
  const { grid } = height;
  const out = new Uint8Array(grid.width * grid.height);
  const heights = decodeTable(height.variable);
  const grounds = decodeTable(ground.variable);
  const over = overlayOn(ground, grid);
  const { offset, scale, maximumCode } = FROST_PLANE_VARIABLE.quantization as { offset: number; scale: number; maximumCode: number };
  const encode = (metres: number) => Math.max(0, Math.min(maximumCode, Math.round((metres - offset) / scale)));
  const frozenCode = encode(COLUMN_FROZEN_METRES);
  for (const rect of cellRects(height)) {
    for (let row = rect.firstRow; row <= rect.lastRow; row += 1) {
      const start = row * grid.width;
      const bits = over.rowBits[row]!;
      const groundStart = over.rows[row]! * over.width;
      for (let column = rect.firstColumn; column <= rect.lastColumn; column += 1) {
        const metres = heights[height.plane[start + column]!]!;
        const elevation = (bits & over.columnBits[column]!) !== 0 ? grounds[over.plane[groundStart + over.columns[column]!]!]! : Number.NaN;
        out[start + column] = !(metres > 0) || Number.isNaN(elevation) ? frozenCode : encode(metres - elevation);
      }
    }
  }
  return out;
}

/** A freezing rain zone smaller than this many cells gets no name: a lone flag
 * is the chart's noise, not an area to warn of. The phase is read at the
 * overlay's half resolution, where three cells are already some 150 km across. */
const MIN_ZONE_CELLS = 3;
/** The most zones named, the largest first. */
const MAX_ZONE_LABELS = 12;
/** About how many rows of the view are searched for the freezing line. */
const FROST_LABEL_ROWS = 24;
const MAX_FROST_ANCHORS = 240;

function longitudeOf(grid: GeoGrid, column: number): number {
  return wrapIndex(grid.firstLongitude + column * grid.longitudeStep + 180, 360) - 180;
}

/** The rate past which a flagged cell counts as falling, mm/h. Both models name
 * a type far beyond what reaches the ground: in the IFS run this was measured on,
 * one freezing cell in ten had more than this. */
export const ZONE_MINIMUM_RATE = 0.1;

/** Whether a cell of `grid` has precipitation falling. */
function wetCells(grid: GeoGrid, rate: PhasePlane | null): (cell: number) => boolean {
  if (!rate) return () => false;
  const over = overlayOn(rate, grid);
  const rates = decodeTable(rate.variable);
  return (cell) => {
    const row = Math.floor(cell / grid.width);
    const column = cell - row * grid.width;
    if ((over.rowBits[row]! & over.columnBits[column]!) === 0) return false;
    return rates[over.plane[over.rows[row]! * over.width + over.columns[column]!]!]! > ZONE_MINIMUM_RATE;
  };
}

/** The middle of each named freezing rain zone. Connected over the four
 * neighbours, across the dateline on a wrapping grid. */
function zoneAnchors(field: PhaseField, rate: PhasePlane | null): PhaseAnchor[] {
  const { grid, weights } = field;
  const { width, height } = grid;
  const seen = new Uint8Array(width * height);
  const stack: number[] = [];
  const zones: { cells: number; column: number; row: number; cell: number }[] = [];
  const wet = wetCells(grid, rate);
  const isZone = (cell: number) => weights[cell * 4 + PHASE_FREEZING_RAIN - 1]! > 0 && seen[cell] === 0 && wet(cell);
  for (let start = 0; start < width * height; start += 1) {
    if (!isZone(start)) continue;
    seen[start] = 1;
    stack.push(start);
    let cells = 0;
    let columnSum = 0;
    let rowSum = 0;
    // Offsets from the first cell, so a zone astride the dateline averages
    // to its middle and not to the far side of the world.
    const startColumn = start % width;
    const visit = (next: number) => {
      if (isZone(next)) {
        seen[next] = 1;
        stack.push(next);
      }
    };
    while (stack.length > 0) {
      const cell = stack.pop()!;
      const row = Math.floor(cell / width);
      const column = cell - row * width;
      cells += 1;
      let offset = column - startColumn;
      if (grid.wraps && offset > width / 2) offset -= width;
      if (grid.wraps && offset < -width / 2) offset += width;
      columnSum += offset;
      rowSum += row;
      if (row > 0) visit(cell - width);
      if (row < height - 1) visit(cell + width);
      if (column > 0) visit(cell - 1);
      else if (grid.wraps) visit(cell + width - 1);
      if (column < width - 1) visit(cell + 1);
      else if (grid.wraps) visit(cell - width + 1);
    }
    if (cells >= MIN_ZONE_CELLS) {
      zones.push({ cells, column: startColumn + columnSum / cells, row: rowSum / cells, cell: start });
    }
  }
  return zones
    .sort((a, b) => b.cells - a.cells)
    .slice(0, MAX_ZONE_LABELS)
    .map((zone) => ({
      kind: "freezing-zone",
      longitude: longitudeOf(grid, zone.column),
      latitude: grid.firstLatitude + zone.row * grid.latitudeStep,
    }));
}

/** Points on the 0 °C line, searched along evenly spaced rows of what the
 * plane holds; the map's collision handling thins them to what fits. */
function frostAnchors(temperature: PhasePlane): PhaseAnchor[] {
  const { grid, plane } = temperature;
  const table = decodeTable(temperature.variable);
  const anchors: PhaseAnchor[] = [];
  for (const rect of cellRects(temperature)) {
    const stride = Math.max(1, Math.floor((rect.lastRow - rect.firstRow + 1) / FROST_LABEL_ROWS));
    for (let row = rect.firstRow; row <= rect.lastRow && anchors.length < MAX_FROST_ANCHORS; row += stride) {
      for (let column = rect.firstColumn; column < rect.lastColumn; column += 1) {
        const west = table[plane[row * grid.width + column]!]!;
        const east = table[plane[row * grid.width + column + 1]!]!;
        if (Number.isNaN(west) || Number.isNaN(east) || west > 0 === east > 0) continue;
        anchors.push({
          kind: "frost-line",
          longitude: longitudeOf(grid, column + west / (west - east)),
          latitude: grid.firstLatitude + row * grid.latitudeStep,
        });
      }
    }
  }
  return anchors.slice(0, MAX_FROST_ANCHORS);
}

/** Everything the chart writes for this frame: the zones, where the rate on
 * screen says something is falling in them, and the freezing line, from its own
 * plane. A frame without one has none of the other to name. */
export function phaseAnchors(field: PhaseField | null, rate: PhasePlane | null, frost: PhasePlane | null): PhaseAnchor[] {
  return [...(field ? zoneAnchors(field, rate) : []), ...(frost ? frostAnchors(frost) : [])];
}
