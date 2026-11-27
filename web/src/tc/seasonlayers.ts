/**
 * A season's tracks on the storm map (`typhoon.ts`): every storm of the
 * archive's season overview (`docs/tc.md` §8.3) drawn at once, by
 * Saffir–Simpson class, the strongest on top. It is the page's view of a
 * season; a storm picked from it is drawn by `StormLayers` above it, and
 * the season stays behind it, faded, as context.
 */

import type { FilterSpecification, GeoJSONSource, LineLayerSpecification, Map as MaplibreMap } from "maplibre-gl";

import type { SeasonOverview } from "./archive";
import { SSHS_CLASSES } from "./intensity";

const SOURCE = "season-tracks";
const HALO = "season-tracks-halo";
/** The layer a pointer finds a track on. */
export const SEASON_TRACK_LAYER = "season-tracks-line";
const HOVER = "season-tracks-hover";
/** No wind: the storm layers' grey. */
const NO_CLASS = "#9a9a9a";

export interface SeasonTrack {
  id: string;
  basin: string;
  /** The strongest class reached, −1 for none: the drawing order. */
  peak: number;
  /** [lon, lat, class], longitudes continuous across the antimeridian and
   * on the Pacific-centred side (20° to 380°). */
  points: [number, number, number][];
}

/** The overview's tracks, unwrapped. A basin is the storm id's prefix. */
export function seasonTracks(overview: SeasonOverview | null): SeasonTrack[] {
  return (overview?.storms ?? [])
    .filter((storm) => storm.track.length > 0)
    .map((storm) => {
      const points: [number, number, number][] = [];
      for (const [lon10, lat10, cls] of storm.track) {
        let lon = lon10 / 10;
        if (points.length === 0) lon = lon < 20 ? lon + 360 : lon > 380 ? lon - 360 : lon;
        else {
          const previous = points[points.length - 1]![0];
          while (lon - previous > 180) lon -= 360;
          while (previous - lon > 180) lon += 360;
        }
        points.push([lon, lat10 / 10, cls]);
      }
      return { id: storm.id, basin: storm.id.slice(0, 2), peak: Math.max(...points.map((point) => point[2])), points };
    });
}

/** One feature per segment, coloured by the class at its start. */
export function seasonFeatures(tracks: readonly SeasonTrack[]): GeoJSON.FeatureCollection<GeoJSON.LineString> {
  const features: GeoJSON.Feature<GeoJSON.LineString>[] = [];
  for (const track of tracks) {
    for (let index = 0; index + 1 < track.points.length; index++) {
      const [lon, lat, cls] = track.points[index]!;
      const [nextLon, nextLat] = track.points[index + 1]!;
      features.push({
        type: "Feature",
        properties: { id: track.id, basin: track.basin, cls, peak: track.peak },
        geometry: { type: "LineString", coordinates: [[lon, lat], [nextLon, nextLat]] },
      });
    }
  }
  return { type: "FeatureCollection", features };
}

/** The tracks' extent, [[west, south], [east, north]], or null for none. */
export function tracksBounds(tracks: readonly SeasonTrack[]): [[number, number], [number, number]] | null {
  const points = tracks.flatMap((track) => track.points);
  if (points.length === 0) return null;
  const lons = points.map((point) => point[0]);
  const lats = points.map((point) => point[1]);
  return [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];
}

const classColor = [
  "match",
  ["get", "cls"],
  ...SSHS_CLASSES.flatMap((item, index) => [index, item.color]),
  NO_CLASS,
] as unknown as string;

export interface SeasonState {
  /** Only this basin's tracks, or every basin. */
  basin: string | null;
  /** The storm drawn by the storm layers: left out here, the rest faded. */
  selected: string | null;
  /** The track under the pointer or the list row, drawn heavier. */
  hover: string | null;
  darkGround: boolean;
}

export class SeasonLayers {
  constructor(private readonly map: MaplibreMap) {}

  /** Add the source and layers to the style on screen (again after a style
   * swap); false when they were there. */
  ensure(before?: string): boolean {
    if (this.map.getSource(SOURCE)) return false;
    this.map.addSource(SOURCE, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    const lines: LineLayerSpecification["layout"] = { "line-cap": "round", "line-join": "round", "line-sort-key": ["get", "peak"] };
    this.map.addLayer({ id: HALO, type: "line", source: SOURCE, layout: lines, paint: { "line-width": 3 } }, before);
    this.map.addLayer(
      { id: SEASON_TRACK_LAYER, type: "line", source: SOURCE, layout: lines, paint: { "line-color": classColor, "line-width": 1.6 } },
      before,
    );
    this.map.addLayer(
      {
        id: HOVER,
        type: "line",
        source: SOURCE,
        layout: lines,
        paint: { "line-color": classColor, "line-width": 3.4 },
        filter: ["==", ["get", "id"], ""],
      },
      before,
    );
    return true;
  }

  setTracks(tracks: readonly SeasonTrack[]): void {
    (this.map.getSource(SOURCE) as GeoJSONSource | undefined)?.setData(seasonFeatures(tracks));
  }

  setState(state: SeasonState): void {
    if (!this.map.getLayer(SEASON_TRACK_LAYER)) return;
    const shown: unknown[] = ["all"];
    if (state.basin !== null) shown.push(["==", ["get", "basin"], state.basin]);
    if (state.selected !== null) shown.push(["!=", ["get", "id"], state.selected]);
    const filter = shown as FilterSpecification;
    const faded = state.selected !== null;
    // Faint enough that the storm's forecasts, drawn in greys above it, are
    // never mistaken for another storm's track.
    const fadedInk = state.darkGround ? "rgba(255,255,255,0.16)" : "rgba(27,26,23,0.12)";
    const halo = state.darkGround ? "rgba(0,0,0,0.45)" : "rgba(27,26,23,0.22)";
    this.map.setFilter(HALO, filter);
    this.map.setFilter(SEASON_TRACK_LAYER, filter);
    this.map.setPaintProperty(HALO, "line-color", faded ? "rgba(0,0,0,0)" : halo);
    this.map.setPaintProperty(SEASON_TRACK_LAYER, "line-color", faded ? fadedInk : classColor);
    this.map.setPaintProperty(SEASON_TRACK_LAYER, "line-width", faded ? 0.8 : 1.6);
    const hover = state.hover !== null && state.hover !== state.selected && (state.basin === null || state.hover.startsWith(state.basin));
    this.map.setFilter(HOVER, ["==", ["get", "id"], hover ? state.hover! : ""]);
  }
}
