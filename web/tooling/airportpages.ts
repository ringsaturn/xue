/** The airport pages: one static HTML page per airport in `airports.json`
 * and an index of them grouped by country, in the storm pages' five
 * languages and layout (`stormpages.ts`): English at the root, the others
 * under a prefix, every translation named with `hreflang`, the English page
 * doubling as the language chooser.
 *
 * What a page states is static and comes from the checked-in list: the
 * airport's names, codes, place, position, elevation and time zone, what the
 * page shows and how often the product updates, and links to the map, the
 * point comparison and the nearest listed airports, and which models the
 * forecast section reads. The weather itself is the live block (`web/src/airports/live.ts`), a small module the page loads
 * that reads the airport product (`docs/airport.md`) in the browser; without
 * it the page is still whole.
 *
 * The list is edited by hand. Membership never depends on the live
 * product, so a page does not come and go with a station's reports.
 * Node-only: imported by `discovery.ts`, never by a page. */

import { readFileSync } from "node:fs";

import type { Plugin } from "vite";

import { airportModelLabel, airportModels } from "../src/airports/modelchart";
import { airportIndexPath, airportPagePath, LIVE_CONFIG_ID, LIVE_ELEMENT_ID, MODELS_ELEMENT_ID, type LiveConfig, type LiveWords } from "../src/airports/pages";
import { SITE_NAME, SITE_ORIGIN } from "../src/site";
import { localizedPath } from "../src/sitepaths";
import { LANGUAGE_REDIRECT, PAGE_LANGS, pageFile, PAGES_CSS, siteNav, stylesheetHref, type PageLang } from "./stormpages";

// --- the list ----------------------------------------------------------------------

export interface Airport {
  icao: string;
  iata: string | null;
  /** English, from OurAirports. */
  name: string;
  city: string;
  /** ISO 3166-1 alpha-2. */
  country: string;
  lat: number;
  lon: number;
  /** Metres. */
  elev: number | null;
  /** IANA zone. */
  tz: string;
  wikidata: string | null;
  /** The name in the other page languages, where there is one. */
  names: Partial<Record<Exclude<PageLang, "en">, string>>;
  /** The city the airport serves, per language; English is `city`. */
  cityNames: Partial<Record<Exclude<PageLang, "en">, string>>;
}

export interface AirportList {
  schemaVersion: 1;
  /** The day the list last changed, `YYYY-MM-DD`: every page's `lastmod`. */
  updated: string;
  airports: Airport[];
}

const NAME_LANGS = ["zh", "zh-Hant", "ja", "ko"] as const;

function knownZone(zone: string): boolean {
  try {
    new Intl.DateTimeFormat("en", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

/** The list, checked: a field missing or out of range, or an ICAO id twice,
 * fails the build rather than publishing a broken page. */
export function validateAirports(input: unknown): AirportList {
  const data = input as { schemaVersion?: unknown; updated?: unknown; airports?: unknown } | null;
  if (data?.schemaVersion !== 1) throw new Error("airports.json: unsupported schemaVersion");
  if (typeof data.updated !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(data.updated)) throw new Error("airports.json: updated must be YYYY-MM-DD");
  if (!Array.isArray(data.airports) || data.airports.length === 0) throw new Error("airports.json: no airports");
  const seen = new Set<string>();
  const airports = data.airports.map((raw: unknown, index): Airport => {
    const entry = raw as Record<string, unknown>;
    const where = `airports.json: airports[${index}]`;
    const text = (key: string, pattern?: RegExp): string => {
      const value = entry[key];
      if (typeof value !== "string" || value.trim() === "" || (pattern && !pattern.test(value))) throw new Error(`${where}.${key} is missing or malformed`);
      return value;
    };
    const number = (key: string, min: number, max: number): number => {
      const value = entry[key];
      if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) throw new Error(`${where}.${key} is missing or out of range`);
      return value;
    };
    const icao = text("icao", /^[A-Z0-9]{4}$/);
    if (seen.has(icao)) throw new Error(`${where}: ${icao} is listed twice`);
    seen.add(icao);
    const tz = text("tz");
    if (!knownZone(tz)) throw new Error(`${where}.tz ${tz} is not a zone this engine knows`);
    const localized = (field: "names" | "cityNames"): Airport["names"] => {
      const out: Airport["names"] = {};
      const raw = (entry[field] ?? {}) as Record<string, unknown>;
      for (const lang of NAME_LANGS) {
        const value = raw[lang];
        if (value === undefined || value === null || value === "") continue;
        if (typeof value !== "string") throw new Error(`${where}.${field}.${lang} must be a string`);
        out[lang] = value;
      }
      return out;
    };
    return {
      icao,
      iata: entry.iata === null || entry.iata === undefined ? null : text("iata", /^[A-Z0-9]{3}$/),
      name: text("name"),
      city: text("city"),
      country: text("country", /^[A-Z]{2}$/),
      lat: number("lat", -90, 90),
      lon: number("lon", -180, 180),
      elev: entry.elev === null || entry.elev === undefined ? null : number("elev", -500, 9000),
      tz,
      wikidata: entry.wikidata === null || entry.wikidata === undefined ? null : text("wikidata", /^Q\d+$/),
      names: localized("names"),
      cityNames: localized("cityNames"),
    };
  });
  return { schemaVersion: 1, updated: data.updated, airports };
}

export function loadAirports(path: string): AirportList {
  return validateAirports(JSON.parse(readFileSync(path, "utf8")));
}

const AIRPORT_PAGES_MODULE = "virtual:airport-pages";

/** Vite plugin: `virtual:airport-pages` hands the shell the ids of the
 * airports with a page (the airport card links them), from the same list
 * the pages are built from, without bundling the list itself. */
export function airportPageIds(listPath: string): Plugin {
  const resolved = `\0${AIRPORT_PAGES_MODULE}`;
  return {
    name: "xue:airport-pages",
    resolveId: (id) => (id === AIRPORT_PAGES_MODULE ? resolved : null),
    load(id) {
      if (id !== resolved) return null;
      this.addWatchFile(listPath);
      const ids = loadAirports(listPath).airports.map((airport) => airport.icao);
      return `export const AIRPORT_PAGE_ICAOS = ${JSON.stringify(ids)};\n`;
    },
  };
}

// --- words ---------------------------------------------------------------------------

interface Words {
  home: string;
  index: string;
  indexIntro: (airports: number, countries: number) => string;
  /** The h1: the name with the language's word for weather. */
  heading: (name: string) => string;
  title: (name: string, codes: string) => string;
  description: (name: string, codes: string, place: string) => string;
  facts: { icao: string; iata: string; city: string; country: string; position: string; elevation: string; zone: string };
  shows: (zone: string) => string;
  noscript: string;
  map: string;
  compare: string;
  nearby: string;
  /** The model forecast's heading, and what it shows: `models` names the
   * runs this airport reads, as a list in the page language. */
  models: string;
  modelsIntro: (models: string) => string;
  languages: string;
  live: LiveWords;
}

export const WORDS: Record<PageLang, Words> = {
  en: {
    home: "Xue",
    index: "Airport weather",
    indexIntro: (airports, countries) =>
      `Live METAR and TAF for ${airports} airports in ${countries} countries and regions: the latest report decoded, the last 24 hours of observations, today's high and low and the current forecast.`,
    heading: (name) => `${name} weather`,
    title: (name, codes) => `${name} (${codes}) weather: METAR, TAF and observations`,
    description: (name, codes, place) =>
      `Latest METAR and TAF for ${name} (${codes}), ${place}: current conditions decoded, the last 24 hours of observations, today's high and low and the aerodrome forecast. Updated about every 30 minutes.`,
    facts: { icao: "ICAO", iata: "IATA", city: "City", country: "Country or region", position: "Position", elevation: "Elevation", zone: "Time zone" },
    shows: (zone) =>
      `This page reads the airport's reports as they are published: the latest METAR (the routine observation, about every 30 minutes) raw and decoded, today's observed high and low on the local clock, the last 24 hours of observations and the current TAF (the aerodrome forecast, issued about every 6 hours). Times are local (${zone}).`,
    noscript: "The live reports need JavaScript; the live map shows them too.",
    map: "Open the live map at this airport",
    compare: "Compare the forecast models here",
    nearby: "Nearby airports",
    models: "Model forecast",
    modelsIntro: (models) => `The next five days from ${models}, the latest run of each read at the model grid cell nearest the airport, with the airport's own reports of the past day as dots. Times are local.`,
    languages: "Languages",
    live: {
      loading: "Reading the latest reports…",
      unavailable: "The live reports are not available right now.",
      latest: "Latest observation",
      minutesAgo: "{n} min ago",
      hoursAgo: "{n} h ago",
      temperature: "Temperature",
      dewPoint: "Dew point",
      wind: "Wind",
      visibility: "Visibility",
      pressure: "Pressure",
      clouds: "Clouds",
      weather: "Weather",
      category: "Flight category",
      today: "Today so far",
      todayNote: "Highest and lowest of {n} reports on {since}, local time.",
      high: "High",
      low: "Low",
      history: "Last 24 hours",
      time: "Time",
      forecast: "Aerodrome forecast (TAF)",
      forecastValid: "Valid {from} – {to}",
      noForecast: "No current TAF for this airport.",
      prevailing: "Initial",
      modelsLoading: "Reading the model forecast…",
      modelsUnavailable: "The model forecast is not available right now.",
      precipitation: "Precipitation rate",
      observed: "Observed (METAR)",
      now: "now",
    },
  },
  zh: {
    home: "Xue",
    index: "机场天气",
    indexIntro: (airports, countries) => `${countries} 个国家和地区的 ${airports} 个机场的实时 METAR 与 TAF：最新报文解读、过去 24 小时观测、今日最高最低气温和当前预报。`,
    heading: (name) => `${name}天气`,
    title: (name, codes) => `${name}（${codes}）天气：METAR、TAF 与实况`,
    description: (name, codes, place) => `${name}（${codes}，${place}）的最新 METAR 与 TAF：当前天气解读、过去 24 小时观测、今日最高最低气温和机场预报，约每 30 分钟更新。`,
    facts: { icao: "ICAO", iata: "IATA", city: "城市", country: "国家或地区", position: "位置", elevation: "海拔", zone: "时区" },
    shows: (zone) =>
      `本页按发布读取机场的报文：最新的 METAR（例行观测，约每 30 分钟一次）原文与解读、当地今日已观测的最高和最低气温、过去 24 小时的观测，以及当前的 TAF（机场预报，约每 6 小时发布一次）。时间为当地时间（${zone}）。`,
    noscript: "实时报文需要 JavaScript；实时地图上也能查看。",
    map: "在实时地图上打开这个机场",
    compare: "在这里对比各预报模式",
    nearby: "附近机场",
    models: "模式预报",
    modelsIntro: (models) => `${models}未来五天的预报（各取最新一次运行，读离机场最近的模式格点），圆点是机场过去一天的实测报文。时间为当地时间。`,
    languages: "语言",
    live: {
      loading: "正在读取最新报文…",
      unavailable: "实时报文暂时无法读取。",
      latest: "最新观测",
      minutesAgo: "{n} 分钟前",
      hoursAgo: "{n} 小时前",
      temperature: "气温",
      dewPoint: "露点",
      wind: "风",
      visibility: "能见度",
      pressure: "气压",
      clouds: "云",
      weather: "天气现象",
      category: "飞行条件",
      today: "今日至今",
      todayNote: "当地 {since} 的 {n} 份报文中的最高和最低值。",
      high: "最高",
      low: "最低",
      history: "过去 24 小时",
      time: "时间",
      forecast: "机场预报（TAF）",
      forecastValid: "有效期 {from} – {to}",
      noForecast: "这个机场目前没有 TAF。",
      prevailing: "起始",
      modelsLoading: "正在读取模式预报…",
      modelsUnavailable: "模式预报暂时无法读取。",
      precipitation: "降水率",
      observed: "实测（METAR）",
      now: "现在",
    },
  },
  "zh-Hant": {
    home: "Xue",
    index: "機場天氣",
    indexIntro: (airports, countries) => `${countries} 個國家和地區的 ${airports} 個機場的即時 METAR 與 TAF：最新報文解讀、過去 24 小時觀測、今日最高最低氣溫和當前預報。`,
    heading: (name) => `${name}天氣`,
    title: (name, codes) => `${name}（${codes}）天氣：METAR、TAF 與實況`,
    description: (name, codes, place) => `${name}（${codes}，${place}）的最新 METAR 與 TAF：當前天氣解讀、過去 24 小時觀測、今日最高最低氣溫和機場預報，約每 30 分鐘更新。`,
    facts: { icao: "ICAO", iata: "IATA", city: "城市", country: "國家或地區", position: "位置", elevation: "海拔", zone: "時區" },
    shows: (zone) =>
      `本頁按發布讀取機場的報文：最新的 METAR（例行觀測，約每 30 分鐘一次）原文與解讀、當地今日已觀測的最高和最低氣溫、過去 24 小時的觀測，以及當前的 TAF（機場預報，約每 6 小時發布一次）。時間為當地時間（${zone}）。`,
    noscript: "即時報文需要 JavaScript；即時地圖上也能查看。",
    map: "在即時地圖上開啟這個機場",
    compare: "在這裡比較各預報模式",
    nearby: "附近機場",
    models: "模式預報",
    modelsIntro: (models) => `${models}未來五天的預報（各取最新一次運行，讀離機場最近的模式格點），圓點是機場過去一天的實測報文。時間為當地時間。`,
    languages: "語言",
    live: {
      loading: "正在讀取最新報文…",
      unavailable: "即時報文暫時無法讀取。",
      latest: "最新觀測",
      minutesAgo: "{n} 分鐘前",
      hoursAgo: "{n} 小時前",
      temperature: "氣溫",
      dewPoint: "露點",
      wind: "風",
      visibility: "能見度",
      pressure: "氣壓",
      clouds: "雲",
      weather: "天氣現象",
      category: "飛行條件",
      today: "今日至今",
      todayNote: "當地 {since} 的 {n} 份報文中的最高和最低值。",
      high: "最高",
      low: "最低",
      history: "過去 24 小時",
      time: "時間",
      forecast: "機場預報（TAF）",
      forecastValid: "有效期 {from} – {to}",
      noForecast: "這個機場目前沒有 TAF。",
      prevailing: "起始",
      modelsLoading: "正在讀取模式預報…",
      modelsUnavailable: "模式預報暫時無法讀取。",
      precipitation: "降水率",
      observed: "實測（METAR）",
      now: "現在",
    },
  },
  ja: {
    home: "Xue",
    index: "空港の天気",
    indexIntro: (airports, countries) => `${countries} の国と地域、${airports} 空港の METAR と TAF をリアルタイムで：最新の通報の解読、過去 24 時間の観測、今日の最高・最低気温、現在の予報。`,
    heading: (name) => `${name}の天気`,
    title: (name, codes) => `${name}（${codes}）の天気：METAR・TAF・観測`,
    description: (name, codes, place) => `${name}（${codes}、${place}）の最新の METAR と TAF：現在の天気の解読、過去 24 時間の観測、今日の最高・最低気温、飛行場予報。約 30 分ごとに更新。`,
    facts: { icao: "ICAO", iata: "IATA", city: "都市", country: "国・地域", position: "位置", elevation: "標高", zone: "タイムゾーン" },
    shows: (zone) =>
      `このページは空港の通報を発表どおりに読み込みます：最新の METAR（定時観測、約 30 分ごと）の原文と解読、現地時間で今日観測された最高・最低気温、過去 24 時間の観測、現在の TAF（飛行場予報、約 6 時間ごとに発表）。時刻は現地時間（${zone}）です。`,
    noscript: "リアルタイムの通報には JavaScript が必要です。リアルタイム地図でも確認できます。",
    map: "この空港をリアルタイム地図で開く",
    compare: "この地点で予報モデルを比較する",
    nearby: "近くの空港",
    models: "数値予報モデル",
    modelsIntro: (models) => `${models}による今後 5 日間の予報（各モデルの最新の実行を、空港に最も近い格子点で読んだもの）と、過去 1 日の空港の観測（点）。時刻は現地時間です。`,
    languages: "言語",
    live: {
      loading: "最新の通報を読み込んでいます…",
      unavailable: "現在、リアルタイムの通報を読み込めません。",
      latest: "最新の観測",
      minutesAgo: "{n} 分前",
      hoursAgo: "{n} 時間前",
      temperature: "気温",
      dewPoint: "露点",
      wind: "風",
      visibility: "視程",
      pressure: "気圧",
      clouds: "雲",
      weather: "天気",
      category: "飛行条件",
      today: "今日これまで",
      todayNote: "現地 {since} の通報 {n} 件の最高と最低。",
      high: "最高",
      low: "最低",
      history: "過去 24 時間",
      time: "時刻",
      forecast: "飛行場予報（TAF）",
      forecastValid: "有効期間 {from} – {to}",
      noForecast: "この空港には現在 TAF がありません。",
      prevailing: "初期",
      modelsLoading: "モデル予報を読み込んでいます…",
      modelsUnavailable: "モデル予報は現在利用できません。",
      precipitation: "降水強度",
      observed: "観測（METAR）",
      now: "現在",
    },
  },
  ko: {
    home: "Xue",
    index: "공항 날씨",
    indexIntro: (airports, countries) => `${countries}개 국가·지역 ${airports}개 공항의 실시간 METAR와 TAF: 최신 보고 해독, 지난 24시간 관측, 오늘의 최고·최저 기온과 현재 예보.`,
    heading: (name) => `${name} 날씨`,
    title: (name, codes) => `${name}(${codes}) 날씨: METAR, TAF, 관측`,
    description: (name, codes, place) => `${name}(${codes}, ${place})의 최신 METAR와 TAF: 현재 날씨 해독, 지난 24시간 관측, 오늘의 최고·최저 기온과 비행장 예보. 약 30분마다 갱신.`,
    facts: { icao: "ICAO", iata: "IATA", city: "도시", country: "국가 또는 지역", position: "위치", elevation: "표고", zone: "시간대" },
    shows: (zone) =>
      `이 페이지는 공항의 보고를 발표되는 대로 읽습니다: 최신 METAR(정시 관측, 약 30분마다)의 원문과 해독, 현지 시각으로 오늘 관측된 최고·최저 기온, 지난 24시간의 관측, 현재 TAF(비행장 예보, 약 6시간마다 발표). 시각은 현지 시각(${zone})입니다.`,
    noscript: "실시간 보고에는 JavaScript가 필요합니다. 실시간 지도에서도 볼 수 있습니다.",
    map: "실시간 지도에서 이 공항 열기",
    compare: "이 지점에서 예보 모델 비교하기",
    nearby: "근처 공항",
    models: "수치 예보 모델",
    modelsIntro: (models) => `${models}의 향후 5일 예보(각 모델의 최신 실행을 공항에서 가장 가까운 격자점에서 읽은 값)와 지난 하루 공항 관측(점). 시간은 현지 시각입니다.`,
    languages: "언어",
    live: {
      loading: "최신 보고를 읽는 중…",
      unavailable: "지금은 실시간 보고를 읽을 수 없습니다.",
      latest: "최신 관측",
      minutesAgo: "{n}분 전",
      hoursAgo: "{n}시간 전",
      temperature: "기온",
      dewPoint: "이슬점",
      wind: "바람",
      visibility: "시정",
      pressure: "기압",
      clouds: "구름",
      weather: "일기",
      category: "비행 조건",
      today: "오늘 지금까지",
      todayNote: "현지 {since}의 보고 {n}건 중 최고와 최저.",
      high: "최고",
      low: "최저",
      history: "지난 24시간",
      time: "시각",
      forecast: "비행장 예보(TAF)",
      forecastValid: "유효 {from} – {to}",
      noForecast: "이 공항은 현재 TAF가 없습니다.",
      prevailing: "초기",
      modelsLoading: "모델 예보를 읽는 중…",
      modelsUnavailable: "모델 예보를 지금은 읽을 수 없습니다.",
      precipitation: "강수 강도",
      observed: "관측(METAR)",
      now: "현재",
    },
  },
};

// --- helpers -------------------------------------------------------------------------

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

/** JSON inside a `<script>`: `<` escaped so no string closes the element. */
function scriptJson(value: unknown): string {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

function spec(lang: PageLang) {
  return PAGE_LANGS.find((item) => item.code === lang)!;
}

/** The airport's name in a page language, English where the list has none. */
export function airportName(airport: Airport, lang: PageLang): string {
  return lang === "en" ? airport.name : (airport.names[lang] ?? airport.name);
}

/** The city the airport serves in the page's language, English when the
 * list has no name in it. */
export function localCity(airport: Airport, lang: PageLang): string {
  return lang === "en" ? airport.city : (airport.cityNames[lang] ?? airport.city);
}

export function countryName(code: string, lang: PageLang): string {
  try {
    return new Intl.DisplayNames([spec(lang).htmlLang], { type: "region" }).of(code) ?? code;
  } catch {
    return code;
  }
}

function codes(airport: Airport): string {
  return airport.iata ? `${airport.icao} / ${airport.iata}` : airport.icao;
}

function formatPosition(lat: number, lon: number): string {
  return `${Math.abs(lat).toFixed(3)}°${lat >= 0 ? "N" : "S"} ${Math.abs(lon).toFixed(3)}°${lon >= 0 ? "E" : "W"}`;
}

/** Great-circle distance, km. */
function distanceKm(a: Airport, b: Airport): number {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** The `count` listed airports nearest to one, nearest first. */
export function nearestAirports(airport: Airport, all: readonly Airport[], count = 4): { airport: Airport; km: number }[] {
  return all
    .filter((other) => other.icao !== airport.icao)
    .map((other) => ({ airport: other, km: distanceKm(airport, other) }))
    .sort((a, b) => a.km - b.km || a.airport.icao.localeCompare(b.airport.icao))
    .slice(0, count);
}

/** The live map at the airport with the airport marks on, and the point
 * comparison there. */
export function mapLink(airport: Airport, lang: PageLang): string {
  return localizedPath(lang, `/?stations=apt#map=10/${airport.lat}/${airport.lon}`);
}

export function compareLink(airport: Airport, lang: PageLang): string {
  return localizedPath(lang, `/compare?lat=${airport.lat.toFixed(2)}&lon=${airport.lon.toFixed(2)}`);
}

// --- the frame -----------------------------------------------------------------------

interface PageShell {
  lang: PageLang;
  paths: Record<PageLang, string>;
  title: string;
  description: string;
  crumbs: { name: string; path: string }[];
  /** Further JSON-LD objects beside the breadcrumb. */
  data: object[];
  body: string;
  /** The live block's module, root-relative, when the page has one. */
  script: string | null;
}

function frame(shell: PageShell): string {
  const own = spec(shell.lang);
  const url = SITE_ORIGIN + shell.paths[shell.lang];
  const words = WORDS[shell.lang];
  const alternates = PAGE_LANGS.map((item) => `<link rel="alternate" hreflang="${item.htmlLang}" href="${SITE_ORIGIN}${shell.paths[item.code]}">`).join("\n    ");
  const ogAlternates = PAGE_LANGS.filter((item) => item.code !== shell.lang)
    .map((item) => `<meta property="og:locale:alternate" content="${item.ogLocale}">`)
    .join("\n    ");
  const breadcrumb = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: shell.crumbs.map((crumb, index) => ({ "@type": "ListItem", position: index + 1, name: crumb.name, item: SITE_ORIGIN + crumb.path })),
  };
  const page = {
    "@context": "https://schema.org",
    "@type": "WebPage",
    "@id": url,
    url,
    name: shell.title,
    description: shell.description,
    inLanguage: own.htmlLang,
    isPartOf: { "@type": "WebSite", "@id": `${SITE_ORIGIN}/#website`, url: `${SITE_ORIGIN}/`, name: SITE_NAME },
  };
  const ld = [breadcrumb, page, ...shell.data].map((item) => `<script type="application/ld+json">${scriptJson(item)}</script>`).join("\n    ");
  const crumbs = shell.crumbs
    .map((crumb, index) => (index === shell.crumbs.length - 1 ? `<span>${escapeHtml(crumb.name)}</span>` : `<a href="${crumb.path}">${escapeHtml(crumb.name)}</a>`))
    .join(" › ");
  const switcher = PAGE_LANGS.map((item) =>
    item.code === shell.lang
      ? `<li aria-current="true">${item.endonym}</li>`
      : `<li><a href="${shell.paths[item.code]}" hreflang="${item.htmlLang}" lang="${item.htmlLang}" data-locale="${item.code}">${item.endonym}</a></li>`,
  ).join("");
  return `<!doctype html>
<html lang="${own.htmlLang}">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>${escapeHtml(shell.title)} · ${SITE_NAME}</title>
    <meta name="description" content="${escapeHtml(shell.description)}">
    <link rel="canonical" href="${url}">
    ${alternates}
    <link rel="alternate" hreflang="x-default" href="${SITE_ORIGIN}${shell.paths.en}">
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="${SITE_NAME}">
    <meta property="og:url" content="${url}">
    <meta property="og:title" content="${escapeHtml(shell.title)}">
    <meta property="og:description" content="${escapeHtml(shell.description)}">
    <meta property="og:image" content="${SITE_ORIGIN}/og-image.jpg">
    <meta property="og:locale" content="${own.ogLocale}">
    ${ogAlternates}
    <meta name="twitter:card" content="summary_large_image">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <link rel="stylesheet" href="${stylesheetHref(`/${AIRPORT_CSS_FILE}`, AIRPORT_CSS)}">
    ${ld}${shell.lang === "en" ? LANGUAGE_REDIRECT : ""}${shell.script ? `\n    <script type="module" src="${shell.script}"></script>` : ""}
  </head>
  <body>
    <main>
      <nav class="crumbs" aria-label="Breadcrumb">${crumbs}</nav>
      ${shell.body}
      ${siteNav(shell.lang)}
      <nav class="langs" aria-label="${words.languages}"><ul>${switcher}</ul></nav>
      <footer>OBSERVATIONS AND FORECASTS: THE WORLD'S METEOROLOGICAL SERVICES UNDER WMO ARRANGEMENTS, REDISTRIBUTED BY <a href="https://aviationweather.gov">NOAA/NWS AVIATION WEATHER CENTER</a> · AIRPORTS: <a href="https://ourairports.com/data/">OURAIRPORTS</a> · <a href="https://www.wikidata.org">WIKIDATA</a> · <a href="${localizedPath(shell.lang, "/")}">${SITE_NAME}</a></footer>
    </main>
    <script>
      document.querySelectorAll("[data-locale]").forEach(function (link) {
        link.addEventListener("click", function () {
          try { localStorage.setItem("xue-locale", link.getAttribute("data-locale")); } catch (error) {}
        });
      });
    </script>
  </body>
</html>
`;
}

function allPaths(path: (lang: PageLang) => string): Record<PageLang, string> {
  return Object.fromEntries(PAGE_LANGS.map((item) => [item.code, path(item.code)])) as Record<PageLang, string>;
}

// --- the pages -----------------------------------------------------------------------

export function renderAirportPage(airport: Airport, all: readonly Airport[], lang: PageLang, script: string | null): { path: string; html: string } {
  const words = WORDS[lang];
  const paths = allPaths((code) => airportPagePath(code, airport.icao));
  const name = airportName(airport, lang);
  const country = countryName(airport.country, lang);
  const place = `${localCity(airport, lang)}, ${country}`;
  const title = words.title(name, codes(airport));
  const description = words.description(name, codes(airport), place);
  const factRows = [
    [words.facts.icao, airport.icao],
    [words.facts.iata, airport.iata ?? "—"],
    [words.facts.city, localCity(airport, lang)],
    [words.facts.country, country],
    [words.facts.position, formatPosition(airport.lat, airport.lon)],
    [words.facts.elevation, airport.elev === null ? "—" : `${Math.round(airport.elev)} m · ${Math.round(airport.elev / 0.3048)} ft`],
    [words.facts.zone, airport.tz],
  ]
    .map(([label, value]) => `<div><dt>${escapeHtml(label!)}</dt><dd>${escapeHtml(value!)}</dd></div>`)
    .join("");
  const nearby = nearestAirports(airport, all)
    .map(
      ({ airport: other, km }) =>
        `<li><a href="${airportPagePath(lang, other.icao)}"><span class="code">${escapeHtml(codes(other))}</span> <span class="name">${escapeHtml(airportName(other, lang))}</span> <span class="meta">${Math.round(km)} km</span></a></li>`,
    )
    .join("");
  const config: LiveConfig = { icao: airport.icao, tz: airport.tz, lat: airport.lat, lon: airport.lon, lang: spec(lang).htmlLang, words: words.live };
  const modelNames = new Intl.ListFormat(spec(lang).htmlLang, { type: "conjunction" }).format(
    airportModels(airport.lon, airport.lat).map(airportModelLabel),
  );
  const body = `<header>
        <p class="eyebrow">${escapeHtml(place)}</p>
        <h1>${escapeHtml(words.heading(name))} <span class="issued">${escapeHtml(codes(airport))}</span></h1>
        <p class="lead">${escapeHtml(words.shows(airport.tz))}</p>
        <p class="links"><a class="open" href="${escapeHtml(mapLink(airport, lang))}">${escapeHtml(words.map)} →</a> <a class="open" href="${escapeHtml(compareLink(airport, lang))}">${escapeHtml(words.compare)} →</a></p>
      </header>
      <dl class="facts">${factRows}</dl>
      <section id="${LIVE_ELEMENT_ID}" aria-live="polite"><script type="application/json" id="${LIVE_CONFIG_ID}">${scriptJson(config)}</script><p class="note" data-status>${escapeHtml(words.live.loading)}</p><noscript><p class="note">${escapeHtml(words.noscript)}</p></noscript></section>
      <section id="${MODELS_ELEMENT_ID}"><h2>${escapeHtml(words.models)}</h2><p class="note">${escapeHtml(words.modelsIntro(modelNames))}</p><p class="note" data-status></p></section>
      <section><h2>${escapeHtml(words.nearby)}</h2><ul class="list">${nearby}</ul></section>`;
  const airportData = {
    "@context": "https://schema.org",
    "@type": "Airport",
    name,
    ...(name !== airport.name ? { alternateName: airport.name } : {}),
    ...(airport.iata ? { iataCode: airport.iata } : {}),
    icaoCode: airport.icao,
    geo: {
      "@type": "GeoCoordinates",
      latitude: airport.lat,
      longitude: airport.lon,
      ...(airport.elev === null ? {} : { elevation: airport.elev }),
    },
    address: { "@type": "PostalAddress", addressLocality: localCity(airport, lang), addressCountry: airport.country },
    ...(airport.wikidata ? { sameAs: `https://www.wikidata.org/wiki/${airport.wikidata}` } : {}),
  };
  return {
    path: paths[lang],
    html: frame({
      lang,
      paths,
      title,
      description,
      crumbs: [
        { name: words.home, path: localizedPath(lang, "/") },
        { name: words.index, path: airportIndexPath(lang) },
        { name, path: paths[lang] },
      ],
      data: [airportData],
      body,
      script: script === null ? null : `/${script}`,
    }),
  };
}

export function renderAirportIndex(all: readonly Airport[], lang: PageLang): { path: string; html: string } {
  const words = WORDS[lang];
  const paths = allPaths(airportIndexPath);
  const collator = new Intl.Collator(spec(lang).htmlLang);
  const byCountry = new Map<string, Airport[]>();
  for (const airport of all) byCountry.set(airport.country, [...(byCountry.get(airport.country) ?? []), airport]);
  const groups = [...byCountry]
    .map(([code, members]) => ({ code, name: countryName(code, lang), members }))
    .sort((a, b) => collator.compare(a.name, b.name))
    .map(({ code, name, members }) => {
      const items = members
        .map((airport) => ({ airport, name: airportName(airport, lang) }))
        .sort((a, b) => collator.compare(a.name, b.name))
        .map(
          ({ airport, name: local }) =>
            `<li><a href="${airportPagePath(lang, airport.icao)}"><span class="code">${escapeHtml(codes(airport))}</span> <span class="name">${escapeHtml(local)}</span> <span class="meta">${escapeHtml(localCity(airport, lang))}</span></a></li>`,
        )
        .join("");
      return `<section id="${code.toLowerCase()}"><h2>${escapeHtml(name)}</h2><ul class="list">${items}</ul></section>`;
    })
    .join("");
  const description = words.indexIntro(all.length, byCountry.size);
  return {
    path: paths[lang],
    html: frame({
      lang,
      paths,
      title: words.index,
      description,
      crumbs: [
        { name: words.home, path: localizedPath(lang, "/") },
        { name: words.index, path: paths[lang] },
      ],
      data: [],
      body: `<header><h1>${escapeHtml(words.index)}</h1><p class="lead">${escapeHtml(description)}</p></header>${groups}`,
      script: null,
    }),
  };
}

/** The pages' stylesheet: the storm pages' plus the live block's. */
export const AIRPORT_CSS_FILE = "airports/pages.css";
export const AIRPORT_CSS = `${PAGES_CSS}.links{display:flex;flex-wrap:wrap;gap:8px}
.list{list-style:none;margin:0;padding:0}.list a{display:flex;gap:10px;align-items:baseline;padding:8px 4px;border-bottom:1px solid var(--line);text-decoration:none}.list a:hover{background:var(--surface)}
.list .code,.list .meta{font:11px ui-monospace,monospace;color:var(--muted)}.list .name{font-weight:600}.list .meta{margin-left:auto;text-align:right}
.raw{margin:8px 0;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:var(--surface);font:13px/1.5 ui-monospace,SFMono-Regular,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.spark{display:block;width:100%;height:auto;margin:8px 0}.spark path{fill:none;stroke:var(--text);stroke-width:1.5}.spark text{font:10px ui-monospace,monospace;fill:var(--muted)}
.mlegend{display:flex;flex-wrap:wrap;gap:4px 14px;list-style:none;margin:8px 0;padding:0;font:11px ui-monospace,monospace;color:var(--muted)}.mlegend li{display:flex;align-items:center;gap:6px}.mlegend svg{width:28px;height:8px}
.mrow{margin:6px 0 0}.mrow figcaption{font-size:12px;font-weight:600}.mrow .unit{font:11px ui-monospace,monospace;color:var(--muted);font-weight:400}.mrow svg,.maxis{display:block;width:100%;height:auto}
.models .trace{fill:none;stroke:var(--text);stroke-width:1.3}.models .obs{fill:var(--text)}.models .day{stroke:var(--line)}.models .now{stroke:var(--muted);stroke-dasharray:1 2}.models text{font:10px ui-monospace,monospace;fill:var(--muted)}.maxis text{text-anchor:start}
.category{display:inline-block;padding:0 6px;border-radius:8px;color:#fff;background:#78808c;font:11px ui-monospace,monospace}.category[data-category=VFR]{background:#1f8a4c}.category[data-category=MVFR]{background:#1d61c4}.category[data-category=IFR]{background:#c62828}.category[data-category=LIFR]{background:#a52bab}
`;

// --- the site ------------------------------------------------------------------------

export interface AirportSite {
  files: { fileName: string; source: string }[];
  /** Absolute addresses, for the sitemap. */
  urls: string[];
  /** One day for every address, the later of the list's `updated` and
   * `AIRPORT_PAGES_REVISED`: the pages' text changes with the list and with
   * their own copy, whatever the live block shows. */
  lastmod: Record<string, string>;
}

/** The Vite entry that builds the live block (`vite.config.ts`). */
export const LIVE_ENTRY = "airport";

/** The live block's file in the bundle, or null when the build has no such
 * entry (the pages are whole without it). */
export function liveScript(bundle: Readonly<Record<string, { type: string; fileName: string; name?: string; isEntry?: boolean }>>): string | null {
  for (const output of Object.values(bundle)) if (output.type === "chunk" && output.isEntry && output.name === LIVE_ENTRY) return output.fileName;
  return null;
}

/** The day the pages' own copy last changed (a section, a heading, the
 * structured data), moved by hand with such a change; the sitemap dates a
 * page by the later of this and the list's `updated`. */
export const AIRPORT_PAGES_REVISED = "2026-11-28";

/** Every page in every language, and their stylesheet. */
export function buildAirportSite(list: AirportList, script: string | null): AirportSite {
  const files: AirportSite["files"] = [{ fileName: AIRPORT_CSS_FILE, source: AIRPORT_CSS }];
  const urls: string[] = [];
  const lastmod: Record<string, string> = {};
  const revised = list.updated > AIRPORT_PAGES_REVISED ? list.updated : AIRPORT_PAGES_REVISED;
  const add = (page: { path: string; html: string }) => {
    files.push({ fileName: pageFile(page.path), source: page.html });
    const url = SITE_ORIGIN + page.path;
    urls.push(url);
    lastmod[url] = revised;
  };
  const sorted = [...list.airports].sort((a, b) => a.icao.localeCompare(b.icao));
  for (const lang of PAGE_LANGS.map((item) => item.code)) {
    add(renderAirportIndex(sorted, lang));
    for (const airport of sorted) add(renderAirportPage(airport, sorted, lang, script));
  }
  return { files, urls, lastmod };
}
