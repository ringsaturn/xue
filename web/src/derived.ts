// Derived layers: fields computed in the browser from published bundles,
// drawn like any other. The arithmetic is composite.ts, hotspot.ts and
// firewx.ts; this is the controller that opens the inputs, waits for
// their planes and feeds the layers.
import type { Map as MaplibreMap } from "maplibre-gl";
import { DERIVED_MAX_CODE, frontPalette, inflowPalette, thermalFrontZone, warmMoistInflow } from "./composite";
import type { LambertDomain } from "./domain";
import { fireWeatherAlert, fireWeatherAlertPalette, fosbergPalette, fosbergPlane } from "./firewx";
import { hotspotPalette, hotspots } from "./hotspot";
import type { MessageKey } from "./i18n";
import { type ContourStyle, ForecastLayer } from "./layer";
import {
  type BundleMetadata,
  type BundleVariable,
  FORECAST_MODELS,
  type ForecastBundleId,
  type ForecastManifest,
  type ForecastModelId,
  frameOffsets,
  hasBundle,
  type VariantDescriptor,
} from "./manifest";
import type { WindParticleLayer } from "./particles";
import { familyLevels } from "./levels";
import { geoGrid } from "./probe";
import type { DecodedFrame, VariableSession } from "./session";
import {
  DERIVED_FIRE_IDS,
  type DerivedFireId,
  FIRE_FIELD_ANCHOR,
  fireFieldOf,
  SETUP_ANCHOR,
  SETUP_DEFAULT_FLOW,
  SETUP_INPUTS,
} from "./urlstate";
import { setupInView, type ViewState } from "./viewstate";

/** What the derived layers read off the page: the map, the legend rows
 * and rail tiles they drive, the run and the playhead (live, through
 * getters), and the session functions they open and decode inputs with. */
export interface DerivedDeps {
  readonly map: MaplibreMap;
  readonly slider: HTMLInputElement;
  readonly particlesToggle: HTMLButtonElement;
  readonly legendDerived: HTMLElement;
  readonly legendInflow: HTMLElement;
  readonly legendFront: HTMLElement;
  readonly legendFire: Record<"hotspot" | "ffwi" | "fwalert", HTMLElement>;
  readonly derivedButtons: () => HTMLButtonElement[];
  readonly sessionOffsetForLead: (session: VariableSession, seconds: number) => number | null;
  readonly manifest: ForecastManifest | null;
  readonly slots: { readonly lines: { readonly layer: { readonly id: string } } };
  readonly layersAdded: boolean;
  readonly overlaySlots: () => { readonly session: VariableSession | null }[];
  readonly windLayer: WindParticleLayer | null;
  readonly selectedModelId: ForecastModelId;
  readonly activeSession: VariableSession | null;
  readonly activeFrameIndex: number | null;
  readonly requestedFrameIndex: number | null;
  readonly initializeSequence: number;
  readonly planeCache: Map<string, DecodedFrame>;
  readonly view: ViewState;
  readonly flowInk: (flow: ForecastBundleId) => readonly [number, number, number, number];
  readonly frameLeadSeconds: (index: number) => number;
  readonly frameValidTime: (index: number) => number;
  readonly cacheKey: (session: VariableSession, variable: BundleVariable, offset: number) => string;
  readonly showError: (message: string) => void;
  readonly modelDomain: (metadata: BundleMetadata) => LambertDomain | null;
  readonly ensureWindGrid: (session: VariableSession) => void;
  readonly refreshViewportTiles: () => void;
  readonly sendPrefetchWindow: (index: number) => void;
  readonly requestDecode: (session: VariableSession, variable: BundleVariable, hour: number) => void;
  readonly syncRailDensity: () => void;
  readonly syncRail: () => void;
  readonly syncUrl: () => void;
  readonly loadVariable: (
    variableId: ForecastBundleId,
    sequence: number,
    role: "overlay",
    member?: null,
    options?: { tier?: VariantDescriptor | null },
  ) => Promise<VariableSession>;
  readonly fireWind: { readonly borrowed: boolean };
  readonly syncFillSuppressed: () => void;
  readonly ensureWindLayer: () => WindParticleLayer;
}

/** One field the shell computes: its layer, the grid it was last
 * configured for, and the input planes the plane on screen was built from
 * (their joined cache keys), so a repeat select never recomputes. */
export type DerivedId = "inflow" | "front" | DerivedFireId;

interface DerivedLayer {
  id: DerivedId;
  layer: ForecastLayer;
  gridSource: BundleMetadata | null;
  builtFrom: string | null;
}

/** The precipitation setup's state: its input sessions, opened beside the
 * composition's own like overlays and kept for the dataset (the flux and
 * the θe for the derived layers, the flow the particles run on, which may be
 * the flux), the derived layers, and the cache keys they are waiting on or
 * showing. */
interface Composite {
  inputs: Map<ForecastBundleId, VariableSession>;
  /** The flow the particles run on while the setup is up; null otherwise. */
  flow: VariableSession | null;
  layers: Record<"inflow" | "front", DerivedLayer>;
  /** Input planes not decoded yet for the frame on screen: a decode landing
   * on one of these retries the composite. */
  wantedKeys: Set<string>;
  /** Input planes the derived fields on screen were built from — protected
   * from eviction like a displayed plane. */
  shownKeys: string[];
}

/** The fire layers' state, the setup's machinery: input sessions opened beside the composition while a layer
 * that reads them is on, a derived layer each (added to the map the first
 * time it is drawn), and the cache keys waited on or shown. */
interface FireDerived {
  inputs: Map<ForecastBundleId, VariableSession>;
  layers: Record<DerivedFireId, DerivedLayer>;
  wantedKeys: Set<string>;
  shownKeys: string[];
}

/** The derived-layer controller: the precipitation setup's composite (the
 * inflow, the frontal zone and the flow) and the fire fields, each computed from
 * input sessions opened beside the composition and drawn on a layer of
 * its own. */
export function createDerived(deps: DerivedDeps) {
  const {
    map,
    slider,
    particlesToggle,
    legendDerived,
    legendInflow,
    legendFront,
    legendFire,
    derivedButtons,
    sessionOffsetForLead,
    slots,
    overlaySlots,
    planeCache,
    view,
    flowInk,
    frameLeadSeconds,
    frameValidTime,
    cacheKey,
    showError,
    modelDomain,
    ensureWindGrid,
    refreshViewportTiles,
    sendPrefetchWindow,
    requestDecode,
    syncRailDensity,
    syncRail,
    loadVariable,
    fireWind,
    syncFillSuppressed,
    ensureWindLayer,
  } = deps;



  function makeDerivedLayer(id: DerivedLayer["id"]): DerivedLayer {
    return {
      id,
      layer: new ForecastLayer((message) => showError(message), `forecast-derived-${id}`),
      gridSource: null,
      builtFrom: null,
    };
  }

  const composite: Composite = {
    inputs: new Map(),
    flow: null,
    layers: { inflow: makeDerivedLayer("inflow"), front: makeDerivedLayer("front") },
    wantedKeys: new Set(),
    shownKeys: [],
  };
  /** The setup's inputs being opened, so a second reconcile does not open
   * them twice. */
  const setupLoading = new Set<ForecastBundleId>();
  /** The bundles the derived layers read: the vapour flux and the θe. */
  const FLUX_ID: ForecastBundleId = "qflux850";
  const WARMTH_ID: ForecastBundleId = "thetae850";

  /** Whether a run ships everything the setup draws. */
  function setupPublished(run: ForecastManifest | null): boolean {
    return run !== null && hasBundle(run, SETUP_ANCHOR) && SETUP_INPUTS.every((id) => hasBundle(run, id));
  }

  /** Whether the setup is the field on screen: the view's switch on the
   * rate, on a run that ships its inputs. */
  function setupOn(): boolean {
    return setupInView(view) && setupPublished(deps.manifest);
  }

  /** The flows the setup's particles can run on in a run: the vapour flux
   * surfaces, then the wind's, each one it ships. */
  function setupFlows(run: ForecastManifest): ForecastBundleId[] {
    return [...familyLevels("qflux"), ...familyLevels("wind")].filter((id) => hasBundle(run, id));
  }

  /** The flow the setup's particles run on: the view's, where the run ships
   * it, else the default. */
  function setupFlow(): ForecastBundleId {
    const run = deps.manifest;
    const chosen = view.flow;
    return chosen !== null && run !== null && hasBundle(run, chosen) ? chosen : SETUP_DEFAULT_FLOW;
  }

  /** The bundles each fire layer is computed from, the first being the grid
   * it is drawn on (hotspot.ts, firewx.ts). A run that ships them all shows
   * the layer's tile; nothing else is asked of the source. */
  const FIRE_DERIVED_INPUTS: Record<DerivedFireId, readonly ForecastBundleId[]> = {
    hotspot: ["ir039", "ir104"],
    ffwi: ["tmp2m", "dpt2m", "wind10m"],
    fwalert: ["tmp2m", "dpt2m", "gust"],
  };

  /** A fire field's code and title over the map, as its tile writes them. */
  const FIRE_FIELD_CODE: Record<DerivedFireId, string> = { hotspot: "HOTSPOT", ffwi: "FFWI", fwalert: "RH·GUST" };
  const FIRE_FIELD_TITLE: Record<DerivedFireId, MessageKey> = {
    hotspot: "legendHotspot",
    ffwi: "legendFfwi",
    fwalert: "legendFwalert",
  };


  const fireDerived: FireDerived = {
    inputs: new Map(),
    layers: { hotspot: makeDerivedLayer("hotspot"), ffwi: makeDerivedLayer("ffwi"), fwalert: makeDerivedLayer("fwalert") },
    wantedKeys: new Set(),
    shownKeys: [],
  };

  /** Whether a session feeds a derived field — the setup's or a fire
   * layer's. Such a session always decodes whole planes: the fields are
   * computed over the grid, and the particles the flow runs respawn
   * anywhere on it. */
  function isCompositeInput(session: VariableSession): boolean {
    if (shownSetupInputs().has(session)) return true;
    return shownFireInputs().has(session);
  }

  /** The setup's input sessions the view reads: none while it is off. */
  function shownSetupInputs(): Set<VariableSession> {
    const inputs = new Set<VariableSession>();
    if (!setupOn()) return inputs;
    for (const id of [FLUX_ID, WARMTH_ID]) {
      const session = composite.inputs.get(id);
      if (session) inputs.add(session);
    }
    if (composite.flow) inputs.add(composite.flow);
    return inputs;
  }

  /** The fire layers' input sessions a layer on screen reads: one whose
   * layers are all off stays resident but decodes nothing for them. */
  function shownFireInputs(): Set<VariableSession> {
    const inputs = new Set<VariableSession>();
    for (const id of DERIVED_FIRE_IDS) {
      if (!view.derived[id]) continue;
      for (const input of FIRE_DERIVED_INPUTS[id]) {
        const session = fireDerived.inputs.get(input);
        if (session) inputs.add(session);
      }
    }
    return inputs;
  }

  /** The derived fields' input sessions that are not otherwise on screen. */
  function compositeSessions(): VariableSession[] {
    const shown = new Set<VariableSession>();
    if (deps.activeSession) shown.add(deps.activeSession);
    for (const slot of overlaySlots()) shown.add(slot.session!);
    const inputs = shownFireInputs();
    for (const session of shownSetupInputs()) inputs.add(session);
    return [...inputs].filter((session) => !shown.has(session));
  }

  /** The derived key under the colour scale: a row per derived layer the
   * view draws — the setup's while it is up, a fire layer's where the run
   * ships all of its inputs. */
  function syncDerivedLegend(): void {
    const setup = setupOn();
    legendInflow.hidden = !(setup && view.derived.inflow);
    legendFront.hidden = !(setup && view.derived.front);
    for (const id of DERIVED_FIRE_IDS) {
      legendFire[id].hidden = !(deps.manifest !== null && fireInputsPublished(deps.manifest, id) && view.derived[id]);
    }
    legendDerived.hidden = legendInflow.hidden && legendFront.hidden && DERIVED_FIRE_IDS.every((id) => legendFire[id].hidden);
  }

  /** The top soil layer's moisture, read as a land mask by the fire weather
   * layers where the run ships it (firewx.ts): fuel burns on land, and the
   * sea's winds alone would paint the oceans. Only land and water matter, so
   * its first frame stands for every lead. */
  const FIRE_LAND_MASK: ForecastBundleId = "soilw1";

  /** Every input a fire layer reads on a run: its own, and the land mask
   * for the fire weather layers where the run ships one. */
  function fireInputs(run: ForecastManifest, id: DerivedFireId): readonly ForecastBundleId[] {
    const inputs = FIRE_DERIVED_INPUTS[id];
    return id !== "hotspot" && hasBundle(run, FIRE_LAND_MASK) ? [...inputs, FIRE_LAND_MASK] : inputs;
  }

  /** The fire fields the rail carries whatever is on screen; the others are
   * reached through the field sheet and take a tile only while on screen,
   * as a field outside the core does. */
  const FIRE_RAIL_CORE: readonly DerivedFireId[] = ["hotspot"];

  /** Whether a fire field's rail tile shows on a run. */
  function fireTileShown(run: ForecastManifest, id: DerivedFireId): boolean {
    return fireInputsPublished(run, id) && (FIRE_RAIL_CORE.includes(id) || fireField() === id);
  }

  /** Whether a run ships every input of a fire layer. */
  function fireInputsPublished(run: ForecastManifest, id: DerivedFireId): boolean {
    return FIRE_DERIVED_INPUTS[id].every((input) => hasBundle(run, input));
  }

  function isFireDerivedId(id: DerivedId): id is DerivedFireId {
    return (DERIVED_FIRE_IDS as readonly string[]).includes(id);
  }

  /** The rail's derived-layer tiles: the setup's two while it is up, a
   * fire layer's wherever the run ships its inputs; pressed while the layer
   * is drawn. */
  function syncDerivedTiles(run: ForecastManifest): void {
    const setup = setupOn();
    for (const button of derivedButtons()) {
      const id = button.dataset.derived as DerivedId;
      if (isFireDerivedId(id)) button.hidden = !fireTileShown(run, id);
      else button.hidden = !setup;
    }
    syncRail();
  }

  /** Flip one of the setup's derived layers: off hides it at once, on
   * shows it from the frame on screen (computing it if its inputs are
   * decoded). The choice is the device's, not the link's. */
  function setDerivedShown(id: "inflow" | "front", shown: boolean): void {
    if (view.derived[id] === shown) return;
    view.derived[id] = shown;
    try {
      localStorage.setItem(SETUP_LAYERS_KEY, JSON.stringify({ inflow: view.derived.inflow, front: view.derived.front }));
    } catch {
      // Storage can be unavailable (privacy modes); the choice holds for the page.
    }
    syncRail();
    syncDerivedLegend();
    if (!shown) composite.layers[id].layer.setVisible(false);
    else if (deps.activeFrameIndex !== null) trySelectComposite(deps.activeFrameIndex);
  }

  /** The fire field on screen: the one the view has on, while the run ships
   * its inputs and its anchor is the field — null otherwise, and the field
   * is then drawn as itself. */
  function fireField(): DerivedFireId | null {
    const id = fireFieldOf(view.derived);
    if (id === null || deps.manifest === null || !fireInputsPublished(deps.manifest, id)) return null;
    return view.field === FIRE_FIELD_ANCHOR[id] ? id : null;
  }

  /** Put a fire field on (or none), hiding the ones going off. */
  function setFireField(next: DerivedFireId | null): void {
    for (const id of DERIVED_FIRE_IDS) {
      const on = id === next;
      if (view.derived[id] === on) continue;
      view.derived[id] = on;
      if (!on) fireDerived.layers[id].layer.setVisible(false);
    }
  }

  /** Whether the setup is up with a flow to run the particles on — the
   * particle switch shows over its scalar fill when it is. */
  function compositeFlowPublished(): boolean {
    return setupOn();
  }

  /** Reconcile the setup with the view: up, open its input sessions beside
   * the composition — after the primary, never blocking it, at the overlay
   * tier — and hand each the frame on screen as it lands; down, take its
   * layers and its flow off. */
  function applyComposite(): void {
    if (!deps.manifest) return;
    syncDerivedLegend();
    syncDerivedTiles(deps.manifest);
    if (!setupOn()) {
      const ran = composite.flow !== null;
      composite.flow = null;
      composite.shownKeys = [];
      for (const derived of Object.values(composite.layers)) derived.layer.setVisible(false);
      // The particles were the setup's flow over a scalar: they go with it.
      if (ran && deps.activeSession && !deps.activeSession.vector && !fireWind.borrowed) deps.windLayer?.setVisible(false);
      return;
    }
    const sequence = deps.initializeSequence;
    const flowId = setupFlow();
    composite.flow = composite.inputs.get(flowId) ?? null;
    for (const id of new Set([FLUX_ID, WARMTH_ID, flowId])) {
      if (composite.inputs.has(id) || setupLoading.has(id) || !hasBundle(deps.manifest, id)) continue;
      setupLoading.add(id);
      loadVariable(id, sequence, "overlay")
        .then((session) => {
          if (sequence !== deps.initializeSequence) return;
          setupLoading.delete(id);
          composite.inputs.set(id, session);
          if (id === setupFlow()) composite.flow = session;
          refreshViewportTiles();
          const index = deps.activeFrameIndex ?? deps.requestedFrameIndex ?? Number(slider.value);
          sendPrefetchWindow(index);
          if (deps.activeFrameIndex !== null) trySelectComposite(deps.activeFrameIndex);
          else requestCompositeDecode(session, index);
          if (deps.activeSession) {
            particlesToggle.hidden = !deps.activeSession.vector && !compositeFlowPublished() && !fireWind.borrowed;
            syncRailDensity();
          }
        })
        .catch((error: unknown) => {
          if (sequence !== deps.initializeSequence) return;
          setupLoading.delete(id);
          if (error instanceof DOMException && error.name === "AbortError") return;
          console.warn(`setup input ${id} unavailable`, error);
        });
    }
  }

  /** Forget the setup's sessions and blank its layers: a new dataset. */
  function resetComposite(): void {
    composite.inputs.clear();
    setupLoading.clear();
    composite.flow = null;
    composite.wantedKeys.clear();
    composite.shownKeys = [];
    for (const derived of Object.values(composite.layers)) {
      derived.layer.setVisible(false);
      derived.gridSource = null;
      derived.builtFrom = null;
    }
  }

  /** An input session's frame for a lead: the first frame of the fire
   * layers' land mask, or of a static axis, stands for every lead; any other
   * input reads the lead's own. */
  function inputOffsetForLead(session: VariableSession, lead: number): number | null {
    const time = session.metadata.time;
    if (time.frameCount === 1 || session.id === FIRE_LAND_MASK) return frameOffsets(time)[0]!;
    return sessionOffsetForLead(session, lead);
  }

  /** Ask an input session for its whole planes at a primary frame. */
  function requestCompositeDecode(session: VariableSession, index: number): void {
    const offset = inputOffsetForLead(session, frameLeadSeconds(index));
    if (offset === null) return;
    for (const variable of session.variables) {
      const key = cacheKey(session, variable, offset);
      const frame = planeCache.get(key);
      if (!frame || frame.tiles !== null) requestDecode(session, variable, offset);
    }
  }

  /** An input session's whole planes for a lead time, straight out of the
   * cache, or null when any is missing — those are then asked for and noted,
   * so their arrival retries the composite. A lead the session's axis lacks is
   * null too, and nothing is asked. */
  function compositeInputPlanes(
    session: VariableSession,
    lead: number,
    wanted: Set<string>,
  ): { keys: string[]; frames: DecodedFrame[] } | null {
    const offset = inputOffsetForLead(session, lead);
    if (offset === null) return null;
    const keys = session.variables.map((variable) => cacheKey(session, variable, offset));
    const frames: DecodedFrame[] = [];
    let complete = true;
    for (const [position, key] of keys.entries()) {
      const frame = planeCache.get(key);
      if (frame && frame.tiles === null) {
        frames.push(frame);
        continue;
      }
      complete = false;
      wanted.add(key);
      requestDecode(session, session.variables[position]!, offset);
    }
    if (!complete) return null;
    for (const [position, key] of keys.entries()) {
      planeCache.delete(key);
      planeCache.set(key, frames[position]!);
    }
    return { keys, frames };
  }

  /** Put a derived plane on its layer, configured for the grid it was
   * computed on, unless the plane on screen was built from these very
   * inputs already. */
  function showDerived(derived: DerivedLayer, source: VariableSession, builtFrom: string, compute: () => Uint8Array): void {
    if (derived.builtFrom === builtFrom) {
      derived.layer.setVisible(true);
      return;
    }
    // A derived layer joins the map the first time it is drawn, over the
    // fill and under the lines.
    if (!map.getLayer(derived.layer.id)) map.addLayer(derived.layer, slots.lines.layer.id);
    if (derived.gridSource !== source.metadata) {
      derived.layer.configureGrid(source.metadata);
      derived.layer.setDomain(modelDomain(source.metadata));
      derived.layer.setContours(derivedContourStyle(derived.id));
      derived.layer.setVectorField(null);
      derived.layer.setPalette(derivedPalette(derived.id));
      derived.gridSource = source.metadata;
    }
    derived.layer.setFrame(compute());
    derived.layer.setVisible(true);
    derived.builtFrom = builtFrom;
  }

  /** A derived layer draws as an outlined region, not a coat: the palette
   * fill is turned down to a wash so the rain underneath still reads, and
   * the shader's named contours trace the index at two strengths in the
   * layer's own ink — where the inflow and the rain band overlap, both are
   * seen, the way a chart's translucent arrow sits over its shading.
   *
   * The plane's codes are the index itself (0..DERIVED_MAX_CODE), decoded
   * here as 50 + index so the regular contour family, which the shader
   * always draws at multiples of the interval, never lands: an interval of
   * 100 has no multiple between 50 and 51, and the zero contour that would
   * otherwise ring every faint patch is out of range. */
  function derivedContourStyle(id: DerivedId): ContourStyle | null {
    // A hotspot is a cell or two: drawn as its palette, nothing traced.
    if (id === "hotspot") return null;
    if (id === "ffwi" || id === "fwalert") {
      // The palettes carry their own translucency; one line traces FFWI 50
      // (a red-flag day) or the edge of the alert region.
      return {
        offset: DERIVED_DECODE_OFFSET,
        scale: 1 / DERIVED_MAX_CODE,
        interval: 100,
        emphasisInterval: 0,
        values: [DERIVED_DECODE_OFFSET + 0.5],
        lineWidth: 1,
        emphasisWidth: 1,
        lineColor: id === "ffwi" ? [0.62, 0.08, 0.06, 0.95] : [0.88, 0.12, 0.16, 0.95],
        fillAlpha: 1,
        smoothing: 0,
      };
    }
    const inflow = id === "inflow";
    return {
      offset: DERIVED_DECODE_OFFSET,
      scale: 1 / DERIVED_MAX_CODE,
      interval: 100,
      emphasisInterval: 0,
      values: (inflow ? [0.3, 0.65] : [0.4]).map((index) => DERIVED_DECODE_OFFSET + index),
      lineWidth: 0.9,
      emphasisWidth: 0.9,
      lineColor: inflow ? [0.8, 0.27, 0.15, 0.95] : [0.45, 0.17, 0.59, 0.95],
      fillAlpha: inflow ? 0.4 : 0.45,
      smoothing: 0,
    };
  }
  const DERIVED_DECODE_OFFSET = 50;

  function derivedPalette(id: DerivedId): Uint8Array {
    if (id === "inflow") return inflowPalette();
    if (id === "front") return frontPalette();
    if (id === "hotspot") return hotspotPalette();
    if (id === "ffwi") return fosbergPalette();
    return fireWeatherAlertPalette();
  }

  /** Compute and show the setup's fields for the primary frame at `index`,
   * and run the particles off its flow, from whatever input planes are
   * decoded: a field whose inputs are not all there yet keeps its last plane
   * up, the way an overlay does. */
  function trySelectComposite(index: number): void {
    if (!deps.layersAdded || !setupOn()) return;
    const lead = frameLeadSeconds(index);
    const fluxSession = composite.inputs.get(FLUX_ID) ?? null;
    const warmthSession = composite.inputs.get(WARMTH_ID) ?? null;
    const fluxPlanes = fluxSession ? compositeInputPlanes(fluxSession, lead, composite.wantedKeys) : null;
    const warmth = warmthSession ? compositeInputPlanes(warmthSession, lead, composite.wantedKeys) : null;
    const flowSession = composite.flow;
    const flow = flowSession === fluxSession ? fluxPlanes : flowSession ? compositeInputPlanes(flowSession, lead, composite.wantedKeys) : null;
    composite.shownKeys = [...new Set([...(fluxPlanes?.keys ?? []), ...(warmth?.keys ?? []), ...(flow?.keys ?? [])])];
    for (const key of composite.shownKeys) composite.wantedKeys.delete(key);
    if (warmth && warmthSession && view.derived.front) {
      const session = warmthSession;
      const plane = warmth.frames[0]!.plane;
      showDerived(composite.layers.front, session, warmth.keys.join("|"), () =>
        thermalFrontZone({ grid: geoGrid(session.metadata), variable: session.variable, plane }),
      );
    }
    if (fluxPlanes && warmth && fluxSession && warmthSession && view.derived.inflow) {
      const [u, v] = fluxSession.variables;
      if (u && v && fluxPlanes.frames.length >= 2) {
        showDerived(composite.layers.inflow, fluxSession, [...fluxPlanes.keys, ...warmth.keys].join("|"), () =>
          warmMoistInflow(
            { grid: geoGrid(fluxSession.metadata), u, v, uPlane: fluxPlanes.frames[0]!.plane, vPlane: fluxPlanes.frames[1]!.plane },
            { grid: geoGrid(warmthSession.metadata), variable: warmthSession.variable, plane: warmth.frames[0]!.plane },
          ),
        );
      }
    }
    if (flow && flowSession && flow.frames.length >= 2 && view.particles && deps.activeSession && !deps.activeSession.vector) {
      ensureWindLayer();
      ensureWindGrid(flowSession);
      deps.windLayer!.setInk(flowInk(flowSession.id));
      deps.windLayer!.setWindPlanes(flow.frames[0]!.plane, flow.frames[1]!.plane);
      deps.windLayer!.setVisible(true);
    }
  }

  /** The rung a fire layer's input opens at. The hotspot test compares
   * cells with their neighbours, so its two channels read at the rung the
   * field on screen reads at, where the run offers that rung for them (on a
   * satellite source every channel has the same ladder); everything else
   * takes what an overlay would. */
  function fireInputTier(id: ForecastBundleId): VariantDescriptor | null | undefined {
    if (!deps.manifest || (id !== "ir039" && id !== "ir104")) return undefined;
    const reference = deps.activeSession;
    if (!reference || !hasBundle(deps.manifest, reference.id)) return undefined;
    const descriptor = deps.manifest.bundles.find((bundle) => bundle.variable === id);
    if (!descriptor) return undefined;
    const { width, height } = reference.variant ?? geoGrid(reference.metadata);
    // The test runs on the CPU over the whole plane every frame: past this
    // many cells (a full 0.04° disk is 9 M) the overlay's own pick stands.
    if (width * height > HOTSPOT_CELL_BUDGET) return undefined;
    if (reference.variant === null) return null;
    return descriptor.variants?.find((variant) => variant.width === width && variant.height === height);
  }
  const HOTSPOT_CELL_BUDGET = 4_000_000;

  /** Open the inputs of every fire layer the view draws and the run can
   * feed, beside the composition like the setup's — after the primary,
   * never blocking it — and hand each the frame on screen as it lands. */
  function applyFireDerived(): void {
    if (!deps.manifest) return;
    syncFillSuppressed();
    syncDerivedLegend();
    const sequence = deps.initializeSequence;
    const wanted = new Set<ForecastBundleId>();
    for (const id of DERIVED_FIRE_IDS) {
      if (view.derived[id] && fireInputsPublished(deps.manifest, id)) for (const input of fireInputs(deps.manifest, id)) wanted.add(input);
    }
    for (const id of wanted) {
      if (fireDerived.inputs.has(id)) continue;
      const tier = fireInputTier(id);
      loadVariable(id, sequence, "overlay", null, tier === undefined ? {} : { tier })
        .then((session) => {
          if (sequence !== deps.initializeSequence) return;
          fireDerived.inputs.set(id, session);
          refreshViewportTiles();
          const index = deps.activeFrameIndex ?? deps.requestedFrameIndex ?? Number(slider.value);
          sendPrefetchWindow(index);
          if (deps.activeFrameIndex !== null) trySelectFireDerived(deps.activeFrameIndex);
          else requestCompositeDecode(session, index);
        })
        .catch((error: unknown) => {
          if (sequence !== deps.initializeSequence) return;
          if (error instanceof DOMException && error.name === "AbortError") return;
          console.warn(`fire layer input ${id} unavailable`, error);
        });
    }
    if (deps.activeFrameIndex !== null) trySelectFireDerived(deps.activeFrameIndex);
  }

  /** Forget the fire layers' sessions and blank their layers: a new dataset. */
  function resetFireDerived(): void {
    fireDerived.inputs.clear();
    fireDerived.wantedKeys.clear();
    fireDerived.shownKeys = [];
    for (const derived of Object.values(fireDerived.layers)) {
      derived.layer.setVisible(false);
      derived.gridSource = null;
      derived.builtFrom = null;
    }
  }

  /** Compute and show the fire layers for the primary frame at `index` from
   * whatever input planes are decoded: a layer whose inputs are not all
   * there yet keeps its last plane up, the way an overlay does. */
  function trySelectFireDerived(index: number): void {
    const run = deps.manifest;
    if (!deps.layersAdded || !run || fireDerived.inputs.size === 0) return;
    const lead = frameLeadSeconds(index);
    const planes = new Map<ForecastBundleId, { session: VariableSession; keys: string[]; frames: DecodedFrame[] }>();
    const shownKeys: string[] = [];
    for (const id of DERIVED_FIRE_IDS) {
      if (!view.derived[id]) continue;
      for (const input of fireInputs(run, id)) {
        if (planes.has(input)) continue;
        const session = fireDerived.inputs.get(input);
        const read = session ? compositeInputPlanes(session, lead, fireDerived.wantedKeys) : null;
        if (session && read) {
          planes.set(input, { session, ...read });
          shownKeys.push(...read.keys);
        }
      }
    }
    fireDerived.shownKeys = shownKeys;
    for (const key of shownKeys) fireDerived.wantedKeys.delete(key);
    const scalar = (input: ForecastBundleId) => {
      const read = planes.get(input);
      return read ? { grid: geoGrid(read.session.metadata), variable: read.session.variable, plane: read.frames[0]!.plane } : null;
    };
    const builtFrom = (ids: readonly ForecastBundleId[]) => ids.flatMap((input) => planes.get(input)!.keys).join("|");
    const ready = (id: DerivedFireId) => view.derived[id] && fireInputs(run, id).every((input) => planes.has(input));
    const land = scalar(FIRE_LAND_MASK);

    if (ready("hotspot")) {
      const shortwave = scalar("ir039")!;
      const longwave = scalar("ir104")!;
      const validTime = frameValidTime(index);
      showDerived(fireDerived.layers.hotspot, planes.get("ir039")!.session, builtFrom(FIRE_DERIVED_INPUTS.hotspot), () =>
        hotspots(shortwave, longwave, validTime, FORECAST_MODELS[deps.selectedModelId].subLongitude ?? null),
      );
    }
    if (ready("ffwi")) {
      const wind = planes.get("wind10m")!;
      const [u, v] = wind.session.variables;
      if (u && v && wind.frames.length >= 2) {
        const temperature = scalar("tmp2m")!;
        const dewPoint = scalar("dpt2m")!;
        showDerived(fireDerived.layers.ffwi, planes.get("tmp2m")!.session, builtFrom(fireInputs(run, "ffwi")), () =>
          fosbergPlane(
            temperature,
            dewPoint,
            {
              grid: geoGrid(wind.session.metadata),
              u,
              v,
              uPlane: wind.frames[0]!.plane,
              vPlane: wind.frames[1]!.plane,
            },
            land,
          ),
        );
      }
    }
    if (ready("fwalert")) {
      const temperature = scalar("tmp2m")!;
      const dewPoint = scalar("dpt2m")!;
      const gust = scalar("gust")!;
      showDerived(fireDerived.layers.fwalert, planes.get("tmp2m")!.session, builtFrom(fireInputs(run, "fwalert")), () =>
        fireWeatherAlert(temperature, dewPoint, gust, land),
      );
    }
  }

  return {
    fireField,
    compositeSessions,
    composite,
    isCompositeInput,
    trySelectComposite,
    trySelectFireDerived,
    requestCompositeDecode,
    fireDerived,
    isFireDerivedId,
    fireTileShown,
    fireInputsPublished,
    FIRE_RAIL_CORE,
    syncDerivedLegend,
    FIRE_FIELD_TITLE,
    FIRE_FIELD_CODE,
    compositeFlowPublished,
    applyComposite,
    applyFireDerived,
    setFireField,
    resetComposite,
    resetFireDerived,
    syncDerivedTiles,
    setDerivedShown,
    setupOn,
    setupPublished,
    setupFlows,
    setupFlow,
  };
}

/** Where the setup's two derived layers are remembered on the device. */
const SETUP_LAYERS_KEY = "xue-setup-layers";

/** The setup's derived layers as this device last left them: both on
 * unless the viewer took one off. */
export function storedSetupLayers(): { inflow: boolean; front: boolean } {
  try {
    const stored = JSON.parse(localStorage.getItem(SETUP_LAYERS_KEY) ?? "null") as unknown;
    if (stored && typeof stored === "object") {
      const { inflow, front } = stored as { inflow?: unknown; front?: unknown };
      return { inflow: inflow !== false, front: front !== false };
    }
  } catch {
    // Storage can be unavailable (privacy modes), or hold something else.
  }
  return { inflow: true, front: true };
}
