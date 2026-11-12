/**
 * The clock the timeline reads in.
 *
 * Every instant the shell shows a forecast *at* — the valid time on the
 * capsule, the tooltip, the day marks along the track and under the
 * meteogram, the probe's lead line — is written in one **display zone**.
 * It is the browser's zone until a point is pinned; then it is the zone the
 * pinned point lies in, so a viewer in Tokyo reading a storm over New York
 * sees it land at New York's hour; and the browser's again when the pin is
 * removed. A model run's cycle is not a display time: 00Z is the cycle's
 * name, and it stays in UTC wherever it is stamped.
 *
 * The point → zone lookup is `tzf-wasm`, a 4 MB polygon index (the
 * `timezone-boundary-builder` set with its ocean zones, so open water
 * answers `Etc/GMT±N`). It is loaded the first time a point is pinned and
 * never before, and its 150 ms of construction is left to a macrotask so
 * the click's paint is not held up by it. A zone the browser's own tz data
 * does not know (a boundary set newer than the engine) is treated as no
 * answer, and the browser's zone stays.
 *
 * That is the `auto` choice. The settings sheet can hold the clock instead
 * on the browser's zone (`local`), on UTC, or on any IANA zone the engine
 * knows, whatever is pinned; the choice is the reader's own habit and is
 * kept in storage, never in the address bar.
 *
 * Like `theme` and `locale`, `displayZone` is a live binding: read it at
 * use, never capture it, and repaint from `onDisplayZoneChange`.
 */

import { normalizeLongitude } from "./probe";

/** The IANA zone the browser's own clock keeps, `UTC` when that cannot be
 * read (very old engines resolve to `undefined`). */
export function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** What the clock follows: the pinned point (`auto`), the browser's zone
 * (`local`), UTC (`utc`), or one IANA zone by its id. */
export type ZonePref = string;
export const ZONE_MODES = ["auto", "local", "utc"] as const;

const STORAGE_KEY = "xue-zone";

/** A stored or picked choice, read leniently: a mode, or a zone this
 * engine can format in; anything else is `auto`. */
export function parseZonePref(value: string | null | undefined): ZonePref {
  const raw = (value ?? "").trim();
  if ((ZONE_MODES as readonly string[]).includes(raw.toLowerCase())) return raw.toLowerCase();
  return raw !== "" && isKnownZone(raw) ? raw : "auto";
}

function readZonePref(): ZonePref {
  try {
    return parseZonePref(localStorage.getItem(STORAGE_KEY));
  } catch {
    return "auto";
  }
}

/** The zone a choice puts the clock on, given the pinned point's zone. */
export function resolveZone(pref: ZonePref, pinned: string | null): string {
  if (pref === "utc") return "UTC";
  if (pref === "local") return browserZone();
  if (pref === "auto") return pinned ?? browserZone();
  return isKnownZone(pref) ? pref : browserZone();
}

/** The pinned point's zone, `null` with nothing pinned. */
let pinnedZone: string | null = null;

/** The reader's choice of clock. A live binding. */
export let zonePref: ZonePref = readZonePref();

/** The zone in force for every valid-time stamp on screen. */
export let displayZone: string = resolveZone(zonePref, pinnedZone);

const listeners = new Set<() => void>();

/** Run `listener` after every change of the display zone or of the choice
 * behind it; the listener rewrites whatever stamps it owns from
 * `displayZone`. */
export function onDisplayZoneChange(listener: () => void): void {
  listeners.add(listener);
}

function notify(): void {
  for (const listener of listeners) listener();
}

/** Record the pinned point's zone, or (`null`) that nothing is pinned. A
 * zone the engine cannot format in counts as none, so an unknown id can
 * never blank the capsule. The clock moves only under `auto`. */
export function setPinnedZone(zone: string | null): void {
  pinnedZone = zone !== null && isKnownZone(zone) ? zone : null;
  const next = resolveZone(zonePref, pinnedZone);
  if (next === displayZone) return;
  displayZone = next;
  notify();
}

/** Replace the reader's choice: persist it and repaint. Listeners hear of
 * it even when the zone stays (`auto` → `local` with nothing pinned), so
 * the settings sheet can mark the new choice. */
export function setZonePref(pref: ZonePref): void {
  zonePref = parseZonePref(pref);
  displayZone = resolveZone(zonePref, pinnedZone);
  try {
    localStorage.setItem(STORAGE_KEY, zonePref);
  } catch {
    // The choice still holds for this page.
  }
  notify();
}

/** Every IANA zone this engine knows, for the sheet's list; UTC alone
 * where the engine cannot say. */
export function knownZones(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return ["UTC"];
  }
}

/** The zone a typed query names, or `null` when it names none or several:
 * an id in any case (`asia/tokyo`), a city with spaces for underscores
 * (`new york`, `Tokyo`), or a part of exactly one id (`kolk`). */
export function matchZone(query: string, zones: readonly string[]): string | null {
  const wanted = query.trim().toLowerCase().replace(/\s+/g, "_");
  if (wanted === "") return null;
  const lower = zones.map((zone) => zone.toLowerCase());
  const exact = lower.indexOf(wanted);
  if (exact >= 0) return zones[exact]!;
  if (wanted === "utc" || wanted === "gmt") return zones.includes("UTC") ? "UTC" : null;
  const city = lower.flatMap((zone, index) => (zone.slice(zone.lastIndexOf("/") + 1) === wanted ? [zones[index]!] : []));
  if (city.length === 1) return city[0]!;
  const partial = lower.flatMap((zone, index) => (zone.includes(wanted) ? [zones[index]!] : []));
  return partial.length === 1 ? partial[0]! : null;
}

/** Whether this engine's tz data can format in `zone`. */
export function isKnownZone(zone: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

// Intl formatters cost milliseconds to build and every frame step formats
// three stamps, so each shape is kept per zone (and per language for the
// weekday, which follows the UI locale).
const stampFormats = new Map<string, Intl.DateTimeFormat>();
const partsFormats = new Map<string, Intl.DateTimeFormat>();
const weekdayFormats = new Map<string, Intl.DateTimeFormat>();

function stampFormat(zone: string): Intl.DateTimeFormat {
  let format = stampFormats.get(zone);
  if (!format) {
    // zh-CN's numeric pattern is `MM/DD HH:MM` in every engine; the h23
    // cycle is what keeps midnight `00:` rather than `24:`.
    format = new Intl.DateTimeFormat("zh-CN", {
      timeZone: zone,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
    stampFormats.set(zone, format);
  }
  return format;
}

function partsFormat(zone: string): Intl.DateTimeFormat {
  let format = partsFormats.get(zone);
  if (!format) {
    format = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    partsFormats.set(zone, format);
  }
  return format;
}

/** The zone's offset from UTC at `at`, in minutes east. Read back out of
 * the formatted wall clock rather than from `timeZoneName: "longOffset"`,
 * which older engines do not implement. */
export function zoneOffsetMinutes(zone: string, at: number): number {
  const instant = Math.floor(at / 1000) * 1000;
  const fields: Record<string, number> = {};
  for (const part of partsFormat(zone).formatToParts(new Date(instant))) {
    if (part.type !== "literal") fields[part.type] = Number(part.value);
  }
  const wall = Date.UTC(
    fields.year ?? 1970,
    (fields.month ?? 1) - 1,
    fields.day ?? 1,
    fields.hour ?? 0,
    fields.minute ?? 0,
    fields.second ?? 0,
  );
  return Math.round((wall - instant) / 60_000);
}

/** The zone as the offset it keeps at `at`: `UTC`, `UTC+9`, `UTC-3:30`.
 * Instrument-panel text — English in every locale, like `F058`. */
export function zoneOffsetLabel(zone: string, at: number): string {
  const minutes = zoneOffsetMinutes(zone, at);
  if (minutes === 0) return "UTC";
  const sign = minutes < 0 ? "-" : "+";
  const total = Math.abs(minutes);
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  return `UTC${sign}${hours}${rest === 0 ? "" : `:${String(rest).padStart(2, "0")}`}`;
}

/** How a zone is named to the viewer: its IANA id (`Asia/Tokyo`), except
 * the nameless ocean and fixed-offset zones (`Etc/GMT-9`, whose sign is
 * POSIX-inverted and reads wrong to everyone), which show as the offset. */
export function zoneDisplayName(zone: string, at: number): string {
  if (zone === "UTC" || zone.startsWith("Etc/")) return zoneOffsetLabel(zone, at);
  return zone;
}

/** `MM/DD HH:MM` in `zone`, with the offset the zone keeps at that instant
 * (`09/13 00:00 UTC+9`). The one full stamp the capsule reads aloud. */
export function formatStamp(value: string | number, zone: string): string {
  const at = typeof value === "number" ? value : Date.parse(value);
  return `${stampFormat(zone).format(new Date(at))} ${zoneOffsetLabel(zone, at)}`;
}

/** `MM/DD HH:MM` in `zone`, no offset: the tooltip's and the probe's lead
 * line, which sit under a capsule already naming the zone. Minutes are
 * kept because half-hour zones exist. */
export function formatCompactStamp(value: number, zone: string): string {
  return stampFormat(zone).format(new Date(value));
}

const clockFormats = new Map<string, Intl.DateTimeFormat>();

/** `HH:MM` in `zone`: what the instrument reads for an observation, whose
 * frame is a moment rather than a lead time. */
export function formatClockStamp(value: number, zone: string): string {
  let format = clockFormats.get(zone);
  if (!format) {
    format = new Intl.DateTimeFormat("zh-CN", {
      timeZone: zone,
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
    clockFormats.set(zone, format);
  }
  return format.format(new Date(value));
}

/** Short weekday in `lang` and the day of month, both read in `zone`: the
 * track's day marks. */
export function formatDayMark(value: number, zone: string, lang: string): string {
  const key = `${lang} ${zone}`;
  let format = weekdayFormats.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat(lang, { weekday: "short", timeZone: zone });
    weekdayFormats.set(key, format);
  }
  const day = partsFormat(zone)
    .formatToParts(new Date(value))
    .find((part) => part.type === "day")?.value;
  return `${format.format(new Date(value))} ${day ?? "--"}`;
}

/** The month an instant falls in, as `YYYY-MM` read in `zone`: how the
 * track's strip divides a seasonal axis, where a day mark per frame would
 * be nine months of weekday names on top of each other. A key, not a
 * label — nothing about it is shown. */
export function monthKey(value: number, zone: string): string {
  const fields: Record<string, string> = {};
  for (const part of partsFormat(zone).formatToParts(new Date(value))) {
    if (part.type !== "literal") fields[part.type] = part.value;
  }
  return `${fields.year ?? "0000"}-${fields.month ?? "00"}`;
}

const monthFormats = new Map<string, Intl.DateTimeFormat>();

/** The month's own name in `lang`, read in `zone`, and the year with it
 * where the strip has just crossed into one (`OCT`, `JAN 2027`). The name
 * is the engine's, never the dictionary's: every locale already has twelve
 * of them. */
export function formatMonthMark(value: number, zone: string, lang: string, withYear: boolean): string {
  const key = `${lang} ${zone} ${withYear ? "y" : "m"}`;
  let format = monthFormats.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat(lang, {
      timeZone: zone,
      month: "short",
      ...(withYear ? { year: "numeric" } : {}),
    });
    monthFormats.set(key, format);
  }
  return format.format(new Date(value));
}

/** The lookup, loaded once and shared by every pin after the first. */
interface ZoneFinder {
  get_tz_name(lng: number, lat: number): string;
}

let finder: Promise<ZoneFinder | null> | null = null;

function loadFinder(): Promise<ZoneFinder | null> {
  finder ??= (async () => {
    try {
      const [module, { default: wasmUrl }] = await Promise.all([
        import("tzf-wasm"),
        import("tzf-wasm/tzf_wasm_bg.wasm?url"),
      ]);
      await module.default({ module_or_path: wasmUrl });
      // Building the index parses the whole polygon set; a macrotask lets
      // the click that started this paint its panel first.
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
      return new module.WasmFinder();
    } catch (error) {
      console.warn("timezone lookup unavailable", error);
      return null;
    }
  })();
  return finder;
}

/** The IANA zone at a point, or `null` when nothing answers there or the
 * lookup could not be loaded. */
export async function zoneAt(longitude: number, latitude: number): Promise<string | null> {
  const found = await loadFinder();
  if (!found) return null;
  // MapLibre hands out the unwrapped longitude past the antimeridian on a
  // wrapped map, and the index knows no point there.
  const zone = found.get_tz_name(normalizeLongitude(longitude), latitude);
  return zone && isKnownZone(zone) ? zone : null;
}

/** For tests: stand a finder in for the wasm one. */
export function useZoneFinder(stub: ZoneFinder | null): void {
  finder = Promise.resolve(stub);
}
