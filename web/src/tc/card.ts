/** The card a clicked storm point opens: the centre's own numbers for that
 * time, as the product carries them — its class as it spells it, the
 * sustained wind in the knots the centres speak and in the viewer's wind
 * unit, the central pressure, the four quadrant radii at each threshold,
 * the radius of maximum wind and the gust when reported, each in the
 * viewer's units (`units.ts`). Codes (`NHC`, `34KT`, `RMW`) are instrument
 * text and stay English — a threshold is named in knots whatever the wind
 * unit; the valid time is the shell's, read in its display zone. */

import { element } from "../dom";
import { t } from "../i18n";
import { coordinateFormat } from "../mapprefs";
import { toMgrs } from "../mgrs";
import { convertDistance, formatDistance, formatPressure, formatWind as formatSpeed, UNIT_LABELS, units } from "../units";
import { agencyColor, TC_AGENCIES } from "./agencies";
import type { TcPointData } from "./layers";
import { QUADRANT_CODES } from "./schema";

const KNOT = 0.514444;

interface CardOptions {
  /** The valid time, formatted the way the capsule formats one. */
  formatTime(time: string): string;
}

function formatLead(seconds: number): string {
  const hours = Math.round(seconds / 3600);
  return `${hours >= 0 ? "+" : "−"}${Math.abs(hours)}h`;
}

function formatWind(value: number): string {
  const knots = `${Math.round(value / KNOT)} kt`;
  return units.wind === "kt" ? knots : `${knots} · ${formatSpeed(value, 0)}`;
}

function formatKm(value: number | null): string {
  return value === null ? "—" : `${Math.round(convertDistance(value))}`;
}

export function buildTcCard(
  data: TcPointData,
  options: CardOptions,
): HTMLElement {
  const root = element("div", "tc-card");
  const color = agencyColor(data.agency);
  if (color) root.style.setProperty("--card-ink", color);

  const head = element("div", "tc-card-head");
  head.append(element("span", "tc-card-name", data.name));
  const who = element("span", "tc-card-code");
  const agency = TC_AGENCIES[data.agency];
  who.textContent =
    data.source === "best"
      ? `${t("tcBest").toUpperCase()} · ${data.code}`
      : data.code;
  if (agency) who.title = agency.name;
  head.append(who);
  root.append(head);

  const when = element("div", "tc-card-time");
  when.append(element("span", "", options.formatTime(data.time)));
  if (data.lead !== undefined)
    when.append(element("span", "tc-card-lead", formatLead(data.lead)));
  if (data.number)
    when.append(element("span", "tc-card-lead", `#${data.number}`));
  root.append(when);

  const headline = element("div", "tc-card-headline");
  if (data.class) headline.append(element("b", "tc-card-class", data.class));
  if (data.vmax !== null)
    headline.append(element("span", "", formatWind(data.vmax)));
  // An agency's whole hectopascals read whole; a measured 922.8 keeps its tenth.
  if (data.pmin !== null)
    headline.append(element("span", "", formatPressure(data.pmin, Number.isInteger(data.pmin) ? 0 : 1)));
  if (headline.childElementCount) root.append(headline);

  if (data.radii) {
    const table = element("table", "tc-card-radii");
    const header = table.createTHead().insertRow();
    header.append(element("th", "", ""));
    for (const quadrant of QUADRANT_CODES)
      header.append(element("th", "", quadrant));
    header.append(element("th", "tc-card-unit", UNIT_LABELS.distance[units.distance]));
    const body = table.createTBody();
    for (const threshold of ["34", "50", "64"] as const) {
      const quadrants = data.radii[threshold];
      if (!quadrants) continue;
      const row = body.insertRow();
      row.append(element("th", "", `${threshold}KT`));
      for (const value of quadrants)
        row.append(element("td", "", formatKm(value)));
      row.append(element("td", "", ""));
    }
    if (body.rows.length) root.append(table);
  }

  const extras: string[] = [];
  if (data.rmw !== null) extras.push(`RMW ${formatDistance(data.rmw, 0)}`);
  if (data.gust !== null) extras.push(`GUST ${formatWind(data.gust)}`);
  if (extras.length)
    root.append(element("div", "tc-card-extra", extras.join(" · ")));

  // A best-track position is good to a tenth of a degree at best (a
  // reconstructed one to about a kilometre), so its MGRS reference names the
  // kilometre square, not the metre.
  const mgrs = coordinateFormat === "mgrs" ? toMgrs(data.lat, data.lon, 2) : null;
  const where = element(
    "div",
    "tc-card-position",
    mgrs ?? `${Math.abs(data.lat).toFixed(1)}°${data.lat >= 0 ? "N" : "S"} ${Math.abs(data.lon).toFixed(1)}°${data.lon >= 0 ? "E" : "W"}`,
  );
  root.append(where);
  return root;
}
