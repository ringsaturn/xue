/** The card a clicked station mark opens, built the way the storm card is:
 * what the index already carries about that station, and nothing fetched.
 *
 * A sounding's card is its headline — the 500 hPa temperature and dew
 * point, the freezing level, the precipitable water and the level count —
 * under the nominal time the ascent is filed at. An airport's is its
 * newest observation decoded: the raw METAR is not in the index, so the
 * card shows the values rather than the line.
 *
 * Station identifiers (`RJTT`, a WMO number), units (`hPa`, `m/s`, `gpm`)
 * and the flight categories (`VFR`) are instrument text and stay English;
 * the row labels and the valid time are the shell's. */

import { t } from "../i18n";
import { categoryColor } from "./layers";
import type { AirportStation, SoundingStationEntry } from "./schema";

export interface StationCardOptions {
  /** The valid time, formatted the way the capsule formats one. */
  formatTime(time: string): string;
  /** Which ground the map is on, for the category's own color. */
  darkGround?: boolean;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** One labelled value under the headline. A row whose value is missing is
 * left out rather than shown empty: the product says what it measured. */
function rows(entries: readonly (readonly [string, string | null])[]): HTMLElement | null {
  const present = entries.filter((entry): entry is [string, string] => entry[1] !== null);
  if (present.length === 0) return null;
  const list = element("dl", "station-card-rows");
  for (const [label, value] of present) {
    list.append(element("dt", "", label));
    list.append(element("dd", "", value));
  }
  return list;
}

function position(lat: number, lon: number): HTMLElement {
  return element(
    "div",
    "station-card-position",
    `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"} ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? "E" : "W"}`,
  );
}

function celsius(value: number | null): string | null {
  return value === null ? null : `${value.toFixed(1)} °C`;
}

/** Metres, as the product means them: ten kilometres is the service's
 * "ten or more" ceiling and reads as such. */
function visibility(value: number | null): string | null {
  if (value === null) return null;
  if (value >= 10000) return "≥ 10 km";
  if (value >= 1000) return `${(value / 1000).toFixed(1)} km`;
  return `${value} m`;
}

/** The wind as one line: the direction it comes from, the speed, and the
 * gust where one is reported. A variable direction is `VRB`, the code the
 * report itself uses, since the direction is not known. */
function wind(wd: number | null, ws: number | null, gust: number | null): string | null {
  if (ws === null) return null;
  const from = wd === null ? "VRB" : `${String(Math.round(wd)).padStart(3, "0")}°`;
  const speed = `${ws.toFixed(1)} m/s`;
  return gust === null ? `${from} ${speed}` : `${from} ${speed} G ${gust.toFixed(1)}`;
}

export function buildSoundingCard(
  station: SoundingStationEntry,
  options: StationCardOptions,
): HTMLElement {
  const root = element("div", "station-card");

  const head = element("div", "station-card-head");
  // The WMO number is how a sounding is named on a chart; the WIGOS id is
  // what the product keys on, and stands in where there is no number.
  head.append(element("span", "station-card-name", station.wmo ?? station.id));
  head.append(element("span", "station-card-kind", t("stationSounding")));
  root.append(head);

  const when = element("div", "station-card-time");
  when.append(element("span", "", options.formatTime(station.latest)));
  if (station.name) when.append(element("span", "station-card-code", station.name));
  root.append(when);

  const headline = element("div", "station-card-headline");
  const { t500, td500 } = station.headline;
  if (t500 !== null || td500 !== null) {
    headline.append(element("b", "station-card-level", "500 hPa"));
    if (t500 !== null) headline.append(element("span", "", celsius(t500)!));
    if (td500 !== null)
      headline.append(element("span", "station-card-dew", `Td ${celsius(td500)!}`));
    root.append(headline);
  }

  const table = rows([
    [
      t("stationFreezingLevel"),
      station.headline.freezingLevel === null
        ? null
        : `${Math.round(station.headline.freezingLevel)} gpm`,
    ],
    [
      t("stationPw"),
      station.headline.pw === null ? null : `${station.headline.pw.toFixed(1)} mm`,
    ],
    [t("stationLevels"), String(station.headline.levels)],
  ]);
  if (table) root.append(table);

  root.append(position(station.lat, station.lon));
  return root;
}

export function buildAirportCard(
  station: AirportStation,
  options: StationCardOptions,
): HTMLElement {
  const root = element("div", "station-card");
  root.style.setProperty(
    "--card-ink",
    categoryColor(station.category, options.darkGround ?? false),
  );

  const head = element("div", "station-card-head");
  head.append(element("span", "station-card-name", station.icao));
  head.append(element("span", "station-card-kind", t("stationAirport")));
  root.append(head);

  const when = element("div", "station-card-time");
  when.append(element("span", "", options.formatTime(station.obsTime)));
  when.append(element("span", "station-card-code", t("stationObserved")));
  root.append(when);

  const headline = element("div", "station-card-headline");
  if (station.category)
    headline.append(element("b", "station-card-category", station.category));
  if (station.t !== null) headline.append(element("span", "", celsius(station.t)!));
  if (station.td !== null)
    headline.append(element("span", "station-card-dew", `Td ${celsius(station.td)!}`));
  const air = wind(station.wd, station.ws, station.gust);
  if (air) headline.append(element("span", "station-card-wind", air));
  if (headline.childElementCount) root.append(headline);

  const table = rows([
    [t("stationCategory"), station.category],
    [t("stationVisibility"), visibility(station.vis)],
    [t("stationQnh"), station.qnh === null ? null : `${station.qnh.toFixed(1)} hPa`],
  ]);
  if (table) root.append(table);

  root.append(position(station.lat, station.lon));
  return root;
}
