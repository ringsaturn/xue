/**
 * The view: what is on screen, as one object.
 *
 * Three kinds of thing share the screen and the rail. A **field** — one at
 * a time, with its own session and time axis — and the **lines** over it
 * make up the composition (`fill` / `lines`; either may be empty but not
 * both, and a pressure surface named as the field is the lines alone).
 * A fire layer the shell derives (hotspots, fire weather) stands in for
 * the field, drawn on its anchor input's session with that held off screen.
 * The precipitation setup is the rate drawn with what makes it: the
 * lines, the particles run by a flow and two derived layers over it.
 * The **overlays** follow the playhead over the field: the particles, a volume
 * (the cloud water drawn in 3D over a 2D field) and, in the setup, its two
 * derived layers. The **marks** — the storm tracks and the
 * two station products — take no session and have their own pointers.
 * `ViewState` holds all of it; the rail's pressed states, the level row and
 * the address bar are projections of it, and the two functions here are the
 * whole of the URL side: `parseView` reads a query string into a view,
 * `searchForView` writes a view back, and one is the inverse of the other for
 * every parameter the shell understands.
 */

import type { Accumulation } from "./accumulation";
import { VOLUME_BUNDLE_LEVELS, type ForecastBundleId, type ForecastModelId } from "./manifest";
import { isPressureBundle, type PressureBundleId } from "./pressure";
import {
  parseAccumulationFromSearch,
  parseGibsFromSearch,
  parseFlowFromSearch,
  parseLinesFromSearch,
  parseParticlesFromSearch,
  parsePhaseFromSearch,
  parseStationsFromSearch,
  parseRadarFromSearch,
  parseSetupFromSearch,
  parseTcFromSearch,
  parseVariableFromSearch,
  parseVolumeFromSearch,
  searchForCaseVariable,
  searchForVariable,
  searchWithAccumulation,
  searchWithGibs,
  searchWithLines,
  searchWithParticles,
  searchWithPhase,
  searchWithRadar,
  searchWithSetup,
  searchWithStations,
  searchWithTc,
  searchNamesVolume,
  searchWithVolume,
  type DerivedFireState,
  type RadarUrlState,
  SETUP_ANCHOR,
  SETUP_DEFAULT_LINES,
  type StationsUrlState,
  type TcUrlState,
} from "./urlstate";

/** The filled field and the lines over it, as slots rather than as one
 * layer. Every `?type=` of old is a composition with one slot filled; the
 * pressure views are `{fill: null, lines: X}`; `?type=precip&lines=pressure`
 * fills both. The **primary** (the fill's session, or the lines' when
 * there is no fill) drives the timeline, the legend, the data card and the
 * ground tone; the lines follow it by lead time. */
export interface ViewComposition {
  fill: ForecastBundleId | null;
  lines: PressureBundleId | null;
}

/** The derived layers: the precipitation setup's two (meaningful only
 * while it is on, remembered on the device, never in the link) and the fire
 * fields (at most one on, never in the link). */
interface DerivedShown extends DerivedFireState {
  inflow: boolean;
  front: boolean;
}

export interface ViewState {
  /** The filled field; null for the lines-alone view. */
  field: ForecastBundleId | null;
  /** The pressure surface drawn as lines, over the field or alone. */
  lines: PressureBundleId | null;
  /** Whether the field is the precipitation setup (`?type=precipsetup`),
   * drawn on the rate's session: meaningful only while `field` is that. */
  setup: boolean;
  /** What the setup's particles run on (`?flow=`); null for the default,
   * the 850 hPa vapour flux. Kept while the setup is off. */
  flow: ForecastBundleId | null;
  /** What the precipitation field is summed over (`?accum=`,
   * accumulation.ts): a window ending at the playhead or a fixed range of
   * valid times; null for the rate. Kept while another field is on screen,
   * so the way back to precipitation returns to it. */
  accumulation: Accumulation | null;
  /** Whether the particle overlay is drawn where there is a flow to draw. */
  particles: boolean;
  /** Whether the precipitation is painted by phase, with the 0 °C line
   * (`?phase=on`, precipphase.ts). Off unless asked for: it loads more bundles. */
  phase: boolean;
  /** The volume bundle drawn over the field (`?volume=`), or null. Only
   * drawn over a 2D field: a volume chosen as the field is the field. */
  volume: ForecastBundleId | null;
  /** The derived layers drawn over the field. */
  derived: DerivedShown;
  /** The NASA GIBS daily picture drawn under the field (`?gibs=`), a
   * `GIBS_LAYERS` id, or null. */
  imagery: string | null;
  marks: {
    tc: TcUrlState;
    stations: StationsUrlState;
    radar: RadarUrlState;
  };
}

/** What the view is written against: the dataset the page is on, and the
 * session fact the writer needs that the view does not carry — whether the
 * particles were ever chosen (only a chosen off is written, so a
 * reduced-motion visitor who never touched the switch hands out ordinary
 * links). */
interface ViewContext {
  model: ForecastModelId;
  caseId: string | null;
  particlesChosen: boolean;
}

/** What `parseView` falls back to where the query string says nothing. */
export interface ViewDefaults {
  /** The field when `?type=` names none. */
  field: ForecastBundleId;
  /** The lines when neither `?type=` nor `?lines=` is given. */
  lines: PressureBundleId | null;
  /** The particle overlay when `?particles=` says nothing this shell
   * understands: the viewer's stored choice, else the device's default. */
  particles: boolean;
  /** The setup's two derived layers: the viewer's stored choice, else both. */
  setupLayers: { inflow: boolean; front: boolean };
}

interface ParsedView {
  view: ViewState;
  /** Whether `?type=` named a layer — what lets a dataset's own default win
   * over the app-wide one, and keeps a chosen layer across a model switch. */
  fieldRequested: boolean;
  /** Whether `?particles=` made a choice. */
  particlesRequested: boolean;
  /** Whether `?volume=` made a choice (`searchNamesVolume`). */
  volumeRequested: boolean;
}

/** The composition whose primary is `primary`: a pressure surface is the
 * lines alone, anything else is the fill with `lines` kept over it. */
export function compositionForPrimary(primary: ForecastBundleId, lines: PressureBundleId | null): ViewComposition {
  return isPressureBundle(primary) ? { fill: null, lines: primary } : { fill: primary, lines };
}

/** The primary of a composition: the fill, else the lines, else `fallback`
 * for the empty composition that is never on screen. */
export function compositionPrimary(view: ViewComposition, fallback: ForecastBundleId): ForecastBundleId {
  return view.fill ?? view.lines ?? fallback;
}

/** The view a query string names, with `defaults` filling what it does not. */
export function parseView(search: string, defaults: ViewDefaults): ParsedView {
  const setup = parseSetupFromSearch(search);
  const requestedField = setup ? SETUP_ANCHOR : parseVariableFromSearch(search);
  const requestedLines = parseLinesFromSearch(search);
  const requestedParticles = parseParticlesFromSearch(search);
  // A setup link that says nothing of its lines draws the sea level
  // pressure; `lines=none` is the setup with them taken off.
  const setupLines = setup && !new URLSearchParams(search).has("lines") ? SETUP_DEFAULT_LINES : null;
  const composition = compositionForPrimary(
    requestedField ?? defaults.field,
    requestedLines ?? setupLines ?? (requestedField === null ? defaults.lines : null),
  );
  return {
    view: {
      field: composition.fill,
      lines: composition.lines,
      setup,
      flow: parseFlowFromSearch(search),
      accumulation: setup ? null : parseAccumulationFromSearch(search),
      particles: requestedParticles ?? defaults.particles,
      phase: parsePhaseFromSearch(search),
      volume: parseVolumeFromSearch(search),
      derived: { ...defaults.setupLayers, hotspot: false, ffwi: false, fwalert: false },
      imagery: parseGibsFromSearch(search),
      marks: {
        tc: parseTcFromSearch(search),
        stations: parseStationsFromSearch(search),
        radar: parseRadarFromSearch(search),
      },
    },
    fieldRequested: requestedField !== null,
    particlesRequested: requestedParticles !== null,
    volumeRequested: searchNamesVolume(search),
  };
}

/** The volume drawn over the view's field: its `volume`, while the field is
 * a 2D one; null with no field, or with a volume as the field. */
export function volumeOverField(view: Pick<ViewState, "field" | "volume">): ForecastBundleId | null {
  if (view.field === null || VOLUME_BUNDLE_LEVELS.has(view.field)) return null;
  return view.volume;
}

/** The query string carrying a view, with every unrelated parameter in
 * `search` kept as it was. `type` names the primary; `lines` the surface
 * over a filled field and nothing when the lines are the view; the
 * overlays and marks only where they differ from their defaults. */
export function searchForView(view: ViewState, search: string, context: ViewContext, fallback: ForecastBundleId): string {
  const primary = compositionPrimary({ fill: view.field, lines: view.lines }, fallback);
  const base =
    context.caseId !== null
      ? searchForCaseVariable(primary, search, context.caseId)
      : searchForVariable(primary, search, context.model);
  const withLines = searchWithLines(base, view.field !== null ? view.lines : null);
  const setup = setupInView(view);
  const withSetup = searchWithSetup(withLines, setup, view.flow, view.lines);
  const withAccumulation = searchWithAccumulation(withSetup, view.field === "prate" && !setup ? view.accumulation : null);
  const withVolume = searchWithVolume(withAccumulation, volumeOverField(view));
  const withParticles = searchWithParticles(withVolume, view.particles || !context.particlesChosen);
  const withPhase = searchWithPhase(withParticles, view.phase);
  const withGibs = searchWithGibs(withPhase, view.imagery);
  const withStations = searchWithStations(withGibs, view.marks.stations);
  const withRadar = searchWithRadar(withStations, view.marks.radar);
  return searchWithTc(withRadar, view.marks.tc);
}

/** Whether the view's field is the precipitation setup: its switch, on the
 * field it is drawn on. */
export function setupInView(view: Pick<ViewState, "field" | "setup">): boolean {
  return view.setup && view.field === SETUP_ANCHOR;
}
