/** The card a clicked storm point opens: the centre's own numbers for that
 * time, as the product carries them — its class as it spells it, the
 * sustained wind in the knots the centres speak and in m/s, the central
 * pressure, the four quadrant radii at each threshold, the radius of
 * maximum wind and the gust when reported. Codes (`NHC`, `34KT`, `RMW`)
 * are instrument text and stay English; the valid time is the shell's,
 * read in its display zone. */

import { t } from "../i18n";
import { agencyColor, TC_AGENCIES } from "./agencies";
import type { TcPointData } from "./layers";
import { QUADRANT_CODES } from "./schema";

const KNOT = 0.514444;

export interface CardOptions {
  /** The valid time, formatted the way the capsule formats one. */
  formatTime(time: string): string;
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

function formatLead(seconds: number): string {
  const hours = Math.round(seconds / 3600);
  return `${hours >= 0 ? "+" : "−"}${Math.abs(hours)}h`;
}

function formatWind(value: number): string {
  return `${Math.round(value / KNOT)} kt · ${Math.round(value)} m/s`;
}

function formatKm(value: number | null): string {
  return value === null ? "—" : `${Math.round(value)}`;
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
  if (data.pmin !== null)
    headline.append(element("span", "", `${Math.round(data.pmin)} hPa`));
  if (headline.childElementCount) root.append(headline);

  if (data.radii) {
    const table = element("table", "tc-card-radii");
    const header = table.createTHead().insertRow();
    header.append(element("th", "", ""));
    for (const quadrant of QUADRANT_CODES)
      header.append(element("th", "", quadrant));
    header.append(element("th", "tc-card-unit", "km"));
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
  if (data.rmw !== null) extras.push(`RMW ${Math.round(data.rmw)} km`);
  if (data.gust !== null) extras.push(`GUST ${formatWind(data.gust)}`);
  if (extras.length)
    root.append(element("div", "tc-card-extra", extras.join(" · ")));

  const where = element(
    "div",
    "tc-card-position",
    `${Math.abs(data.lat).toFixed(1)}°${data.lat >= 0 ? "N" : "S"} ${Math.abs(data.lon).toFixed(1)}°${data.lon >= 0 ? "E" : "W"}`,
  );
  root.append(where);
  return root;
}
