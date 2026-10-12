/**
 * The view: what is on screen, as one object.
 *
 * Three kinds of thing share the screen and the rail. A **field** — one at
 * a time, with its own session and time axis — and the **lines** over it
 * make up the composition (`fill` / `lines`; either may be empty but not
 * both, and a pressure surface named as the field is the lines alone).
 * The **overlays** follow the playhead over the field: the particles, a volume
 * (the cloud water drawn in 3D over a 2D field), the fire layers the shell
 * derives (hotspots, fire weather) and, under the experiment, its two
 * derived layers. The **marks** — the storm tracks and the
 * two station products — take no session and have their own pointers.
 * `ViewState` holds all of it; the rail's pressed states, the level row and
 * the address bar are projections of it, and the two functions here are the
 * whole of the URL side: `parseView` reads a query string into a view,
 * `searchForView` writes a view back, and one is the inverse of the other for
 * every parameter the shell understands.
 */

import { VOLUME_BUNDLE_LEVELS, type ForecastBundleId, type ForecastModelId } from "./manifest";
import { isPressureBundle, type PressureBundleId } from "./pressure";
import {
  parseDerivedFromSearch,
  parseGibsFromSearch,
  parseExperimentFromSearch,
  parseLinesFromSearch,
  parseParticlesFromSearch,
  parseStationsFromSearch,
  parseRadarFromSearch,
  parseTcFromSearch,
  parseVariableFromSearch,
  parseVolumeFromSearch,
  searchForCaseVariable,
  searchForVariable,
  searchWithDerived,
  searchWithGibs,
  searchWithExperiment,
  searchWithLines,
  searchWithParticles,
  searchWithRadar,
  searchWithStations,
  searchWithTc,
  searchNamesVolume,
  searchWithVolume,
  type DerivedFireState,
  type RadarUrlState,
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

/** The derived layers: the experiment's two (`?x=`, meaningful only while
 * it is on) and the fire layers (`?derived=`), which need no experiment. */
interface DerivedShown extends DerivedFireState {
  inflow: boolean;
  front: boolean;
}

export interface ViewState {
  /** The filled field; null for the lines-alone view. */
  field: ForecastBundleId | null;
  /** The pressure surface drawn as lines, over the field or alone. */
  lines: PressureBundleId | null;
  /** Whether the particle overlay is drawn where there is a flow to draw. */
  particles: boolean;
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
 * two session facts the writer needs that the view does not carry — the
 * experiment's switch (a page-load choice) and whether the particles were
 * ever chosen (only a chosen off is written, so a reduced-motion visitor
 * who never touched the switch hands out ordinary links). */
interface ViewContext {
  model: ForecastModelId;
  caseId: string | null;
  experimentEnabled: boolean;
  particlesChosen: boolean;
}

/** What `parseView` falls back to where the query string says nothing. */
export interface ViewDefaults {
  /** The field when `?type=` names none. */
  field: ForecastBundleId;
  /** The lines when neither `?type=` nor `?lines=` is given (the
   * experiment opens with the sea level pressure over its fill). */
  lines: PressureBundleId | null;
  /** The particle overlay when `?particles=` says nothing this shell
   * understands: the viewer's stored choice, else the device's default. */
  particles: boolean;
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
  const requestedField = parseVariableFromSearch(search);
  const requestedLines = parseLinesFromSearch(search);
  const requestedParticles = parseParticlesFromSearch(search);
  const experiment = parseExperimentFromSearch(search);
  const composition = compositionForPrimary(
    requestedField ?? defaults.field,
    requestedLines ?? (requestedField === null ? defaults.lines : null),
  );
  return {
    view: {
      field: composition.fill,
      lines: composition.lines,
      particles: requestedParticles ?? defaults.particles,
      volume: parseVolumeFromSearch(search),
      // The fire layers are not offered yet: they stay behind the experiment
      // until they are tested.
      derived: {
        inflow: experiment.inflow,
        front: experiment.front,
        ...(experiment.enabled ? parseDerivedFromSearch(search) : { hotspot: false, ffwi: false, fwalert: false }),
      },
      // The GIBS underlay is not offered yet either: experiment only.
      imagery: experiment.enabled ? parseGibsFromSearch(search) : null,
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
  const withVolume = searchWithVolume(withLines, volumeOverField(view));
  const withParticles = searchWithParticles(withVolume, view.particles || !context.particlesChosen);
  const withExperiment = searchWithExperiment(withParticles, {
    enabled: context.experimentEnabled,
    inflow: view.derived.inflow,
    front: view.derived.front,
  });
  const withDerived = searchWithDerived(withExperiment, view.derived);
  const withGibs = searchWithGibs(withDerived, view.imagery);
  const withStations = searchWithStations(withGibs, view.marks.stations);
  const withRadar = searchWithRadar(withStations, view.marks.radar);
  return searchWithTc(withRadar, view.marks.tc);
}
