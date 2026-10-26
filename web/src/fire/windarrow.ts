/** The wind arrow over a case's fire marks: the mean 10 m wind at the
 * newest pass's detections, from the start of that pass to the playhead.
 * The orbiters see the fire twice a day; what the wind did in between is
 * what the next pass shows the fire line to have done, so the arrow grows
 * its average hour by hour until the next pass resets it.
 *
 * The arrow points downwind, the way the air (and the fire) goes, and its
 * length is a fixed scale of the mean speed on the ground — kilometres per
 * m/s — so it shrinks and grows with the map like the marks. The label
 * at its tip carries the number and the time the average runs from. */

import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import type { GeoJSONSource, Map as MaplibreMap } from "maplibre-gl";

const SOURCE = "fire-wind-arrow";
const LAYERS = {
  casing: "fire-wind-arrow-casing",
  line: "fire-wind-arrow-line",
  label: "fire-wind-arrow-label",
} as const;

/** Ground length per m/s of mean wind: 10 m/s draws 30 km, about the
 * distance a fire line runs in half a day of strong wind, and a few
 * centimetres of screen at the zoom a case opens on. */
export const ARROW_KM_PER_MS = 3;
/** The head's arms, as a share of the shaft and at this angle off it. */
const HEAD_SHARE = 0.3;
const HEAD_MIN_KM = 3;
const HEAD_ANGLE = (150 * Math.PI) / 180;
const KM_PER_DEGREE = 111.32;

export interface FireWindMean {
  /** Where the arrow starts: the newest pass's detections' mean position. */
  center: [number, number];
  /** The mean wind, m/s toward east and north. */
  u: number;
  v: number;
  /** The epoch ms the average runs from. */
  from: number;
}

/** A point `east`, `north` km from `[lon, lat]`, on the flat-earth
 * approximation a few tens of kilometres allow. */
function offset([lon, lat]: [number, number], east: number, north: number): [number, number] {
  const cos = Math.max(0.01, Math.cos((lat * Math.PI) / 180));
  return [lon + east / (KM_PER_DEGREE * cos), lat + north / KM_PER_DEGREE];
}

/** `04:42Z` for an epoch ms. */
function clock(time: number): string {
  return `${new Date(time).toISOString().slice(11, 16)}Z`;
}

/** The arrow as features: the shaft and the head's two arms as lines, and
 * the label at the tip (at the tail in a calm, which has no shaft). */
export function arrowFeatures(mean: FireWindMean): FeatureCollection {
  const speed = Math.hypot(mean.u, mean.v);
  const features: Feature[] = [];
  let labelAt = mean.center;
  if (speed > 0) {
    const length = speed * ARROW_KM_PER_MS;
    const east = (mean.u / speed) * length;
    const north = (mean.v / speed) * length;
    const tip = offset(mean.center, east, north);
    const head = Math.max(HEAD_MIN_KM, length * HEAD_SHARE);
    const angle = Math.atan2(north, east);
    const arm = (turn: number) =>
      offset(tip, Math.cos(angle + turn) * head, Math.sin(angle + turn) * head);
    const lines: [number, number][][] = [
      [mean.center, tip],
      [arm(HEAD_ANGLE), tip, arm(-HEAD_ANGLE)],
    ];
    for (const coordinates of lines) {
      features.push({ type: "Feature", geometry: { type: "LineString", coordinates }, properties: {} } satisfies Feature<LineString>);
    }
    labelAt = tip;
  }
  features.push({
    type: "Feature",
    geometry: { type: "Point", coordinates: labelAt },
    properties: { label: `${speed.toFixed(1)} m/s · ${clock(mean.from)} →` },
  } satisfies Feature<Point>);
  return { type: "FeatureCollection", features };
}

export class FireWindArrow {
  private added = false;
  /** What is drawn, so a repeat of the same mean is not a rebuild. */
  private shownKey: string | null = null;

  constructor(private readonly map: MaplibreMap) {}

  ensure(before?: string): void {
    if (this.added) return;
    const empty: FeatureCollection = { type: "FeatureCollection", features: [] };
    if (!this.map.getSource(SOURCE)) this.map.addSource(SOURCE, { type: "geojson", data: empty });
    // White over a dark casing reads over a false-colour picture, a light
    // basemap and a dark one alike, and is neither of the marks' colours.
    if (!this.map.getLayer(LAYERS.casing))
      this.map.addLayer(
        {
          id: LAYERS.casing,
          type: "line",
          source: SOURCE,
          filter: ["==", ["geometry-type"], "LineString"],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#141414", "line-opacity": 0.7, "line-width": 5 },
        },
        before,
      );
    if (!this.map.getLayer(LAYERS.line))
      this.map.addLayer(
        {
          id: LAYERS.line,
          type: "line",
          source: SOURCE,
          filter: ["==", ["geometry-type"], "LineString"],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#ffffff", "line-width": 2.2 },
        },
        before,
      );
    if (!this.map.getLayer(LAYERS.label))
      this.map.addLayer(
        {
          id: LAYERS.label,
          type: "symbol",
          source: SOURCE,
          filter: ["==", ["geometry-type"], "Point"],
          layout: {
            "text-field": ["get", "label"],
            "text-font": ["Noto Sans Medium"],
            "text-size": 12,
            // Beside the tip, on whichever side the other labels leave
            // room; the layer sits on top, so it is placed first.
            "text-variable-anchor": ["left", "right", "top", "bottom"],
            "text-radial-offset": 0.7,
          },
          paint: { "text-color": "#ffffff", "text-halo-color": "#141414", "text-halo-width": 1.4 },
        },
        before,
      );
    this.added = true;
    this.shownKey = null;
  }

  /** Draw the mean, or nothing for null. */
  show(mean: FireWindMean | null): void {
    if (!this.added) return;
    const key = mean
      ? `${mean.center.join(",")}|${mean.u.toFixed(2)}|${mean.v.toFixed(2)}|${mean.from}`
      : null;
    if (key === this.shownKey) return;
    this.shownKey = key;
    const source = this.map.getSource(SOURCE) as GeoJSONSource | undefined;
    source?.setData(mean ? arrowFeatures(mean) : { type: "FeatureCollection", features: [] });
  }

  remove(): void {
    if (!this.added) return;
    for (const id of Object.values(LAYERS)) if (this.map.getLayer(id)) this.map.removeLayer(id);
    if (this.map.getSource(SOURCE)) this.map.removeSource(SOURCE);
    this.added = false;
    this.shownKey = null;
  }
}

/** The mean wind over `[from, to]` from a series sampled at `times`: the
 * vector mean of the samples inside, or the latest sample not after `to`
 * when none falls inside (a pass minutes old); null with no sample at or
 * before `to`. NaN samples (missing frames) are skipped. */
export function meanWind(
  times: readonly number[],
  u: ArrayLike<number>,
  v: ArrayLike<number>,
  from: number,
  to: number,
): { u: number; v: number; count: number; last: number } | null {
  let sumU = 0;
  let sumV = 0;
  let count = 0;
  let latest = -1;
  for (let i = 0; i < times.length; i++) {
    const time = times[i]!;
    if (time > to) break;
    if (!Number.isFinite(u[i]!) || !Number.isFinite(v[i]!)) continue;
    latest = i;
    if (time < from) continue;
    sumU += u[i]!;
    sumV += v[i]!;
    count += 1;
  }
  if (count > 0) return { u: sumU / count, v: sumV / count, count, last: latest };
  if (latest < 0) return null;
  return { u: u[latest]!, v: v[latest]!, count: 1, last: latest };
}
