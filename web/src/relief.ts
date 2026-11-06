// The altitude correction over the 3D relief: the controller that opens
// the orography and the isobaric column and feeds the layer. The formula
// is lapse.ts; the shader side is layer.ts.
import type { Map as MaplibreMap } from "maplibre-gl";
import { COLUMN_CAPACITY } from "./lapse";
import { type ColumnWindow, type CorrectionGrid, type ForecastLayer, type LapseCorrection, lapseWeight } from "./layer";
import {
  type BundleVariable,
  type ForecastBundleId,
  type ForecastManifest,
  frameOffsets,
  hasBundle,
  type KnownBundleId,
} from "./manifest";
import type { VariableSession } from "./session";
import { type TileRect, viewportTileRects } from "./tiles";
import { cropGrid, geoGrid, gridMapFor } from "./gridmap";

/** What the relief correction reads off the page: the map, the run and the
 * primary session (live, through getters), and the session functions it
 * opens and decodes its own bundles with. */
export interface ReliefDeps {
  readonly map: MaplibreMap;
  readonly sceneApplied: boolean;
  readonly sessionOffsetForLead: (session: VariableSession, seconds: number) => number | null;
  readonly manifest: ForecastManifest | null;
  readonly slotFor: (id: ForecastBundleId) => { layer: Pick<ForecastLayer, "setLapseCorrection"> };
  readonly activeSession: VariableSession | null;
  readonly activeFrameIndex: number | null;
  readonly initializeSequence: number;
  readonly sessions: Map<string, VariableSession>;
  readonly frameLeadSeconds: (index: number) => number;
  readonly terrainColumnLevels: () => { level: number; temperature: string; height: string; }[];
  readonly requestDecode: (session: VariableSession, variable: BundleVariable, hour: number, tiles?: TileRect[] | null) => void;
  readonly loadVariable: (variableId: ForecastBundleId, sequence: number, role: "probe") => Promise<VariableSession>;
}

/** The relief controller: opens the run's orography and the isobaric
 * column for the view, and hands the primary layer the correction. */
export function createRelief(deps: ReliefDeps) {
  const {
    map,
    sessionOffsetForLead,
    slotFor,
    sessions,
    frameLeadSeconds,
    terrainColumnLevels,
    requestDecode,
    loadVariable,
  } = deps;
  // ---------------------------------------------------------------------------
  // Altitude correction over the 3D relief (layer.ts::lapseCodes): the 2 m
  // temperature moved from the model's smoothed ground to the DEM's by the
  // formula the meteogram's terrain row reads (lapse.ts). Only with the
  // terrain on and a run that publishes its orography; the run's `orog` plane
  // is decoded whole once (a megabyte on GFS) and kept as its own copy, since
  // cached planes are recycled. Above the model ground the formula reads the
  // run's terrain-correction pair for the frame on screen, decoded for the
  // view's tiles alone; until a frame's pair lands, the last one shown stands
  // in (the pair changes slowly from one frame to the next, far less than the
  // standard lapse rate differs from it), and a run without one keeps the
  // standard rate throughout.

  /** The orography session the correction reads, and its plane once decoded. */
  let relief: { session: VariableSession; plane: Uint8Array | null } | null = null;

  /** The fields the correction applies to: a temperature 2 m above the ground. */
  const LAPSE_FIELDS: ReadonlySet<KnownBundleId> = new Set(["tmp2m"]);

  function lapseWanted(session: VariableSession | null): session is VariableSession {
    return (
      session !== null &&
      deps.sceneApplied &&
      map.getTerrain() !== null &&
      session.chartId !== null &&
      LAPSE_FIELDS.has(session.chartId) &&
      session.variable.quantization.type === "linear" &&
      deps.manifest !== null &&
      hasBundle(deps.manifest, "orog")
    );
  }

  function syncLapse(): void {
    const session = deps.activeSession;
    const layer = session ? slotFor(session.id).layer : null;
    if (!lapseWanted(session) || !layer) {
      layer?.setLapseCorrection(null);
      return;
    }
    syncReliefColumn();
    const orography = sessions.get("orog");
    if (relief && relief.session === orography && relief.plane) {
      layer.setLapseCorrection(lapseCorrection(session, relief.session, relief.plane, reliefColumn?.shown ?? null));
      return;
    }
    if (relief && relief.session === orography) return;
    const sequence = deps.initializeSequence;
    void loadVariable("orog", sequence, "probe")
      .then((opened) => {
        if (sequence !== deps.initializeSequence) return;
        relief = { session: opened, plane: null };
        const offset = frameOffsets(opened.metadata.time)[0] ?? 0;
        requestDecode(opened, opened.variable, offset);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        console.warn("relief: orog not opened:", error instanceof Error ? error.message : error);
      });
  }

  /** A whole orography plane arrived for the correction. */
  function acceptReliefPlane(session: VariableSession, plane: Uint8Array): void {
    if (!relief || relief.session !== session) return;
    relief.plane = plane.slice();
    syncLapse();
  }

  let lapseCorrectionCache: { key: string; value: LapseCorrection } | null = null;

  /** A session's grid as the correction's shader reads it. */
  function correctionGrid(session: VariableSession): CorrectionGrid {
    return geoGrid(session.metadata);
  }

  function lapseCorrection(
    temperature: VariableSession,
    orography: VariableSession,
    plane: Uint8Array,
    column: ColumnWindow | null,
  ): LapseCorrection | null {
    const tq = temperature.variable.quantization;
    const oq = orography.variable.quantization;
    if (tq.type !== "linear" || oq.type !== "linear") return null;
    const key = `${temperature.key}:${orography.key}`;
    const cached = lapseCorrectionCache;
    if (cached?.key === key && cached.value.orography === plane && cached.value.column === column) return cached.value;
    const value: LapseCorrection = {
      orography: plane,
      grid: correctionGrid(orography),
      orographyOffset: oq.offset,
      orographyScale: oq.scale,
      temperatureOffset: tq.offset,
      temperatureScale: tq.scale,
      maximumCode: tq.maximumCode,
      column,
    };
    lapseCorrectionCache = { key, value };
    return value;
  }

  /** A window of the grid, in the grid's own cells: its first column (which
   * may lie past either edge of a wrapping grid), its first row and its size. */
  interface ColumnCells {
    column: number;
    row: number;
    width: number;
    height: number;
  }

  /** The column the map's correction reads: its sessions once open, level by
   * level, the frame, window and tiles last asked for, what has landed for
   * it (each plane cut to the window as it arrives), and the window on
   * screen. */
  let reliefColumn: {
    levels: { temperature: VariableSession | null; height: VariableSession | null }[];
    /** `<offset>:<window>:<tiles>` of the request in flight, or of the last. */
    asked: string | null;
    /** When the request in flight was sent, or null: a request the decode
     * queue dropped for a newer one is given up after a while. */
    inflightSince: number | null;
    landed: {
      asked: string;
      /** The frame offset asked of each session. */
      expected: Map<VariableSession, number>;
      window: ColumnCells;
      tiles: TileRect[] | null;
      values: Map<VariableSession, Float32Array>;
    } | null;
    shown: ColumnWindow | null;
  } | null = null;

  /** Open the column's sessions for a run that has one, and ask for the frame
   * on screen. */
  function syncReliefColumn(): void {
    const levels = terrainColumnLevels();
    if (levels.length === 0) {
      reliefColumn = null;
      return;
    }
    const opened = levels.map((level) => ({
      temperature: sessions.get(level.temperature) ?? null,
      height: sessions.get(level.height) ?? null,
    }));
    const same =
      reliefColumn !== null &&
      reliefColumn.levels.length === opened.length &&
      reliefColumn.levels.every((level, index) => level.temperature === opened[index]!.temperature && level.height === opened[index]!.height);
    if (!same) {
      reliefColumn = { levels: opened, asked: null, inflightSince: null, landed: null, shown: null };
    }
    const missing = levels.flatMap((level) => [level.temperature, level.height]).filter((id) => !sessions.has(id));
    if (missing.length > 0) {
      const sequence = deps.initializeSequence;
      for (const id of missing) {
        void loadVariable(id, sequence, "probe")
          .then(() => {
            if (sequence === deps.initializeSequence) syncLapse();
          })
          .catch((error: unknown) => {
            if (error instanceof DOMException && error.name === "AbortError") return;
            console.warn(`relief: ${id} not opened:`, error instanceof Error ? error.message : error);
          });
      }
      return;
    }
    requestColumnFrame();
  }

  /** Every session of the column, once all are open. */
  function reliefColumnSessions(): VariableSession[] | null {
    const column = reliefColumn;
    if (!column) return null;
    const all = column.levels.flatMap((level) => [level.temperature, level.height]);
    return all.every((session): session is VariableSession => session !== null) ? all : null;
  }

  /** Ask for the column at the frame on screen, over the view's tiles — one
   * request at a time, so a playing loop never queues frames it has moved
   * past. */
  function requestColumnFrame(): void {
    const column = reliefColumn;
    const all = reliefColumnSessions();
    if (!column || !all || deps.activeFrameIndex === null) return;
    const reference = all[0]!;
    // Nothing is decoded while the correction is off or faded out.
    if (!lapseWanted(deps.activeSession) || lapseWeight(gridMapFor(correctionGrid(reference)).nominalStepDegrees(), map.getZoom()) <= 0) return;
    const now = performance.now();
    if (column.inflightSince !== null && now - column.inflightSince < COLUMN_REQUEST_PATIENCE_MS) return;
    const offset = sessionOffsetForLead(reference, frameLeadSeconds(deps.activeFrameIndex));
    if (offset === null) return;
    const window = viewColumnCells(reference);
    if (window === null) return;
    const tiles = columnViewTiles(reference);
    const asked = `${offset}:${JSON.stringify(window)}:${tiles === null ? "all" : JSON.stringify(tiles)}`;
    if (asked === column.asked && column.inflightSince === null) return;
    column.asked = asked;
    column.inflightSince = now;
    const expected = new Map<VariableSession, number>();
    for (const session of all) {
      const sessionOffset = sessionOffsetForLead(session, frameLeadSeconds(deps.activeFrameIndex));
      // A surface without this frame leaves the column incomplete; the
      // correction keeps the last one shown.
      if (sessionOffset === null) return;
      expected.set(session, sessionOffset);
    }
    column.landed = { asked, expected, window, tiles, values: new Map() };
    for (const [session, sessionOffset] of expected) requestDecode(session, session.variable, sessionOffset, tiles);
  }

  /** How long a request for the column may stay unanswered before the next
   * frame asks again. */
  const COLUMN_REQUEST_PATIENCE_MS = 3000;

  /** The largest window, in cells either way, the column is built over: past
   * it the view is far too wide for the correction to show (it fades in at
   * a few dozen pixels a cell). */
  const COLUMN_WINDOW_LIMIT = 512;

  /** The cells under the view on a session's grid, one cell wider all round,
   * or null when that is past `COLUMN_WINDOW_LIMIT`. */
  function viewColumnCells(session: VariableSession): ColumnCells | null {
    const grid = correctionGrid(session);
    const bounds = map.getBounds();
    if (grid.projection) {
      const cover = gridMapFor(grid).coverOfBounds({
        west: bounds.getWest(),
        east: bounds.getEast(),
        south: bounds.getSouth(),
        north: bounds.getNorth(),
      });
      if (!cover) return null;
      const width = cover.lastColumn - cover.firstColumn + 1;
      const height = cover.lastRow - cover.firstRow + 1;
      if (width <= 1 || height <= 1 || width > COLUMN_WINDOW_LIMIT || height > COLUMN_WINDOW_LIMIT) return null;
      return { column: cover.firstColumn, row: cover.firstRow, width, height };
    }
    const firstColumn = Math.floor((bounds.getWest() - grid.firstLongitude) / grid.longitudeStep) - 1;
    const lastColumn = Math.ceil((bounds.getEast() - grid.firstLongitude) / grid.longitudeStep) + 1;
    const rowA = (bounds.getNorth() - grid.firstLatitude) / grid.latitudeStep;
    const rowB = (bounds.getSouth() - grid.firstLatitude) / grid.latitudeStep;
    const firstRow = Math.max(0, Math.floor(Math.min(rowA, rowB)) - 1);
    const lastRow = Math.min(grid.height - 1, Math.ceil(Math.max(rowA, rowB)) + 1);
    const width = grid.wraps ? lastColumn - firstColumn + 1 : Math.min(grid.width - 1, lastColumn) - Math.max(0, firstColumn) + 1;
    const height = lastRow - firstRow + 1;
    if (width <= 1 || height <= 1 || width > COLUMN_WINDOW_LIMIT || height > COLUMN_WINDOW_LIMIT) return null;
    return { column: grid.wraps ? firstColumn : Math.max(0, firstColumn), row: firstRow, width, height };
  }

  /** The column's tiles under the view, or null for the whole plane. */
  function columnViewTiles(session: VariableSession): TileRect[] | null {
    if (!session.tiles || !session.streaming) return null;
    const bounds = map.getBounds();
    return viewportTileRects(session.metadata, session.tiles, {
      west: bounds.getWest(),
      east: bounds.getEast(),
      south: bounds.getSouth(),
      north: bounds.getNorth(),
    });
  }

  /** One of the column's planes arrived: cut the window out of it (the
   * cache recycles the plane), and once every one of the frame asked for is
   * in, build the column and show it. */
  function acceptColumnPlane(session: VariableSession, offset: number, plane: Uint8Array): void {
    const column = reliefColumn;
    const landed = column?.landed;
    const all = reliefColumnSessions();
    if (!column || !landed || !all || landed.expected.get(session) !== offset || landed.values.has(session)) return;
    landed.values.set(session, windowValues(session, plane, landed.window, landed.tiles));
    if (landed.values.size < all.length) return;
    column.inflightSince = null;
    column.shown = columnWindow(column, landed);
    // syncLapse shows it, then asks for the frame on screen if that has
    // moved on meanwhile.
    syncLapse();
  }

  /** A plane's values over a window, NaN where the cell was not decoded (a
   * tile outside `tiles`) or is the codebook's nodata. */
  function windowValues(session: VariableSession, plane: Uint8Array, window: ColumnCells, tiles: TileRect[] | null): Float32Array {
    const out = new Float32Array(window.width * window.height).fill(Number.NaN);
    const quantization = session.variable.quantization;
    if (quantization.type !== "linear") return out;
    const grid = correctionGrid(session);
    const geometry = session.tiles;
    const decoded = (column: number, row: number) => {
      if (tiles === null || !geometry) return true;
      const tileColumn = Math.floor(column / geometry.tileWidth);
      const tileRow = Math.floor(row / geometry.tileHeight);
      return tiles.some(
        (rect) => tileColumn >= rect.firstColumn && tileColumn <= rect.lastColumn && tileRow >= rect.firstRow && tileRow <= rect.lastRow,
      );
    };
    for (let j = 0; j < window.height; j += 1) {
      const row = window.row + j;
      for (let i = 0; i < window.width; i += 1) {
        const column = (((window.column + i) % grid.width) + grid.width) % grid.width;
        if (!decoded(column, row)) continue;
        const code = plane[row * grid.width + column]!;
        if (code === quantization.nodataCode || code > quantization.maximumCode) continue;
        out[j * window.width + i] = quantization.offset + code * quantization.scale;
      }
    }
    return out;
  }

  /** The column over the window for the shader: per cell its surfaces sorted
   * by height (lapse.ts::sortedColumn), packed three RGBA texels a cell. */
  function columnWindow(
    column: NonNullable<typeof reliefColumn>,
    landed: NonNullable<NonNullable<typeof reliefColumn>["landed"]>,
  ): ColumnWindow {
    const { window } = landed;
    const cells = window.width * window.height;
    const a = new Float32Array(cells * 4);
    const b = new Float32Array(cells * 4);
    const c = new Float32Array(cells * 4);
    const planes = column.levels.map((level) => ({
      temperature: landed.values.get(level.temperature!)!,
      height: landed.values.get(level.height!)!,
    }));
    const heights = new Array<number>(COLUMN_CAPACITY);
    const temperatures = new Array<number>(COLUMN_CAPACITY);
    for (let cell = 0; cell < cells; cell += 1) {
      let count = 0;
      for (const plane of planes) {
        const z = plane.height[cell]!;
        const t = plane.temperature[cell]!;
        if (!Number.isFinite(z) || !Number.isFinite(t)) continue;
        // Insertion by height: five surfaces at most.
        let at = count;
        while (at > 0 && heights[at - 1]! > z) {
          heights[at] = heights[at - 1]!;
          temperatures[at] = temperatures[at - 1]!;
          at -= 1;
        }
        heights[at] = z;
        temperatures[at] = t;
        count += 1;
      }
      for (let k = count; k < COLUMN_CAPACITY; k += 1) {
        heights[k] = heights[Math.max(0, count - 1)] ?? 0;
        temperatures[k] = temperatures[Math.max(0, count - 1)] ?? 0;
      }
      const o = cell * 4;
      a[o] = heights[0]!; a[o + 1] = heights[1]!; a[o + 2] = heights[2]!; a[o + 3] = heights[3]!;
      b[o] = heights[4]!; b[o + 1] = temperatures[0]!; b[o + 2] = temperatures[1]!; b[o + 3] = temperatures[2]!;
      c[o] = temperatures[3]!; c[o + 1] = temperatures[4]!; c[o + 2] = count < 2 ? 0 : count;
    }
    const grid = correctionGrid(column.levels[0]!.temperature!);
    return {
      a,
      b,
      c,
      grid: cropGrid(grid, window.column, window.row, window.width, window.height),
    };
  }

  return {
    syncLapse,
    requestColumnFrame,
    acceptReliefPlane,
    acceptColumnPlane,
    get relief() {
      return relief;
    },
  };
}
