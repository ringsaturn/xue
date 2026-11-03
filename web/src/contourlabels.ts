import type { Feature, FeatureCollection } from "geojson";
import { GeoJSONSource, type Map as MaplibreMap } from "maplibre-gl";
import { currentBasemapTheme } from "./basemap";
import type { LambertDomain } from "./domain";
import { t } from "./i18n";
import type { VariableIdentity } from "./identity";
import { type CellWindow, type LabelRequest, strideFor } from "./isolines";
import type { LabelsWorkerRequest, LabelsWorkerResponse } from "./labels.worker";
import type { ContourStyle } from "./layer";
import type { BundleVariable } from "./manifest";
import { firstSymbolLayerId } from "./marks";
import { pressureLevelForIdentity } from "./pressure";
import { geoGrid, wrap } from "./probe";
import type { DecodedFrame, VariableSession } from "./session";
import type { CoverageBox, TileRect } from "./tiles";

/** What the contour labels read off the page: the map, the lines slot and
 * the primary session (live, through getters), and the frame cache. */
export interface ContourLabelsDeps {
  readonly map: MaplibreMap;
  readonly slots: { readonly lines: { readonly session: VariableSession | null; readonly shownKey: string | null } };
  readonly layersAdded: boolean;
  readonly activeSession: VariableSession | null;
  readonly playing: boolean;
  readonly modelDomain: () => LambertDomain | null;
  readonly sessionCoverage: (session: VariableSession, tiles: TileRect[] | null) => CoverageBox;
  readonly cachedFrame: (key: string, session: VariableSession) => DecodedFrame | undefined;
  readonly contourStyleFor: (variable: BundleVariable, identity?: VariableIdentity | null, overlay?: boolean, longitudeStep?: number) => ContourStyle | null;
  readonly mapStyleReady: boolean;
}

/** The contour labels' controller: the worker that traces them, the two
 * symbol layers they are placed on, and when a frame is traced again. */
export function createContourLabels(deps: ContourLabelsDeps) {
  const {
    map,
    slots,
    modelDomain,
    sessionCoverage,
    cachedFrame,
    contourStyleFor,
  } = deps;
  // -- contour labels ----------------------------------------------------------
  //
  // Values written on the isobars, and an H or an L on every closed high and
  // low. The lines are the GPU's; the labels need geometry, which
  // `isolines.ts` traces from the displayed plane in its own worker, and
  // MapLibre's symbol layers place — along the line for a value, at a point
  // for a center — with the same collision handling the basemap's names get.
  // A halo in the ground's own tone opens a gap in the line under each value,
  // which is how a chart has always made room for a label.

  const LABEL_SOURCE = "pressure-labels";
  const CONTOUR_LABEL_LAYER = "pressure-contour-labels";
  const CENTER_LABEL_LAYER = "pressure-center-labels";
  /** During playback the labels are recomputed at most this often: a line's
   * label sits at a fixed distance along it, so re-tracing every frame would
   * make every label crawl. Stopping, stepping and panning refresh at once. */
  const LABELS_PLAYBACK_INTERVAL_MS = 1000;
  /** How far beyond the viewport the trace extends, as a fraction of it, so a
   * small pan shows labels before the next trace lands. */
  const LABELS_VIEW_MARGIN = 0.2;
  /** A high or a low has to be the extreme within this radius, and stand out
   * from that window's rim by a whole contour interval — one closed line. */
  const CENTER_RADIUS_DEGREES = 8;

  let labelsWorker: Worker | null = null;
  let labelsRequestId = 0;
  let labelsInFlight = false;
  /** A frame asked for while one is being traced; sent when that one lands. */
  let labelsPending: DecodedFrame | null = null;
  let labelsLastPlane: DecodedFrame | null = null;
  let labelsLastAt = 0;
  let labelsShown = false;

  function ensureLabelsWorker(): Worker {
    if (!labelsWorker) {
      labelsWorker = new Worker(new URL("./labels.worker.ts", import.meta.url), { type: "module" });
      labelsWorker.onmessage = (event: MessageEvent<LabelsWorkerResponse>) => handleLabels(event.data);
    }
    return labelsWorker;
  }

  /** The two symbol layers over one GeoJSON source, added under the basemap's
   * own names so a city keeps its label over a contour's. */
  function ensureLabelLayers(): GeoJSONSource | null {
    if (!deps.mapStyleReady) return null;
    const existing = map.getSource(LABEL_SOURCE);
    if (existing) return existing as GeoJSONSource;
    map.addSource(LABEL_SOURCE, { type: "geojson", data: emptyLabels(), buffer: 128 });
    const before = firstSymbolLayerId(map);
    map.addLayer(
      {
        id: CONTOUR_LABEL_LAYER,
        type: "symbol",
        source: LABEL_SOURCE,
        filter: ["==", ["geometry-type"], "LineString"],
        layout: {
          "symbol-placement": "line",
          "symbol-spacing": 420,
          "text-field": ["get", "label"],
          "text-font": ["Noto Sans Regular"],
          "text-size": 11,
          "text-letter-spacing": 0.04,
          "text-max-angle": 25,
          "text-padding": 4,
          "text-rotation-alignment": "map",
          "text-pitch-alignment": "viewport",
        },
        paint: { "text-halo-width": 2.5, "text-halo-blur": 0.5 },
      },
      before,
    );
    map.addLayer(
      {
        id: CENTER_LABEL_LAYER,
        type: "symbol",
        source: LABEL_SOURCE,
        filter: ["==", ["geometry-type"], "Point"],
        layout: {
          "text-field": [
            "format",
            ["get", "letter"],
            { "font-scale": 1.7, "text-font": ["literal", ["Noto Sans Medium"]] },
            "\n",
            {},
            ["get", "label"],
            { "font-scale": 0.9 },
          ],
          "text-font": ["Noto Sans Regular"],
          "text-size": 12,
          "text-line-height": 1.05,
          "text-padding": 6,
        },
        paint: { "text-halo-width": 2 },
      },
      before,
    );
    applyLabelInk();
    return map.getSource(LABEL_SOURCE) as GeoJSONSource;
  }

  /** Ink for the labels: the ground's own ink, and a halo of the ground's
   * ocean tone rather than the basemap's paper, so it reads as a gap in the
   * line rather than a sticker on it. Over a filled field there is no one
   * tone to open a gap in — the halo would be a box of ocean on a warm
   * temperature field — so the lines slot as an overlay takes a soft shadow
   * in the ink's opposite instead, the way the basemap's own names do. */
  function applyLabelInk(): void {
    if (!map.getLayer(CONTOUR_LABEL_LAYER)) return;
    const theme = currentBasemapTheme();
    const darkGround = document.body.dataset.ground === "dark";
    const overlay = slots.lines.session !== null && slots.lines.session !== deps.activeSession;
    const ink = darkGround ? "#eef1f4" : "#2a2824";
    const halo = overlay ? (darkGround ? "rgba(0, 0, 0, 0.6)" : "rgba(255, 255, 255, 0.8)") : theme.ocean;
    for (const id of [CONTOUR_LABEL_LAYER, CENTER_LABEL_LAYER]) {
      map.setPaintProperty(id, "text-color", ink);
      map.setPaintProperty(id, "text-halo-color", halo);
      map.setPaintProperty(id, "text-halo-width", overlay ? 1.5 : id === CONTOUR_LABEL_LAYER ? 2.5 : 2);
      map.setPaintProperty(id, "text-halo-blur", overlay ? 1 : 0.5);
    }
  }

  function emptyLabels(): FeatureCollection {
    return { type: "FeatureCollection", features: [] };
  }

  function clearLabels(): void {
    labelsPending = null;
    labelsLastPlane = null;
    // Anything still being traced is for a session that is gone.
    labelsRequestId += 1;
    labelsInFlight = false;
    if (labelsShown) {
      (map.getSource(LABEL_SOURCE) as GeoJSONSource | undefined)?.setData(emptyLabels());
      labelsShown = false;
    }
  }

  /** Trace the labels for the plane on screen: right away when the viewer is
   * stepping or panning, throttled during playback. */
  function scheduleLabels(frame: DecodedFrame, force: boolean): void {
    const session = slots.lines.session;
    if (!session || !deps.layersAdded || !contourStyleFor(session.variable, session.identity)) return;
    const now = performance.now();
    if (!force && deps.playing && now - labelsLastAt < LABELS_PLAYBACK_INTERVAL_MS) {
      // Remember the newest frame so the next allowed trace is not stale.
      labelsPending = frame;
      return;
    }
    if (labelsInFlight) {
      labelsPending = frame;
      return;
    }
    sendLabels(frame);
  }

  /** The frame on screen, traced again — after a pan, a zoom, or a stop. */
  function refreshLabels(): void {
    const slot = slots.lines;
    const session = slot.session;
    if (!session || slot.shownKey === null || !contourStyleFor(session.variable, session.identity)) return;
    const frame = cachedFrame(slot.shownKey, session);
    if (frame) scheduleLabels(frame, true);
  }

  /** The cells the view covers, widened by the margin, in the grid's own
   * terms: columns may run past the width on a wrapping grid. */
  function viewportCellWindow(grid: ReturnType<typeof geoGrid>): CellWindow | null {
    const bounds = map.getBounds();
    const west = bounds.getWest();
    const east = bounds.getEast();
    const north = bounds.getNorth();
    const south = bounds.getSouth();
    const spanLongitude = Math.min(360, (east - west) * (1 + 2 * LABELS_VIEW_MARGIN));
    const spanLatitude = (north - south) * (1 + 2 * LABELS_VIEW_MARGIN);
    const centerLongitude = (west + east) / 2;
    const centerLatitude = (north + south) / 2;
    const columnSpan = Math.ceil(spanLongitude / grid.longitudeStep) + 1;
    const rowSpan = Math.ceil(spanLatitude / Math.abs(grid.latitudeStep)) + 1;
    // Degrees east of the grid origin, signed: a view west of a regional
    // window is at a negative cell, not most of the way around the world, and
    // a window past the antimeridian (a satellite disk to 200.7) takes a view
    // spelled at -170 as one at 190 (`tiles.ts` does the same).
    const offset = wrap(centerLongitude - spanLongitude / 2 - grid.firstLongitude + 180, 360) - 180;
    let column0 = Math.floor(offset / grid.longitudeStep);
    let columns = columnSpan;
    if (grid.wraps) {
      column0 = wrap(column0, grid.width);
      columns = Math.min(columns, grid.width);
    } else {
      const end = Math.min(grid.width, column0 + columns);
      column0 = Math.max(0, column0);
      columns = end - column0;
    }
    // Rows count down from the first (northern) latitude.
    const rowStart = Math.floor((centerLatitude + spanLatitude / 2 - grid.firstLatitude) / grid.latitudeStep);
    const row0 = Math.max(0, rowStart);
    const rows = Math.min(grid.height, rowStart + rowSpan) - row0;
    if (columns <= 0 || rows <= 0) return null;
    return { column0, row0, columns, rows };
  }

  function sendLabels(frame: DecodedFrame): void {
    const session = slots.lines.session;
    if (!session) return;
    const grid = geoGrid(session.metadata);
    // The labels are traced from the same smoothed field the shader draws,
    // so they read the width off the session's own grid too.
    const style = contourStyleFor(session.variable, session.identity, false, grid.longitudeStep);
    const level = pressureLevelForIdentity(session.identity);
    if (!style || !level || !ensureLabelLayers()) return;
    const window = viewportCellWindow(grid);
    if (!window) return;
    const request: LabelRequest = {
      grid,
      window,
      coverage: sessionCoverage(session, frame.tiles),
      domain: modelDomain() ?? undefined,
      stride: strideFor(window.columns * window.rows),
      smoothingCells: style.smoothing,
      offset: style.offset,
      scale: style.scale,
      interval: level.contourInterval,
      centerRadiusDegrees: CENTER_RADIUS_DEGREES,
      prominence: level.contourInterval,
    };
    labelsRequestId += 1;
    labelsInFlight = true;
    labelsLastPlane = frame;
    labelsLastAt = performance.now();
    // The cache keeps the plane; the worker gets its own copy to read.
    const copy = frame.plane.slice();
    const message: LabelsWorkerRequest = { requestId: labelsRequestId, buffer: copy.buffer, request };
    ensureLabelsWorker().postMessage(message, [copy.buffer]);
  }

  function handleLabels(response: LabelsWorkerResponse): void {
    if (response.requestId !== labelsRequestId) return;
    labelsInFlight = false;
    const source = ensureLabelLayers();
    if (source) {
      const features: Feature[] = [];
      for (const line of response.result.lines) {
        features.push({
          type: "Feature",
          properties: { label: String(line.value) },
          geometry: { type: "LineString", coordinates: line.coordinates },
        });
      }
      for (const center of response.result.centers) {
        features.push({
          type: "Feature",
          properties: {
            letter: t(center.kind === "high" ? "centerHigh" : "centerLow"),
            label: String(center.value),
          },
          geometry: { type: "Point", coordinates: [center.longitude, center.latitude] },
        });
      }
      source.setData({ type: "FeatureCollection", features });
      labelsShown = true;
    }
    // A frame that arrived during the trace, unless it is the one just done.
    const pending = labelsPending;
    labelsPending = null;
    if (pending && pending !== labelsLastPlane) scheduleLabels(pending, !deps.playing);
  }

  return {
    applyLabelInk,
    refreshLabels,
    scheduleLabels,
    clearLabels,
  };
}
