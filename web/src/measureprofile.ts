/**
 * The elevation profile under a finished distance, as a small chart: the
 * DEM's height against the horizontal distance along the line, the lowest
 * and highest heights and the line's length on its axes, and the profile's
 * readings (the ground length, the straight line) under it. A pointer over
 * the chart reads the height and the distance there and hands the point to
 * the map, which marks it on the line.
 *
 * Heights are drawn exaggerated, as every profile is: the chart's height is
 * fixed and the span of heights fills it, so a gentle slope over a long line
 * reads as a slope rather than a flat line.
 */

import { t } from "./i18n";

/** One sample of the profile: where it is, how far along the line, and the
 * DEM's height there, metres. */
export interface ProfilePoint {
  lon: number;
  lat: number;
  along: number;
  height: number;
}

const WIDTH = 280;
const HEIGHT = 96;
/** Room for the axis numbers: the heights on the left, the distances below. */
const LEFT = 44;
const BOTTOM = 16;
const TOP = 6;
const RIGHT = 6;

const SVG = "http://www.w3.org/2000/svg";

function svg<K extends keyof SVGElementTagNameMap>(name: K, attributes: Record<string, string | number>): SVGElementTagNameMap[K] {
  const element = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

/** The chart's height axis: the lowest and highest heights, padded so a
 * flat line does not sit on the frame. */
export function heightRange(track: readonly ProfilePoint[]): [number, number] {
  let low = Infinity;
  let high = -Infinity;
  for (const point of track) {
    low = Math.min(low, point.height);
    high = Math.max(high, point.height);
  }
  if (!Number.isFinite(low)) return [0, 1];
  const pad = Math.max(5, (high - low) * 0.08);
  return [low - pad, high + pad];
}

/** The sample nearest a distance along the line. */
export function nearestSample(track: readonly ProfilePoint[], along: number): number {
  let lo = 0;
  let hi = track.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (track[mid]!.along < along) lo = mid;
    else hi = mid;
  }
  return Math.abs(track[lo]!.along - along) <= Math.abs(track[hi]!.along - along) ? lo : hi;
}

export interface ProfileChartOptions {
  /** Lines read under the chart (the ground length, the straight line). */
  details: readonly string[];
  formatDistance: (metres: number) => string;
  formatHeight: (metres: number) => string;
  /** The sample under the pointer, or null when it leaves the chart. */
  onHover: (point: ProfilePoint | null) => void;
}

/** The chart for a profile's track. */
export function profileChart(track: readonly ProfilePoint[], options: ProfileChartOptions): HTMLElement {
  const root = document.createElement("figure");
  root.className = "measure-profile";
  const readout = document.createElement("figcaption");
  readout.className = "measure-profile-readout";
  const chart = svg("svg", { viewBox: `0 0 ${WIDTH} ${HEIGHT}`, width: WIDTH, height: HEIGHT, role: "img" });
  chart.setAttribute("aria-label", t("measureProfileAria"));
  root.append(readout, chart);
  for (const detail of options.details) {
    const line = document.createElement("p");
    line.className = "measure-profile-detail";
    line.textContent = detail;
    root.append(line);
  }
  if (track.length < 2) return root;

  const length = track[track.length - 1]!.along || 1;
  const [low, high] = heightRange(track);
  const x = (along: number) => LEFT + ((WIDTH - LEFT - RIGHT) * along) / length;
  const y = (height: number) => TOP + ((HEIGHT - TOP - BOTTOM) * (high - height)) / (high - low);
  const base = HEIGHT - BOTTOM;

  const line = track.map((point) => `${x(point.along).toFixed(1)},${y(point.height).toFixed(1)}`).join(" ");
  chart.append(
    svg("polygon", { class: "measure-profile-area", points: `${x(0).toFixed(1)},${base} ${line} ${x(length).toFixed(1)},${base}` }),
    svg("polyline", { class: "measure-profile-line", points: line }),
    svg("line", { class: "measure-profile-axis", x1: LEFT, y1: base, x2: WIDTH - RIGHT, y2: base }),
  );
  const label = (text: string, attributes: Record<string, string | number>) => {
    const node = svg("text", { class: "measure-profile-label", ...attributes });
    node.textContent = text;
    chart.append(node);
  };
  const lowest = Math.min(...track.map((point) => point.height));
  const highest = Math.max(...track.map((point) => point.height));
  label(options.formatHeight(highest), { x: LEFT - 4, y: y(highest) + 3, "text-anchor": "end" });
  if (highest - lowest >= 1) label(options.formatHeight(lowest), { x: LEFT - 4, y: y(lowest) + 3, "text-anchor": "end" });
  label("0", { x: LEFT, y: HEIGHT - 3, "text-anchor": "start" });
  label(options.formatDistance(length), { x: WIDTH - RIGHT, y: HEIGHT - 3, "text-anchor": "end" });

  const cursor = svg("line", { class: "measure-profile-cursor", x1: 0, y1: TOP, x2: 0, y2: base, visibility: "hidden" });
  const dot = svg("circle", { class: "measure-profile-dot", r: 3, cx: 0, cy: 0, visibility: "hidden" });
  chart.append(cursor, dot);

  // At rest the readout is the line's length and the span of heights; the
  // arrows are kept for the climb and the descent in the details.
  const resting = () => `${options.formatDistance(length)} · ${options.formatHeight(lowest)} – ${options.formatHeight(highest)}`;
  readout.textContent = resting();
  const show = (point: ProfilePoint | null) => {
    const visibility = point ? "visible" : "hidden";
    cursor.setAttribute("visibility", visibility);
    dot.setAttribute("visibility", visibility);
    if (point) {
      const px = x(point.along).toFixed(1);
      cursor.setAttribute("x1", px);
      cursor.setAttribute("x2", px);
      dot.setAttribute("cx", px);
      dot.setAttribute("cy", y(point.height).toFixed(1));
      readout.textContent = `${options.formatDistance(point.along)} · ${options.formatHeight(point.height)}`;
    } else {
      readout.textContent = resting();
    }
    options.onHover(point);
  };
  chart.addEventListener("pointermove", (event) => {
    const box = chart.getBoundingClientRect();
    const along = (((event.clientX - box.left) * (WIDTH / box.width) - LEFT) / (WIDTH - LEFT - RIGHT)) * length;
    show(track[nearestSample(track, Math.min(length, Math.max(0, along)))]!);
  });
  chart.addEventListener("pointerleave", () => show(null));
  return root;
}
