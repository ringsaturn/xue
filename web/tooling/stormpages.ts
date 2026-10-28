/** The storm pages: one static HTML page per archived storm, per season and
 * for the archive as a whole, in five languages — what a search engine or an
 * agent reads without running the storm map (`typhoon.html`), and where a
 * search for a storm by its name lands.
 *
 * The layout follows the blog's (Hugo's): English at the root, the other
 * languages under a prefix — `/tc/2026/choi-wan-wp262026`,
 * `/zh/tc/2026/choi-wan-wp262026` — each page naming every translation with
 * `hreflang` (English as `x-default`) and the Open Graph locales. The English
 * page doubles as the language chooser: a reader whose site language (the
 * `xue-locale` the rest of the site keeps) or browser asks for one of the
 * others is sent there; a crawler stays.
 *
 * Everything is derived from the published archive (`docs/tc.md` §8–9) at
 * build time: the facts of a storm are its best track's, the table its
 * synoptic points, the verification its scores. Node-only: imported by
 * `discovery.ts`, never by a page. */

import { SITE_NAME, SITE_ORIGIN } from "../src/site";
import {
  type ArchiveCatalog,
  type BestRecord,
  type ErrorColumns,
  type SeasonIndex,
  type SeasonStorm,
  validateArchiveCatalog,
  validateBest,
  validateMonthIndex,
  validateScores,
  validateSeasonIndex,
} from "../src/tc/archive";
import { SSHS_CLASSES, sshsClass } from "../src/tc/intensity";
import { stormNameIn } from "../src/tc/namestable";
import type { TcPoint, TcTrack } from "../src/tc/schema";

// --- languages ---------------------------------------------------------------

export type PageLang = "en" | "zh" | "zh-Hant" | "ja" | "ko";

interface LangSpec {
  code: PageLang;
  /** The path prefix, empty for English at the root. */
  prefix: string;
  htmlLang: string;
  /** Open Graph wants language_TERRITORY. */
  ogLocale: string;
  endonym: string;
}

export const PAGE_LANGS: readonly LangSpec[] = [
  { code: "en", prefix: "", htmlLang: "en", ogLocale: "en_US", endonym: "English" },
  { code: "zh", prefix: "/zh", htmlLang: "zh-CN", ogLocale: "zh_CN", endonym: "简体中文" },
  { code: "zh-Hant", prefix: "/zh-hant", htmlLang: "zh-TW", ogLocale: "zh_TW", endonym: "繁體中文" },
  { code: "ja", prefix: "/ja", htmlLang: "ja", ogLocale: "ja_JP", endonym: "日本語" },
  { code: "ko", prefix: "/ko", htmlLang: "ko", ogLocale: "ko_KR", endonym: "한국어" },
];

const KNOT = 0.514444;
const LEADS = [24, 48, 72, 120] as const;

interface Words {
  storm: Record<"WP" | "hurricane" | "cyclone", string>;
  basins: Record<string, string>;
  archive: string;
  archiveIntro: string;
  season: (season: number) => string;
  seasonIntro: (season: number, storms: number) => string;
  home: string;
  title: (kind: string, local: string, issued: string, year: number) => string;
  /** The page's opening paragraph, from the facts. */
  summary: (facts: SummaryFacts) => string;
  facts: { id: string; basin: string; life: string; peak: string; pressure: string; forecasts: string; sources: string };
  open: string;
  track: string;
  trackNote: string;
  columns: { time: string; position: string; wind: string; pressure: string; grade: string };
  verification: string;
  verificationNote: string;
  source: string;
  noVerification: string;
  unnamed: string;
  days: (days: number) => string;
  storms: string;
  languages: string;
}

export interface SummaryFacts {
  kind: string;
  name: string;
  basin: string;
  first: string;
  last: string;
  days: number;
  peakKt: number | null;
  peakClass: string | null;
  pmin: number | null;
  forecasts: number;
  sources: string[];
  best48: { source: string; km: number } | null;
}

const date = (iso: string) => iso.slice(0, 10);

export const WORDS: Record<PageLang, Words> = {
  en: {
    storm: { WP: "Typhoon", hurricane: "Hurricane", cyclone: "Cyclone" },
    basins: { WP: "western North Pacific", EP: "eastern North Pacific", CP: "central North Pacific", AL: "North Atlantic", IO: "North Indian Ocean", SH: "Southern Hemisphere" },
    archive: "Past tropical cyclones",
    archiveIntro: "Every numbered tropical cyclone since 2023: the warning centres' best tracks, each archived forecast made for it, and how far each one missed.",
    season: (season) => `${season} tropical cyclone season`,
    seasonIntro: (season, storms) => `${storms} numbered storms of the ${season} season, by basin, with their best tracks and forecast verification.`,
    home: "Xue",
    title: (kind, local, _issued, year) => `${kind} ${local} (${year}): track, intensity and forecast verification`,
    summary: (f) =>
      `${f.kind} ${f.name} was tracked in the ${f.basin} from ${date(f.first)} to ${date(f.last)} (${f.days} days)` +
      (f.peakKt !== null ? `, peaking at ${f.peakKt} kt${f.peakClass ? ` (${f.peakClass})` : ""}` : "") +
      (f.pmin !== null ? ` with a minimum pressure of ${f.pmin} hPa` : "") +
      `. ${f.forecasts} archived forecasts from ${f.sources.join(", ") || "no source"} are verified against its best track` +
      (f.best48 ? `; at 48 hours the closest was ${f.best48.source}, ${f.best48.km} km off on average.` : "."),
    facts: { id: "ATCF ID", basin: "Basin", life: "Tracked", peak: "Peak wind", pressure: "Minimum pressure", forecasts: "Archived forecasts", sources: "Sources" },
    open: "Open the interactive storm map",
    track: "Best track",
    trackNote: "Six-hourly positions of the US best track (JTWC / NHC, one-minute winds); grades on the Saffir–Simpson scale.",
    columns: { time: "Time (UTC)", position: "Position", wind: "Wind (kt)", pressure: "Pressure (hPa)", grade: "Grade" },
    verification: "Forecast track error (km)",
    verificationNote: "Mean great-circle distance to the best track at each lead time, counted while the system was a tropical or subtropical cyclone; the number of cases is in brackets.",
    source: "Source",
    noVerification: "No forecast of this storm has been verified yet.",
    unnamed: "Unnamed",
    days: (days) => `${days} days`,
    storms: "Storms",
    languages: "Languages",
  },
  zh: {
    storm: { WP: "台风", hurricane: "飓风", cyclone: "气旋" },
    basins: { WP: "西北太平洋", EP: "东北太平洋", CP: "中北太平洋", AL: "北大西洋", IO: "北印度洋", SH: "南半球" },
    archive: "历史热带气旋",
    archiveIntro: "2023 年以来的每一个编号热带气旋：各预报中心的最佳路径、为它做过的每一次预报，以及每次偏了多远。",
    season: (season) => `${season} 年热带气旋季`,
    seasonIntro: (season, storms) => `${season} 年季的 ${storms} 个编号风暴，按海域列出，附最佳路径与预报检验。`,
    home: "Xue",
    title: (kind, local, issued, year) => `${kind}${local}（${issued}，${year}）路径、强度与预报检验`,
    summary: (f) =>
      `${f.kind}${f.name}活动于${f.basin}，自 ${date(f.first)} 至 ${date(f.last)}，共 ${f.days} 天` +
      (f.peakKt !== null ? `，最大风速 ${f.peakKt} 节${f.peakClass ? `（${f.peakClass}）` : ""}` : "") +
      (f.pmin !== null ? `，最低气压 ${f.pmin} 百帕` : "") +
      `。存档中来自 ${f.sources.join("、") || "—"} 的 ${f.forecasts} 次预报已与最佳路径比对` +
      (f.best48 ? `；48 小时预报中 ${f.best48.source} 最接近，平均偏差 ${f.best48.km} 公里。` : "。"),
    facts: { id: "ATCF 编号", basin: "海域", life: "活动时间", peak: "最大风速", pressure: "最低气压", forecasts: "存档预报", sources: "来源" },
    open: "打开交互式台风地图",
    track: "最佳路径",
    trackNote: "美国最佳路径（JTWC / NHC，1 分钟平均风速）每 6 小时的位置；等级按萨菲尔–辛普森分级。",
    columns: { time: "时间（UTC）", position: "位置", wind: "风速（节）", pressure: "气压（百帕）", grade: "等级" },
    verification: "预报路径误差（公里）",
    verificationNote: "各预报时效上与最佳路径的平均大圆距离，只计系统处于热带或副热带气旋阶段的时次；括号内为样本数。",
    source: "来源",
    noVerification: "这个风暴的预报尚未完成检验。",
    unnamed: "未命名",
    days: (days) => `${days} 天`,
    storms: "风暴",
    languages: "语言",
  },
  "zh-Hant": {
    storm: { WP: "颱風", hurricane: "颶風", cyclone: "氣旋" },
    basins: { WP: "西北太平洋", EP: "東北太平洋", CP: "中北太平洋", AL: "北大西洋", IO: "北印度洋", SH: "南半球" },
    archive: "歷史熱帶氣旋",
    archiveIntro: "2023 年以來的每一個編號熱帶氣旋：各預報中心的最佳路徑、為它做過的每一次預報，以及每次偏了多遠。",
    season: (season) => `${season} 年熱帶氣旋季`,
    seasonIntro: (season, storms) => `${season} 年季的 ${storms} 個編號風暴，按海域列出，附最佳路徑與預報檢驗。`,
    home: "Xue",
    title: (kind, local, issued, year) => `${kind}${local}（${issued}，${year}）路徑、強度與預報檢驗`,
    summary: (f) =>
      `${f.kind}${f.name}活動於${f.basin}，自 ${date(f.first)} 至 ${date(f.last)}，共 ${f.days} 天` +
      (f.peakKt !== null ? `，最大風速 ${f.peakKt} 節${f.peakClass ? `（${f.peakClass}）` : ""}` : "") +
      (f.pmin !== null ? `，最低氣壓 ${f.pmin} 百帕` : "") +
      `。存檔中來自 ${f.sources.join("、") || "—"} 的 ${f.forecasts} 次預報已與最佳路徑比對` +
      (f.best48 ? `；48 小時預報中 ${f.best48.source} 最接近，平均偏差 ${f.best48.km} 公里。` : "。"),
    facts: { id: "ATCF 編號", basin: "海域", life: "活動時間", peak: "最大風速", pressure: "最低氣壓", forecasts: "存檔預報", sources: "來源" },
    open: "開啟互動式颱風地圖",
    track: "最佳路徑",
    trackNote: "美國最佳路徑（JTWC / NHC，1 分鐘平均風速）每 6 小時的位置；等級按薩菲爾–辛普森分級。",
    columns: { time: "時間（UTC）", position: "位置", wind: "風速（節）", pressure: "氣壓（百帕）", grade: "等級" },
    verification: "預報路徑誤差（公里）",
    verificationNote: "各預報時效上與最佳路徑的平均大圓距離，只計系統處於熱帶或副熱帶氣旋階段的時次；括號內為樣本數。",
    source: "來源",
    noVerification: "這個風暴的預報尚未完成檢驗。",
    unnamed: "未命名",
    days: (days) => `${days} 天`,
    storms: "風暴",
    languages: "語言",
  },
  ja: {
    storm: { WP: "台風", hurricane: "ハリケーン", cyclone: "サイクロン" },
    basins: { WP: "北西太平洋", EP: "北東太平洋", CP: "北中部太平洋", AL: "北大西洋", IO: "北インド洋", SH: "南半球" },
    archive: "過去の熱帯低気圧",
    archiveIntro: "2023年以降の番号付きの熱帯低気圧すべて：各予報センターのベストトラック、その嵐に出された予報、それぞれのずれ。",
    season: (season) => `${season}年の熱帯低気圧シーズン`,
    seasonIntro: (season, storms) => `${season}年シーズンの番号付きの嵐 ${storms} 個を海域別に、ベストトラックと予報検証とともに掲載。`,
    home: "Xue",
    title: (kind, local, issued, year) => `${kind}${local}（${issued}、${year}年）の進路・強度・予報検証`,
    summary: (f) =>
      `${f.kind}${f.name}は${f.basin}で ${date(f.first)} から ${date(f.last)} まで（${f.days}日間）追跡され` +
      (f.peakKt !== null ? `、最大風速は ${f.peakKt} ノット${f.peakClass ? `（${f.peakClass}）` : ""}` : "") +
      (f.pmin !== null ? `、最低気圧は ${f.pmin} hPa` : "") +
      `に達しました。${f.sources.join("・") || "—"} によるアーカイブ済み予報 ${f.forecasts} 件をベストトラックと照合しています` +
      (f.best48 ? `。48時間予報で最も近かったのは ${f.best48.source} で、平均のずれは ${f.best48.km} km でした。` : "。"),
    facts: { id: "ATCF 番号", basin: "海域", life: "追跡期間", peak: "最大風速", pressure: "最低気圧", forecasts: "アーカイブ予報", sources: "予報元" },
    open: "インタラクティブな台風マップを開く",
    track: "ベストトラック",
    trackNote: "米国ベストトラック（JTWC / NHC、1分間平均風速）の6時間ごとの位置。階級はサファ・シンプソン・スケール。",
    columns: { time: "時刻（UTC）", position: "位置", wind: "風速（ノット）", pressure: "気圧（hPa）", grade: "階級" },
    verification: "予報進路誤差（km）",
    verificationNote: "各予報時間におけるベストトラックとの平均大円距離。熱帯または亜熱帯低気圧である間のみを数えます。括弧内は事例数。",
    source: "予報元",
    noVerification: "この嵐の予報はまだ検証されていません。",
    unnamed: "名前なし",
    days: (days) => `${days}日間`,
    storms: "嵐",
    languages: "言語",
  },
  ko: {
    storm: { WP: "태풍", hurricane: "허리케인", cyclone: "사이클론" },
    basins: { WP: "북서태평양", EP: "북동태평양", CP: "북중태평양", AL: "북대서양", IO: "북인도양", SH: "남반구" },
    archive: "지난 열대저기압",
    archiveIntro: "2023년 이후 번호가 붙은 모든 열대저기압: 각 예보 센터의 베스트 트랙, 그 폭풍에 대해 나온 모든 예보, 그리고 각각 얼마나 빗나갔는지.",
    season: (season) => `${season}년 열대저기압 시즌`,
    seasonIntro: (season, storms) => `${season}년 시즌의 번호가 붙은 폭풍 ${storms}개를 해역별로, 베스트 트랙과 예보 검증과 함께.`,
    home: "Xue",
    title: (kind, local, issued, year) => `${kind} ${local}(${issued}, ${year}) 진로·강도·예보 검증`,
    summary: (f) =>
      `${f.kind} ${f.name}은(는) ${f.basin}에서 ${date(f.first)}부터 ${date(f.last)}까지(${f.days}일) 추적되었으며` +
      (f.peakKt !== null ? ` 최대 풍속은 ${f.peakKt}노트${f.peakClass ? `(${f.peakClass})` : ""}` : "") +
      (f.pmin !== null ? `, 최저 기압은 ${f.pmin} hPa` : "") +
      `였습니다. ${f.sources.join(", ") || "—"}의 예보 ${f.forecasts}건을 베스트 트랙과 대조했습니다` +
      (f.best48 ? `. 48시간 예보에서 가장 가까웠던 것은 ${f.best48.source}로 평균 ${f.best48.km} km 빗나갔습니다.` : "."),
    facts: { id: "ATCF 번호", basin: "해역", life: "추적 기간", peak: "최대 풍속", pressure: "최저 기압", forecasts: "보관된 예보", sources: "출처" },
    open: "인터랙티브 태풍 지도 열기",
    track: "베스트 트랙",
    trackNote: "미국 베스트 트랙(JTWC / NHC, 1분 평균풍)의 6시간 간격 위치. 등급은 사피어-심프슨 척도.",
    columns: { time: "시각(UTC)", position: "위치", wind: "풍속(노트)", pressure: "기압(hPa)", grade: "등급" },
    verification: "예보 진로 오차(km)",
    verificationNote: "각 예보 시간에서 베스트 트랙까지의 평균 대원 거리로, 열대 또는 아열대 저기압이었던 동안만 셉니다. 괄호 안은 사례 수.",
    source: "출처",
    noVerification: "이 폭풍의 예보는 아직 검증되지 않았습니다.",
    unnamed: "이름 없음",
    days: (days) => `${days}일`,
    storms: "폭풍",
    languages: "언어",
  },
};

/** The source codes the pages print, as the storm map does. */
const SOURCE_CODES: Record<string, string> = {
  nhc: "NHC",
  jtwc: "JTWC",
  gfs: "GFS",
  gefs: "GEFS",
  ecmwf: "ECMWF",
  ecmwfens: "ECMWF ENS",
  gdm: "Google DeepMind",
};
const SOURCE_ORDER = ["nhc", "jtwc", "ecmwf", "ecmwfens", "gfs", "gefs", "gdm"];
const BASIN_ORDER = ["WP", "EP", "AL", "CP", "IO", "SH"];

// --- naming and addressing --------------------------------------------------------

/** `choi-wan-wp262026`: the name (a reused one, so the ATCF id after it). */
export function stormSlug(storm: { id: string; name: string | null }): string {
  const name = (storm.name ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `${name ? `${name}-` : ""}${storm.id.toLowerCase()}`;
}

/** The root-relative address of a page, as Pages serves it (no `.html`). */
export function pagePath(lang: PageLang, kind: "archive" | "season" | "storm", season?: number, slug?: string): string {
  const prefix = PAGE_LANGS.find((item) => item.code === lang)!.prefix;
  if (kind === "archive") return `${prefix}/tc/`;
  if (kind === "season") return `${prefix}/tc/${season}/`;
  return `${prefix}/tc/${season}/${slug}`;
}

/** The file a page is written to under the build's output. */
export function pageFile(path: string): string {
  const relative = path.replace(/^\//, "");
  return relative.endsWith("/") ? `${relative}index.html` : `${relative}.html`;
}

/** "Typhoon Ragasa", "台风桦加沙": the CJK scripts write the kind and the
 * name together, Korean and English with a space. */
function kindAndName(lang: PageLang, kind: string, name: string): string {
  return lang === "zh" || lang === "zh-Hant" || lang === "ja" ? `${kind}${name}` : `${kind} ${name}`;
}

function stormKind(basin: string, words: Words): string {
  if (basin === "WP") return words.storm.WP;
  return basin === "IO" || basin === "SH" ? words.storm.cyclone : words.storm.hurricane;
}

function localName(storm: { name: string | null; basin: string; id: string }, lang: PageLang, words: Words): string {
  const local = stormNameIn(storm.name, storm.basin, lang);
  if (!local) return `${words.unnamed} ${storm.id.slice(0, 4)}`;
  return lang === "en" ? titleCase(local) : local;
}

function titleCase(name: string): string {
  return name.toLowerCase().replace(/(^|[-\s])([a-z])/g, (_, separator: string, letter: string) => separator + letter.toUpperCase());
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

// --- the facts of a storm ---------------------------------------------------------

/** The US best track (else the first with points) at its synoptic times. */
export function synopticTrack(best: BestRecord | null): TcPoint[] {
  const keys = Object.keys(best?.best ?? {}).sort((a, b) => (a === "usa" ? -1 : b === "usa" ? 1 : a.localeCompare(b)));
  for (const key of keys) {
    const points = (best!.best[key] as TcTrack).points.filter((point) => /T(00|06|12|18):00/.test(point.time));
    if (points.length) return points;
  }
  return [];
}

/** Mean track error by source and lead against one truth (`usa` first). */
export function verification(truths: Record<string, Record<string, ErrorColumns>> | null): Map<string, Map<number, { km: number; n: number }>> {
  const result = new Map<string, Map<number, { km: number; n: number }>>();
  const truth = truths?.usa ?? Object.values(truths ?? {})[0];
  for (const [source, columns] of Object.entries(truth ?? {})) {
    const sums = new Map<number, { total: number; n: number }>();
    columns.lead.forEach((lead, row) => {
      if (!(LEADS as readonly number[]).includes(lead)) return;
      const sum = sums.get(lead) ?? { total: 0, n: 0 };
      sum.total += columns.track[row]!;
      sum.n += 1;
      sums.set(lead, sum);
    });
    if (sums.size) result.set(source, new Map([...sums].map(([lead, sum]) => [lead, { km: Math.round(sum.total / sum.n), n: sum.n }])));
  }
  return result;
}

export interface StormData {
  season: number;
  entry: SeasonStorm;
  best: BestRecord | null;
  forecasts: number;
  errors: Record<string, Record<string, ErrorColumns>> | null;
}

export function summaryFacts(storm: StormData, lang: PageLang): SummaryFacts {
  const words = WORDS[lang];
  const track = synopticTrack(storm.best);
  const winds = track.map((point) => point.vmax).filter((value): value is number => value !== null);
  const pressures = track.map((point) => point.pmin).filter((value): value is number => value !== null);
  const peak = winds.length ? Math.max(...winds) : storm.entry.vmax;
  const first = storm.entry.first ?? track[0]?.time ?? "";
  const last = storm.entry.last ?? track.at(-1)?.time ?? "";
  const scores = verification(storm.errors);
  let best48: SummaryFacts["best48"] = null;
  for (const [source, leads] of scores) {
    const cell = leads.get(48);
    if (cell && cell.n >= 3 && (!best48 || cell.km < best48.km)) best48 = { source: SOURCE_CODES[source] ?? source, km: cell.km };
  }
  return {
    kind: stormKind(storm.entry.basin, words),
    name: localName(storm.entry, lang, words),
    basin: words.basins[storm.entry.basin] ?? storm.entry.basin,
    first,
    last,
    days: first && last ? Math.max(1, Math.round((Date.parse(last) - Date.parse(first)) / 86400000)) : 0,
    peakKt: peak === null || peak === undefined ? null : Math.round(peak / KNOT),
    peakClass: peak === null || peak === undefined ? null : (sshsClass(peak)?.code ?? null),
    pmin: pressures.length ? Math.round(Math.min(...pressures)) : null,
    forecasts: storm.forecasts,
    sources: SOURCE_ORDER.filter((source) => storm.entry.sources.includes(source)).map((source) => SOURCE_CODES[source]!),
    best48,
  };
}

// --- the picture ---------------------------------------------------------------------

/** The track as an inline SVG: segments in their Saffir–Simpson colors on a
 * one-degree graticule, degrees of longitude shortened by the latitude so
 * the shape is the storm's. */
export function trackSvg(track: readonly TcPoint[], title: string): string {
  if (track.length < 2) return "";
  const anchor = track[0]!.lon;
  const xs = track.map((point) => anchor + ((((point.lon - anchor + 540) % 360) + 360) % 360) - 180);
  const ys = track.map((point) => point.lat);
  const midLat = (Math.max(...ys) + Math.min(...ys)) / 2;
  const squash = Math.cos((midLat * Math.PI) / 180);
  const pad = 2;
  const west = Math.min(...xs) - pad;
  const east = Math.max(...xs) + pad;
  const south = Math.min(...ys) - pad;
  const north = Math.max(...ys) + pad;
  const width = 640;
  const scale = width / ((east - west) * squash);
  const height = Math.min(640, Math.max(240, Math.round((north - south) * scale)));
  const yScale = height / (north - south);
  const x = (lon: number) => ((lon - west) * squash * scale).toFixed(1);
  const y = (lat: number) => ((north - lat) * yScale).toFixed(1);
  const lines: string[] = [];
  const step = (east - west) > 40 ? 10 : 5;
  for (let lon = Math.ceil(west / step) * step; lon <= east; lon += step)
    lines.push(`<line x1="${x(lon)}" y1="0" x2="${x(lon)}" y2="${height}"/>`);
  for (let lat = Math.ceil(south / step) * step; lat <= north; lat += step)
    lines.push(`<line x1="0" y1="${y(lat)}" x2="${width}" y2="${y(lat)}"/>`);
  const segments = track.slice(0, -1).map((point, index) => {
    const color = sshsClass(point.vmax)?.color ?? "#9a9a9a";
    return `<line x1="${x(xs[index]!)}" y1="${y(ys[index]!)}" x2="${x(xs[index + 1]!)}" y2="${y(ys[index + 1]!)}" stroke="${color}"/>`;
  });
  const dots = track.map((point, index) => `<circle cx="${x(xs[index]!)}" cy="${y(ys[index]!)}" r="2.2" fill="${sshsClass(point.vmax)?.color ?? "#9a9a9a"}"/>`);
  const legend = SSHS_CLASSES.map(
    (item, index) => `<g transform="translate(${8 + index * 44},${height - 14})"><rect width="14" height="6" rx="3" fill="${item.color}"/><text x="18" y="6">${item.code}</text></g>`,
  );
  return `<svg class="tc-track" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(title)}"><g class="grid">${lines.join("")}</g><g class="path" stroke-width="3" stroke-linecap="round">${segments.join("")}</g><g>${dots.join("")}</g><g class="legend">${legend.join("")}</g></svg>`;
}

// --- the pages -----------------------------------------------------------------------

interface PageShell {
  lang: PageLang;
  /** The page's own address in each language, root-relative. */
  paths: Record<PageLang, string>;
  title: string;
  description: string;
  crumbs: { name: string; path: string }[];
  body: string;
}

function head(shell: PageShell): string {
  const spec = PAGE_LANGS.find((item) => item.code === shell.lang)!;
  const own = SITE_ORIGIN + shell.paths[shell.lang];
  const alternates = PAGE_LANGS.map(
    (item) => `<link rel="alternate" hreflang="${item.htmlLang}" href="${SITE_ORIGIN}${shell.paths[item.code]}">`,
  ).join("\n    ");
  const ogAlternates = PAGE_LANGS.filter((item) => item.code !== shell.lang)
    .map((item) => `<meta property="og:locale:alternate" content="${item.ogLocale}">`)
    .join("\n    ");
  const breadcrumb = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: shell.crumbs.map((crumb, index) => ({
      "@type": "ListItem",
      position: index + 1,
      name: crumb.name,
      item: SITE_ORIGIN + crumb.path,
    })),
  };
  const page = {
    "@context": "https://schema.org",
    "@type": "WebPage",
    "@id": own,
    url: own,
    name: shell.title,
    description: shell.description,
    inLanguage: spec.htmlLang,
    isPartOf: { "@type": "WebSite", "@id": `${SITE_ORIGIN}/#website`, url: `${SITE_ORIGIN}/`, name: SITE_NAME },
  };
  return `<!doctype html>
<html lang="${spec.htmlLang}">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>${escapeHtml(shell.title)} · ${SITE_NAME}</title>
    <meta name="description" content="${escapeHtml(shell.description)}">
    <link rel="canonical" href="${own}">
    ${alternates}
    <link rel="alternate" hreflang="x-default" href="${SITE_ORIGIN}${shell.paths.en}">
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="${SITE_NAME}">
    <meta property="og:url" content="${own}">
    <meta property="og:title" content="${escapeHtml(shell.title)}">
    <meta property="og:description" content="${escapeHtml(shell.description)}">
    <meta property="og:image" content="${SITE_ORIGIN}/og-image.jpg">
    <meta property="og:locale" content="${spec.ogLocale}">
    ${ogAlternates}
    <meta name="twitter:card" content="summary_large_image">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <link rel="stylesheet" href="/tc/pages.css">
    <script type="application/ld+json">${JSON.stringify(breadcrumb)}</script>
    <script type="application/ld+json">${JSON.stringify(page)}</script>${shell.lang === "en" ? LANGUAGE_REDIRECT : ""}
  </head>`;
}

/** On an English page only: send a reader whose site language, else
 * browser language, is one of the others to that version. A crawler (no
 * storage, an English or no language) stays; the switcher's links record
 * the reader's choice, so picking English is not undone. */
const LANGUAGE_REDIRECT = `
    <script>
      (function () {
        try {
          var prefixes = { zh: "/zh", "zh-Hant": "/zh-hant", ja: "/ja", ko: "/ko" };
          var chosen = localStorage.getItem("xue-locale");
          var wanted = chosen;
          if (!wanted) {
            var tag = ((navigator.languages && navigator.languages[0]) || navigator.language || "").toLowerCase();
            if (/^zh/.test(tag)) wanted = /hant|tw|hk|mo/.test(tag) ? "zh-Hant" : "zh";
            else if (/^ja/.test(tag)) wanted = "ja";
            else if (/^ko/.test(tag)) wanted = "ko";
          }
          if (wanted && prefixes[wanted]) location.replace(prefixes[wanted] + location.pathname + location.search + location.hash);
        } catch (error) {
          // No storage (a privacy mode): stay on the English page.
        }
      })();
    </script>`;

function frame(shell: PageShell): string {
  const words = WORDS[shell.lang];
  const crumbs = shell.crumbs
    .map((crumb, index) =>
      index === shell.crumbs.length - 1 ? `<span>${escapeHtml(crumb.name)}</span>` : `<a href="${crumb.path}">${escapeHtml(crumb.name)}</a>`,
    )
    .join(" › ");
  const switcher = PAGE_LANGS.map((item) =>
    item.code === shell.lang
      ? `<li aria-current="true">${item.endonym}</li>`
      : `<li><a href="${shell.paths[item.code]}" hreflang="${item.htmlLang}" lang="${item.htmlLang}" data-locale="${item.code}">${item.endonym}</a></li>`,
  ).join("");
  return `${head(shell)}
  <body>
    <main>
      <nav class="crumbs" aria-label="Breadcrumb">${crumbs}</nav>
      ${shell.body}
      <nav class="langs" aria-label="${words.languages}"><ul>${switcher}</ul></nav>
      <footer>BEST TRACKS <a href="https://www.ncei.noaa.gov/products/international-best-track-archive">IBTrACS</a> · FORECASTS NHC · JTWC · <a href="https://hurricanes.ral.ucar.edu">UCAR RAL</a> · CONTAINS MODIFIED <a href="https://www.ecmwf.int/en/forecasts/datasets/open-data">ECMWF OPEN DATA</a> (<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>) · GOOGLE DEEPMIND TRACKS: CONTAINS EXPERIMENTAL RESEARCH DATA PRODUCED BY © 2023-25 GOOGLE LLC (CC BY 4.0), FOR RESEARCH MODELLING ONLY · <a href="/">${SITE_NAME}</a></footer>
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

function allPaths(kind: "archive" | "season" | "storm", season?: number, slug?: string): Record<PageLang, string> {
  return Object.fromEntries(PAGE_LANGS.map((item) => [item.code, pagePath(item.code, kind, season, slug)])) as Record<PageLang, string>;
}

function formatPosition(point: TcPoint): string {
  const lat = `${Math.abs(point.lat).toFixed(1)}°${point.lat >= 0 ? "N" : "S"}`;
  const lon = `${Math.abs(point.lon).toFixed(1)}°${point.lon >= 0 ? "E" : "W"}`;
  return `${lat} ${lon}`;
}

export function renderStormPage(storm: StormData, lang: PageLang): { path: string; html: string } {
  const words = WORDS[lang];
  const facts = summaryFacts(storm, lang);
  const slug = stormSlug(storm.entry);
  const paths = allPaths("storm", storm.season, slug);
  const issued = storm.entry.name ? titleCase(storm.entry.name) : storm.entry.id.slice(0, 4);
  const title = words.title(facts.kind, facts.name, issued, storm.season);
  const summary = words.summary(facts);
  const track = synopticTrack(storm.best);
  const mapLink = `/typhoon?storm=${storm.entry.id}&lang=${encodeURIComponent(lang)}`;
  const factRows = [
    [words.facts.id, storm.entry.id],
    [words.facts.basin, facts.basin],
    [words.facts.life, facts.first ? `${date(facts.first)} → ${date(facts.last)} (${words.days(facts.days)})` : "—"],
    [words.facts.peak, facts.peakKt !== null ? `${facts.peakKt} kt · ${Math.round(facts.peakKt * KNOT)} m/s${facts.peakClass ? ` · ${facts.peakClass}` : ""}` : "—"],
    [words.facts.pressure, facts.pmin !== null ? `${facts.pmin} hPa` : "—"],
    [words.facts.forecasts, String(facts.forecasts)],
    [words.facts.sources, facts.sources.join(" · ") || "—"],
  ]
    .map(([label, value]) => `<div><dt>${escapeHtml(label!)}</dt><dd>${escapeHtml(value!)}</dd></div>`)
    .join("");
  const rows = track
    .map((point) => {
      const grade = sshsClass(point.vmax);
      return `<tr><td>${point.time.slice(0, 13).replace("T", " ")}Z</td><td>${formatPosition(point)}</td><td>${point.vmax === null ? "—" : Math.round(point.vmax / KNOT)}</td><td>${point.pmin === null ? "—" : Math.round(point.pmin)}</td><td>${grade ? `<span class="grade" style="--grade:${grade.color}">${grade.code}</span>` : "—"}</td></tr>`;
    })
    .join("");
  const scores = verification(storm.errors);
  const sources = SOURCE_ORDER.filter((source) => scores.has(source)).concat([...scores.keys()].filter((source) => !SOURCE_ORDER.includes(source)));
  const scoreTable = sources.length
    ? `<table><thead><tr><th>${words.source}</th>${LEADS.map((lead) => `<th>${lead} h</th>`).join("")}</tr></thead><tbody>${sources
        .map(
          (source) =>
            `<tr><th scope="row">${SOURCE_CODES[source] ?? source}</th>${LEADS.map((lead) => {
              const cell = scores.get(source)?.get(lead);
              return `<td>${cell ? `${cell.km} <small>(${cell.n})</small>` : "—"}</td>`;
            }).join("")}</tr>`,
        )
        .join("")}</tbody></table><p class="note">${words.verificationNote}</p>`
    : `<p class="note">${words.noVerification}</p>`;
  const body = `<header>
        <p class="eyebrow">${escapeHtml(words.season(storm.season))} · ${escapeHtml(facts.basin)}</p>
        <h1>${escapeHtml(kindAndName(lang, facts.kind, facts.name))} <span class="issued">${escapeHtml(issued)} · ${storm.entry.id}</span></h1>
        <p class="lead">${escapeHtml(summary)}</p>
        <p><a class="open" href="${mapLink}">${words.open} →</a></p>
      </header>
      <dl class="facts">${factRows}</dl>
      ${trackSvg(track, title)}
      <section><h2>${words.verification}</h2>${scoreTable}</section>
      <section><h2>${words.track}</h2><p class="note">${words.trackNote}</p><div class="scroll"><table><thead><tr><th>${words.columns.time}</th><th>${words.columns.position}</th><th>${words.columns.wind}</th><th>${words.columns.pressure}</th><th>${words.columns.grade}</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
  return {
    path: paths[lang],
    html: frame({
      lang,
      paths,
      title,
      description: summary,
      crumbs: [
        { name: words.home, path: "/" },
        { name: words.archive, path: pagePath(lang, "archive") },
        { name: words.season(storm.season), path: pagePath(lang, "season", storm.season) },
        { name: kindAndName(lang, facts.kind, facts.name), path: paths[lang] },
      ],
      body,
    }),
  };
}

export function renderSeasonPage(season: number, storms: readonly StormData[], lang: PageLang): { path: string; html: string } {
  const words = WORDS[lang];
  const paths = allPaths("season", season);
  const groups = BASIN_ORDER.map((basin) => {
    const members = storms
      .filter((storm) => storm.entry.basin === basin)
      .sort((a, b) => (a.entry.first ?? "").localeCompare(b.entry.first ?? "") || a.entry.id.localeCompare(b.entry.id));
    if (!members.length) return "";
    const items = members
      .map((storm) => {
        const facts = summaryFacts(storm, lang);
        const span = facts.first ? `${date(facts.first).slice(5)} → ${date(facts.last).slice(5)}` : "";
        const peak = facts.peakKt !== null ? `${facts.peakKt} kt${facts.peakClass ? ` ${facts.peakClass}` : ""}` : "";
        return `<li><a href="${pagePath(lang, "storm", season, stormSlug(storm.entry))}"><span class="code">${storm.entry.id.slice(0, 4)}</span> <span class="name">${escapeHtml(facts.name)}</span> <span class="meta">${span} · ${peak}</span></a></li>`;
      })
      .join("");
    return `<section><h2>${escapeHtml(words.basins[basin] ?? basin)}</h2><ul class="storms">${items}</ul></section>`;
  }).join("");
  const title = words.season(season);
  const description = words.seasonIntro(season, storms.length);
  return {
    path: paths[lang],
    html: frame({
      lang,
      paths,
      title,
      description,
      crumbs: [
        { name: words.home, path: "/" },
        { name: words.archive, path: pagePath(lang, "archive") },
        { name: title, path: paths[lang] },
      ],
      body: `<header><h1>${escapeHtml(title)}</h1><p class="lead">${escapeHtml(description)}</p><p><a class="open" href="/typhoon?lang=${lang}">${words.open} →</a></p></header>${groups}`,
    }),
  };
}

export function renderArchivePage(seasons: readonly { season: number; storms: number }[], lang: PageLang): { path: string; html: string } {
  const words = WORDS[lang];
  const paths = allPaths("archive");
  const items = [...seasons]
    .sort((a, b) => b.season - a.season)
    .map((item) => `<li><a href="${pagePath(lang, "season", item.season)}"><span class="name">${escapeHtml(words.season(item.season))}</span> <span class="meta">${item.storms} ${words.storms}</span></a></li>`)
    .join("");
  return {
    path: paths[lang],
    html: frame({
      lang,
      paths,
      title: words.archive,
      description: words.archiveIntro,
      crumbs: [
        { name: words.home, path: "/" },
        { name: words.archive, path: paths[lang] },
      ],
      body: `<header><h1>${escapeHtml(words.archive)}</h1><p class="lead">${escapeHtml(words.archiveIntro)}</p><p><a class="open" href="/typhoon?lang=${lang}">${words.open} →</a></p></header><ul class="storms">${items}</ul>`,
    }),
  };
}

/** The pages' one stylesheet: the site's paper and ink, system fonts. */
export const PAGES_CSS = `:root{color-scheme:light dark;--bg:#f3efe6;--text:#1b1a17;--muted:#6e6a60;--line:rgba(27,26,23,.16);--surface:rgba(251,249,244,.88)}
@media (prefers-color-scheme:dark){:root{--bg:#000;--text:#fff;--muted:rgba(255,255,255,.62);--line:rgba(255,255,255,.18);--surface:rgba(255,255,255,.06)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans","Noto Sans CJK SC","Malgun Gothic",sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 18px 48px}a{color:inherit}
.crumbs{font-size:12px;color:var(--muted)}.crumbs a{text-decoration:none}.crumbs a:hover{text-decoration:underline}
h1{margin:10px 0 8px;font:400 34px/1.15 "Instrument Serif",Georgia,"Songti SC","Hiragino Mincho ProN",serif}h1 .issued{font-size:18px;color:var(--muted);font-style:italic}
h2{margin:28px 0 8px;font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase}
.eyebrow,.note,small{color:var(--muted);font-size:12px}.lead{font-size:16px}
.open{display:inline-block;padding:6px 14px;border:1px solid var(--text);border-radius:16px;text-decoration:none;font-size:13px}.open:hover{background:var(--text);color:var(--bg)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px 18px;margin:18px 0}.facts div{border-top:1px solid var(--line);padding-top:6px}
.facts dt{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.facts dd{margin:2px 0 0;font-family:ui-monospace,SFMono-Regular,monospace;font-size:13px}
.tc-track{display:block;width:100%;height:auto;margin:12px 0;border:1px solid var(--line);border-radius:6px;background:var(--surface)}.tc-track .grid line{stroke:var(--line);stroke-width:1}.tc-track .legend text{font:9px ui-monospace,monospace;fill:var(--muted)}
table{width:100%;border-collapse:collapse;font:12px ui-monospace,SFMono-Regular,monospace;font-variant-numeric:tabular-nums}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}thead th{color:var(--muted);font-weight:500}
.scroll{overflow-x:auto}.grade{display:inline-block;padding:0 6px;border-radius:8px;background:var(--grade);color:#111}
.storms{list-style:none;margin:0;padding:0}.storms a{display:flex;gap:10px;align-items:baseline;padding:8px 4px;border-bottom:1px solid var(--line);text-decoration:none}.storms a:hover{background:var(--surface)}
.storms .code,.storms .meta{font:11px ui-monospace,monospace;color:var(--muted)}.storms .name{font-weight:600}.storms .meta{margin-left:auto}
.langs ul{display:flex;flex-wrap:wrap;gap:6px 14px;list-style:none;padding:0;margin:32px 0 0;font-size:13px}.langs li[aria-current]{font-weight:600}
footer{margin-top:28px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font:9px/1.7 ui-monospace,monospace;letter-spacing:.04em}footer a{color:var(--text);text-decoration:none}
`;

// --- reading the archive and putting the site together ----------------------------

async function getJson(url: string): Promise<unknown> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

async function getLines(url: string): Promise<unknown[]> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return (await response.text())
    .split("\n")
    .filter((line) => line.length > 0)
    .map((line) => JSON.parse(line) as unknown);
}

/** Every storm with a best track, read from the published archive under
 * `dataBaseUrl` (the data root, ending in `/`). */
export async function loadStorms(dataBaseUrl: string): Promise<{ catalog: ArchiveCatalog; storms: StormData[] }> {
  const root = new URL("tc/archive/", dataBaseUrl).href;
  const catalog = validateArchiveCatalog(await getJson(`${root}catalog.json?t=${Date.now()}`));
  const records = new Map<string, number>();
  await Promise.all(
    catalog.months.map(async (entry) => {
      const raw = await getJson(`${root}${entry.path}?v=${entry.crc32}`);
      validateMonthIndex(raw);
      // The count of records per storm is the month index's own field.
      for (const storm of (raw as { storms: { id: string; records: number }[] }).storms)
        records.set(storm.id, (records.get(storm.id) ?? 0) + storm.records);
    }),
  );
  const storms: StormData[] = [];
  for (const entry of catalog.seasons) {
    const index: SeasonIndex = validateSeasonIndex(await getJson(`${root}${entry.path}?v=${entry.crc32}`));
    const directory = `${root}${String(index.season).padStart(4, "0")}/`;
    const bestById = new Map<string, BestRecord>();
    for (const line of await getLines(`${directory}${index.best.path}?v=${index.best.crc32}`)) {
      const best = validateBest(line);
      bestById.set(best.storm, best);
    }
    const errorsById = new Map<string, Record<string, Record<string, ErrorColumns>>>();
    const scoresEntry = catalog.scores.find((item) => item.season === index.season);
    if (scoresEntry) {
      const scores = validateScores(await getJson(`${root}${scoresEntry.path}?v=${scoresEntry.crc32}`));
      for (const line of await getLines(`${directory}${scores.errors.path}?v=${scores.errors.crc32}`)) {
        const document = line as { storm: string; truths: Record<string, Record<string, ErrorColumns>> };
        errorsById.set(document.storm, document.truths);
      }
    }
    for (const storm of index.storms) {
      const best = bestById.get(storm.id) ?? null;
      if (!best) continue;
      storms.push({ season: index.season, entry: storm, best, forecasts: records.get(storm.id) ?? 0, errors: errorsById.get(storm.id) ?? null });
    }
  }
  return { catalog, storms };
}

export interface StormSite {
  files: { fileName: string; source: string }[];
  /** Absolute addresses, for the sitemap. */
  urls: string[];
}

/** Every page in every language, and the stylesheet they share. */
export function buildStormSite(storms: readonly StormData[]): StormSite {
  const files: StormSite["files"] = [{ fileName: "tc/pages.css", source: PAGES_CSS }];
  const urls: string[] = [];
  const add = (page: { path: string; html: string }) => {
    files.push({ fileName: pageFile(page.path), source: page.html });
    urls.push(SITE_ORIGIN + page.path);
  };
  const seasons = [...new Set(storms.map((storm) => storm.season))].sort((a, b) => a - b);
  for (const lang of PAGE_LANGS.map((item) => item.code)) {
    add(renderArchivePage(seasons.map((season) => ({ season, storms: storms.filter((storm) => storm.season === season).length })), lang));
    for (const season of seasons) add(renderSeasonPage(season, storms.filter((storm) => storm.season === season), lang));
    for (const storm of storms) add(renderStormPage(storm, lang));
  }
  return { files, urls };
}
