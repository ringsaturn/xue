/** The live block of a static airport page: the newest METAR raw and
 * decoded, the day's observed high and low on the airport's clock, the last
 * 24 hours as a temperature line and a table, and the current TAF with its
 * groups. One pointer, one index and one range read of the station's line
 * (`docs/airport.md` §1), through the shell's own reader.
 *
 * The page is complete without it: everything here is progressive, and a
 * failure leaves the page's static text with a one-line note. The words
 * come from the page (`#live-config`), so this module carries no
 * translations. Values read in the viewer's units (`units.ts`). */

import { dataBaseUrl } from "../site";
import { fetchAirportIndex, fetchAirportStation } from "../stations/fetch";
import { formatClouds, formatVisibility, formatWindGust } from "../stations/observations";
import type { AirportStationHistory, Metar, Taf, TafPeriod } from "../stations/schema";
import { convertTemperature, formatPressure, formatTemperature, UNIT_LABELS, units } from "../units";
import { fill, LIVE_CONFIG_ID, LIVE_ELEMENT_ID, type LiveConfig } from "./pages";
import { dayExtremes } from "./today";

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

function clock(config: LiveConfig, withDay: boolean): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat(config.lang, {
    timeZone: config.tz,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(withDay ? { month: "short", day: "numeric" } : {}),
  });
}

function ago(config: LiveConfig, time: string, nowMs: number): string {
  const minutes = Math.max(0, Math.round((nowMs - Date.parse(time)) / 60000));
  return minutes < 120 ? fill(config.words.minutesAgo, { n: minutes }) : fill(config.words.hoursAgo, { n: Math.round(minutes / 60) });
}

function rows(entries: readonly (readonly [string, string | null])[]): string {
  return entries
    .filter((entry): entry is readonly [string, string] => entry[1] !== null)
    .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("");
}

function category(value: string | null): string {
  return value ? `<span class="category" data-category="${escapeHtml(value)}">${escapeHtml(value)}</span>` : "—";
}

function latest(config: LiveConfig, metar: Metar, nowMs: number): string {
  const w = config.words;
  const when = new Intl.DateTimeFormat(config.lang, {
    timeZone: config.tz,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  }).format(Date.parse(metar.time));
  const facts = rows([
    [w.temperature, metar.t === null ? null : formatTemperature(metar.t)],
    [w.dewPoint, metar.td === null ? null : formatTemperature(metar.td)],
    [w.wind, formatWindGust(metar.wd, metar.ws, metar.gust)],
    [w.visibility, formatVisibility(metar.vis)],
    [w.pressure, metar.qnh === null ? null : `QNH ${formatPressure(metar.qnh)}`],
    [w.clouds, formatClouds(metar.cloud)],
    [w.weather, metar.wx],
  ]);
  return `<h2>${escapeHtml(w.latest)}</h2>
    <p class="note">${escapeHtml(when)} · ${escapeHtml(ago(config, metar.time, nowMs))} · ${category(metar.category)}</p>
    <pre class="raw">${escapeHtml(metar.raw)}</pre>
    <dl class="facts">${facts}</dl>`;
}

function today(config: LiveConfig, metars: readonly Metar[], nowMs: number): string {
  const extremes = dayExtremes(metars, config.tz, nowMs);
  if (extremes === null) return "";
  const w = config.words;
  const at = clock(config, false);
  const facts = rows([
    [w.high, `${formatTemperature(extremes.high.t)} · ${at.format(Date.parse(extremes.high.time))}`],
    [w.low, `${formatTemperature(extremes.low.t)} · ${at.format(Date.parse(extremes.low.time))}`],
  ]);
  return `<h2>${escapeHtml(w.today)}</h2><dl class="facts">${facts}</dl>
    <p class="note">${escapeHtml(fill(w.todayNote, { n: extremes.reports, since: extremes.day }))}</p>`;
}

/** The temperature over the window as a line, oldest on the left. */
function temperatureLine(metars: readonly Metar[]): string {
  const points = metars
    .filter((metar) => metar.t !== null)
    .map((metar) => ({ x: Date.parse(metar.time), y: convertTemperature(metar.t!) }))
    .sort((a, b) => a.x - b.x);
  if (points.length < 2) return "";
  const width = 640;
  const height = 120;
  const pad = 18;
  const x0 = points[0]!.x;
  const x1 = points.at(-1)!.x;
  let low = Math.min(...points.map((point) => point.y));
  let high = Math.max(...points.map((point) => point.y));
  if (high - low < 4) {
    const middle = (high + low) / 2;
    low = middle - 2;
    high = middle + 2;
  }
  const x = (value: number) => (((value - x0) / (x1 - x0 || 1)) * (width - 2 * pad) + pad).toFixed(1);
  const y = (value: number) => (height - pad - ((value - low) / (high - low)) * (height - 2 * pad)).toFixed(1);
  const path = points.map((point, index) => `${index ? "L" : "M"}${x(point.x)} ${y(point.y)}`).join("");
  const label = UNIT_LABELS.temperature[units.temperature];
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}">
    <path d="${path}"/>
    <text x="2" y="${pad - 6}">${high.toFixed(0)} ${escapeHtml(label)}</text>
    <text x="2" y="${height - 4}">${low.toFixed(0)} ${escapeHtml(label)}</text>
  </svg>`;
}

function history(config: LiveConfig, metars: readonly Metar[]): string {
  const w = config.words;
  const at = clock(config, false);
  const body = metars
    .map(
      (metar) =>
        `<tr><td>${escapeHtml(at.format(Date.parse(metar.time)))}</td><td>${metar.t === null ? "—" : escapeHtml(formatTemperature(metar.t, 0))}</td><td>${metar.td === null ? "—" : escapeHtml(formatTemperature(metar.td, 0))}</td><td>${escapeHtml(formatWindGust(metar.wd, metar.ws, metar.gust) ?? "—")}</td><td>${escapeHtml(formatVisibility(metar.vis) ?? "—")}</td><td>${metar.qnh === null ? "—" : escapeHtml(formatPressure(metar.qnh))}</td><td>${category(metar.category)}</td></tr>`,
    )
    .join("");
  return `<h2>${escapeHtml(w.history)}</h2>${temperatureLine(metars)}
    <div class="scroll"><table><thead><tr><th>${escapeHtml(w.time)}</th><th>${escapeHtml(w.temperature)}</th><th>${escapeHtml(w.dewPoint)}</th><th>${escapeHtml(w.wind)}</th><th>${escapeHtml(w.visibility)}</th><th>${escapeHtml(w.pressure)}</th><th>${escapeHtml(w.category)}</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function periodName(period: TafPeriod, words: LiveConfig["words"]): string {
  if (period.change === null && period.prob === null) return words.prevailing;
  const prob = period.prob === null ? "" : `PROB${period.prob}`;
  const change = period.change === null || period.change === "PROB" ? "" : period.change;
  return [prob, change].filter(Boolean).join(" ");
}

function forecast(config: LiveConfig, taf: Taf | null): string {
  const w = config.words;
  if (taf === null) return `<h2>${escapeHtml(w.forecast)}</h2><p class="note">${escapeHtml(w.noForecast)}</p>`;
  const at = clock(config, true);
  const body = taf.periods
    .map(
      (period) =>
        `<tr><td>${escapeHtml(periodName(period, w))}</td><td>${escapeHtml(at.format(Date.parse(period.from)))} – ${escapeHtml(at.format(Date.parse(period.to)))}</td><td>${escapeHtml(formatWindGust(period.wd, period.ws, period.gust) ?? "")}</td><td>${escapeHtml(formatVisibility(period.vis) ?? "")}</td><td>${escapeHtml(period.wx ?? "")}</td><td>${escapeHtml(formatClouds(period.cloud) ?? "")}</td></tr>`,
    )
    .join("");
  const valid = fill(w.forecastValid, { from: at.format(Date.parse(taf.from)), to: at.format(Date.parse(taf.to)) });
  return `<h2>${escapeHtml(w.forecast)}</h2>
    <p class="note">${escapeHtml(valid)}</p>
    <pre class="raw">${escapeHtml(taf.raw)}</pre>
    <div class="scroll"><table><thead><tr><th></th><th>${escapeHtml(w.time)}</th><th>${escapeHtml(w.wind)}</th><th>${escapeHtml(w.visibility)}</th><th>${escapeHtml(w.weather)}</th><th>${escapeHtml(w.clouds)}</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

export function renderLive(config: LiveConfig, station: AirportStationHistory, nowMs: number): string {
  const newest = station.metars[0];
  return [
    newest ? latest(config, newest, nowMs) : "",
    today(config, station.metars, nowMs),
    forecast(config, station.taf),
    history(config, station.metars),
  ].join("");
}

async function start(): Promise<void> {
  const root = document.getElementById(LIVE_ELEMENT_ID);
  const source = document.getElementById(LIVE_CONFIG_ID);
  if (!root || !source?.textContent) return;
  const config = JSON.parse(source.textContent) as LiveConfig;
  const status = root.querySelector<HTMLElement>("[data-status]");
  const say = (text: string) => {
    if (status) status.textContent = text;
  };
  say(config.words.loading);
  try {
    // The shell's data root is relative to the site's root, not to this
    // page's directory.
    const base = new URL(dataBaseUrl(), new URL("/", location.href)).href;
    const loaded = await fetchAirportIndex(base);
    const row = loaded?.index.stations.find((station) => station.icao === config.icao);
    if (!loaded || !row) {
      say(config.words.unavailable);
      return;
    }
    const station = await fetchAirportStation(loaded, row);
    const content = document.createElement("div");
    content.innerHTML = renderLive(config, station, Date.now());
    root.replaceChildren(source, content);
  } catch (error) {
    console.warn("airport: live block failed:", error);
    say(config.words.unavailable);
  }
}

if (typeof document !== "undefined") void start();
