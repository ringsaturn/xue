/** Where the static airport pages live (`web/tooling/airportpages.ts`
 * writes them), and what a page hands its live block (`live.ts`). The
 * pages share the storm pages' language layout (`tc/pages.ts`): English at
 * the root, four languages under a prefix. */

import { pagePrefix } from "../tc/pages";

/** `/ja/airports/` */
export function airportIndexPath(locale: string): string {
  return `${pagePrefix(locale)}/airports/`;
}

/** `/ja/airports/rjtt`, as Pages serves it (no `.html`). */
export function airportPagePath(locale: string, icao: string): string {
  return `${pagePrefix(locale)}/airports/${icao.toLowerCase()}`;
}

/** The id of the element the live block fills, and of the JSON script
 * inside it that configures it. */
export const LIVE_ELEMENT_ID = "live";
export const LIVE_CONFIG_ID = "live-config";
/** The id of the element the model forecast fills (`models.ts`). */
export const MODELS_ELEMENT_ID = "models";

/** The labels the live block writes, in the page's language. Unit symbols,
 * flight categories, cloud codes and the reports themselves stay as the
 * product carries them. A label with holes (`{n}`) is a template the block
 * fills: the words travel to it as JSON, so they are strings, not
 * functions. */
export interface LiveWords {
  loading: string;
  unavailable: string;
  latest: string;
  /** `{n}`: minutes since the report. */
  minutesAgo: string;
  /** `{n}`: hours since the report. */
  hoursAgo: string;
  temperature: string;
  dewPoint: string;
  wind: string;
  visibility: string;
  pressure: string;
  clouds: string;
  weather: string;
  category: string;
  today: string;
  /** `{n}`: reports counted; `{since}`: the local day. */
  todayNote: string;
  high: string;
  low: string;
  history: string;
  time: string;
  forecast: string;
  /** `{from}`, `{to}`: the forecast's validity. */
  forecastValid: string;
  noForecast: string;
  prevailing: string;
  modelsLoading: string;
  modelsUnavailable: string;
  precipitation: string;
  /** The legend's name for the airport's own reports. */
  observed: string;
  /** The axis mark at the present moment. */
  now: string;
}

/** Everything the live block needs from the page, as the page writes it
 * into `#live-config`. */
export interface LiveConfig {
  icao: string;
  /** The airport's IANA zone: the day "today" means, and the clock the
   * tables read in. */
  tz: string;
  /** The airport's position, the point the model forecast is read at. */
  lat: number;
  lon: number;
  /** BCP 47 tag the page is written in, for dates and numbers. */
  lang: string;
  words: LiveWords;
}

/** A template's holes filled: `"{n} min ago"` with `{ n: 5 }`. */
export function fill(template: string, values: Readonly<Record<string, string | number>>): string {
  return template.replace(/\{(\w+)\}/g, (hole, name: string) => (name in values ? String(values[name]) : hole));
}
