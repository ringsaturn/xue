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
import { geoGrid } from "./probe";
import type { DecodedFrame, VariableSession } from "./session";
import { DERIVED_FIRE_IDS, type DerivedFireId, FIRE_FIELD_ANCHOR, fireFieldOf } from "./urlstate";
import type { ViewState } from "./viewstate";

/** What the derived layers read off the page: the map, the legend rows
 * and rail tiles they drive, the run and the playhead (live, through
 * getters), and the session functions they open and decode inputs with. */
export interface DerivedDeps {
  readonly experimentEnabled: boolean;
  readonly EXPERIMENT_FLOW_ID: ForecastBundleId;
  readonly EXPERIMENT_WARMTH_ID: ForecastBundleId;
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
  readonly experimentFlowInk: () => readonly [number, number, number, number];
  readonly frameLeadSeconds: (index: number) => number;
  readonly frameValidTime: (index: number) => number;
  readonly cacheKey: (session: VariableSession, variable: BundleVariable, offset: number) => string;
  readonly showError: (message: string) => void;
  readonly modelDomain: () => LambertDomain | null;
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

/** One field the experiment computes: its layer, the grid it was last
 * configured for, and the input planes the plane on screen was built from
 * (their joined cache keys), so a repeat select never recomputes. */
export type DerivedId = "inflow" | "front" | DerivedFireId;

interface DerivedLayer {
  id: DerivedId;
  layer: ForecastLayer;
  gridSource: BundleMetadata | null;
  builtFrom: string | null;
}

/** The experiment's state: the two input sessions, opened beside the
 * composition's own like overlays, the derived layers, and the cache keys
 * they are waiting on or showing. Null unless `?x=true`. */
interface Composite {
  flow: VariableSession | null;
  warmth: VariableSession | null;
  layers: Record<"inflow" | "front", DerivedLayer>;
  /** Input planes not decoded yet for the frame on screen: a decode landing
   * on one of these retries the composite. */
  wantedKeys: Set<string>;
  /** Input planes the derived fields on screen were built from — protected
   * from eviction like a displayed plane. */
  shownKeys: string[];
}

/** The fire layers' state, the experiment's machinery without the
 * experiment: input sessions opened beside the composition while a layer
 * that reads them is on, a derived layer each (added to the map the first
 * time it is drawn), and the cache keys waited on or shown. */
interface FireDerived {
  inputs: Map<ForecastBundleId, VariableSession>;
  layers: Record<DerivedFireId, DerivedLayer>;
  wantedKeys: Set<string>;
  shownKeys: string[];
}

/** The derived-layer controller: the `?x=` experiment's composite (the
 * inflow and the frontal zone) and the fire fields, each computed from
 * input sessions opened beside the composition and drawn on a layer of
 * its own. */
export function createDerived(deps: DerivedDeps) {
  const {
    experimentEnabled,
    EXPERIMENT_FLOW_ID,
    EXPERIMENT_WARMTH_ID,
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
    experimentFlowInk,
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
    syncUrl,
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

  const composite: Composite | null = experimentEnabled
    ? {
        flow: null,
        warmth: null,
        layers: { inflow: makeDerivedLayer("inflow"), front: makeDerivedLayer("front") },
        wantedKeys: new Set(),
        shownKeys: [],
      }
    : null;

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

  /** Whether a session feeds a derived field — the experiment's or a fire
   * layer's. Such a session always decodes whole planes: the fields are
   * computed over the grid, and the particles the flow runs respawn
   * anywhere on it. */
  function isCompositeInput(session: VariableSession): boolean {
    if (composite !== null && (session === composite.flow || session === composite.warmth)) return true;
    return shownFireInputs().has(session);
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
    if (composite?.flow) inputs.add(composite.flow);
    if (composite?.warmth) inputs.add(composite.warmth);
    return [...inputs].filter((session) => !shown.has(session));
  }

  /** The derived key under the colour scale: a row per derived layer the
   * run can feed and the view draws — the frontal zone needs the θe, the
   * inflow both experiment inputs, a fire layer all of its own — and the
   * experiment's rows nothing at all outside it. */
  function syncDerivedLegend(): void {
    const warmth = experimentEnabled && deps.manifest !== null && hasBundle(deps.manifest, EXPERIMENT_WARMTH_ID);
    const flow = warmth && compositeFlowPublished();
    legendInflow.hidden = !(flow && view.derived.inflow);
    legendFront.hidden = !(warmth && view.derived.front);
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

  /** The rail's derived-layer tiles, all under the experiment: its own on a
   * run that ships their inputs, a fire layer's wherever the run ships its
   * inputs; pressed while the layer is drawn. */
  function syncDerivedTiles(run: ForecastManifest): void {
    const warmth = experimentEnabled && hasBundle(run, EXPERIMENT_WARMTH_ID);
    const flow = warmth && hasBundle(run, EXPERIMENT_FLOW_ID);
    for (const button of derivedButtons()) {
      const id = button.dataset.derived as DerivedId;
      if (isFireDerivedId(id)) button.hidden = !fireTileShown(run, id);
      else button.hidden = id === "inflow" ? !flow : !warmth;
    }
    syncRail();
  }

  /** Flip one of the experiment's derived layers: off hides it at once, on
   * shows it from the frame on screen (computing it if its inputs are
   * decoded). */
  function setDerivedShown(id: "inflow" | "front", shown: boolean): void {
    if (view.derived[id] === shown) return;
    view.derived[id] = shown;
    syncRail();
    syncDerivedLegend();
    syncUrl();
    if (!composite) return;
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

  /** Whether the run on screen ships the experiment's flow bundle — the
   * particle switch shows over a scalar fill when it does. */
  function compositeFlowPublished(): boolean {
    return composite !== null && deps.manifest !== null && hasBundle(deps.manifest, EXPERIMENT_FLOW_ID);
  }

  /** Open the experiment's input sessions beside the composition — after the
   * primary, never blocking it, at the overlay tier — and hand each the frame
   * on screen as it lands. A run that does not ship an input leaves that
   * field off: the frontal zone needs the θe alone, the inflow both. */
  function applyComposite(): void {
    if (!composite || !deps.manifest) return;
    const sequence = deps.initializeSequence;
    const inputs: Array<["flow" | "warmth", ForecastBundleId]> = [
      ["flow", EXPERIMENT_FLOW_ID],
      ["warmth", EXPERIMENT_WARMTH_ID],
    ];
    for (const [role, id] of inputs) {
      if (composite[role]?.id === id || !hasBundle(deps.manifest, id)) continue;
      loadVariable(id, sequence, "overlay")
        .then((session) => {
          if (sequence !== deps.initializeSequence || !composite) return;
          composite[role] = session;
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
          if (error instanceof DOMException && error.name === "AbortError") return;
          console.warn(`composite input ${id} unavailable`, error);
        });
    }
  }

  /** Forget the experiment's sessions and blank its layers: a new dataset. */
  function resetComposite(): void {
    if (!composite) return;
    composite.flow = null;
    composite.warmth = null;
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
    // A fire layer joins the map the first time it is drawn, over the fill
    // and under the lines like the experiment's.
    if (!map.getLayer(derived.layer.id)) map.addLayer(derived.layer, slots.lines.layer.id);
    if (derived.gridSource !== source.metadata) {
      derived.layer.configureGrid(source.metadata);
      derived.layer.setDomain(modelDomain());
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

  /** Compute and show the experiment's fields for the primary frame at
   * `index`, and run the particles off the flow, from whatever input planes
   * are decoded: a field whose inputs are not all there yet keeps its last
   * plane up, the way an overlay does, and a field whose input the run does
   * not ship stays off. The fill's own vector, when the fill is one, keeps
   * the particles; the flow only drives them over a scalar. */
  function trySelectComposite(index: number): void {
    if (!composite || !deps.layersAdded) return;
    const lead = frameLeadSeconds(index);
    const flow = composite.flow ? compositeInputPlanes(composite.flow, lead, composite.wantedKeys) : null;
    const warmth = composite.warmth ? compositeInputPlanes(composite.warmth, lead, composite.wantedKeys) : null;
    composite.shownKeys = [...(flow?.keys ?? []), ...(warmth?.keys ?? [])];
    for (const key of composite.shownKeys) composite.wantedKeys.delete(key);
    if (warmth && composite.warmth && view.derived.front) {
      const session = composite.warmth;
      const plane = warmth.frames[0]!.plane;
      showDerived(composite.layers.front, session, warmth.keys.join("|"), () =>
        thermalFrontZone({ grid: geoGrid(session.metadata), variable: session.variable, plane }),
      );
    }
    if (flow && warmth && composite.flow && composite.warmth && view.derived.inflow) {
      const flowSession = composite.flow;
      const warmthSession = composite.warmth;
      const [u, v] = flowSession.variables;
      if (u && v && flow.frames.length >= 2) {
        showDerived(composite.layers.inflow, flowSession, [...flow.keys, ...warmth.keys].join("|"), () =>
          warmMoistInflow(
            { grid: geoGrid(flowSession.metadata), u, v, uPlane: flow.frames[0]!.plane, vPlane: flow.frames[1]!.plane },
            { grid: geoGrid(warmthSession.metadata), variable: warmthSession.variable, plane: warmth.frames[0]!.plane },
          ),
        );
      }
    }
    if (flow && composite.flow && flow.frames.length >= 2 && view.particles && deps.activeSession && !deps.activeSession.vector) {
      ensureWindLayer();
      ensureWindGrid(composite.flow);
      deps.windLayer!.setInk(experimentFlowInk());
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
   * feed, beside the composition like the experiment's — after the primary,
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
  };
}
