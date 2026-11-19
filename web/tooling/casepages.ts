/** The case pages: one static HTML page per showcase case and an index, in
 * the storm pages' five languages and layout (`stormpages.ts`): English at
 * the root, `/cases/<id>`, the others under a prefix, `/zh/cases/<id>`,
 * every translation named with `hreflang`, the English page doubling as the
 * language chooser. A case in the viewer (`/?case=<id>`) is a map with no
 * text until it runs; this is what a search for the event lands on.
 *
 * Everything is the published catalog's (`showcase.json`, the row
 * `xuebuild/showcase.py` writes): title, summary, markers and part labels
 * in the case's own words, the facts from its fields. A case that is a part
 * of another's set gets no page of its own: its lead's page lists it, as
 * the showcase list does. Node-only: imported by `discovery.ts`, never by a
 * page. */

import { formatRegion, formatUtcHour } from "../src/format";
import { en, type MessageKey } from "../src/locales/en";
import { ja } from "../src/locales/ja";
import { ko } from "../src/locales/ko";
import { zh } from "../src/locales/zh";
import { zhHant } from "../src/locales/zh-hant";
import { SITE_NAME, SITE_ORIGIN } from "../src/site";
import { pagePrefix } from "../src/tc/pages";
import { LANGUAGE_REDIRECT, PAGE_LANGS, PAGES_CSS, pageFile, type PageLang } from "./stormpages";

// --- the catalog rows the pages read ----------------------------------------

type Text = Record<string, string>;
type Box = [number, number, number, number];

export interface CaseMarker {
  kind: "place" | "event";
  lon: number;
  lat: number;
  time?: string;
  label: Text;
  note: Text;
  sources: { title: string; url: string }[];
}

export interface CaseRow {
  id: string;
  title: Text;
  summary: Text;
  model: string;
  product: string;
  runTime: string;
  eventTime?: string;
  timeZone?: string;
  forecastHours: number;
  bbox: Box;
  variables: string[];
  tags: string[];
  credit?: string;
  markers: CaseMarker[];
  parts?: { case: string; label: Text }[];
}

export interface CaseCatalog {
  /** The day the catalog was written, `YYYY-MM-DD`, when it says. */
  day: string | null;
  cases: CaseRow[];
}

const SLUG = /^[a-z0-9-]+$/;

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

/** A locale map with its English string: `xuebuild/showcase.py` requires
 * zh and en, so a row without English is not one it wrote. */
function text(value: unknown): Text | null {
  const strings: Text = {};
  for (const [key, item] of Object.entries(record(value) ?? {})) if (typeof item === "string" && item.length > 0) strings[key] = item;
  return strings.en ? strings : null;
}

function isZone(zone: unknown): zone is string {
  if (typeof zone !== "string") return false;
  try {
    new Intl.DateTimeFormat("en", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

const isTime = (value: unknown): value is string => typeof value === "string" && Number.isFinite(Date.parse(value));

function marker(input: unknown): CaseMarker | null {
  const value = record(input);
  if (!value || (value.kind !== "place" && value.kind !== "event")) return null;
  const { lon, lat } = value;
  if (typeof lon !== "number" || typeof lat !== "number" || !Number.isFinite(lon) || !Number.isFinite(lat)) return null;
  const label = text(value.label);
  const note = text(value.note);
  if (!label || !note) return null;
  if (value.time !== undefined && !isTime(value.time)) return null;
  const sources = (Array.isArray(value.sources) ? value.sources : [])
    .map((source: unknown) => record(source))
    .filter(
      (source): source is { title: string; url: string } =>
        typeof source?.title === "string" && source.title.length > 0 && typeof source.url === "string" && /^https:\/\/[^\s/]+\.\S+$/.test(source.url),
    )
    .map(({ title, url }) => ({ title, url }));
  return { kind: value.kind, lon, lat, ...(value.time !== undefined ? { time: value.time as string } : {}), label, note, sources };
}

/** One row, or null when it lacks what a page states. */
export function caseRow(input: unknown): CaseRow | null {
  const value = record(input);
  if (!value || typeof value.id !== "string" || !SLUG.test(value.id)) return null;
  const title = text(value.title);
  const summary = text(value.summary);
  const { model, product, forecastHours, bbox, variables } = value;
  if (!title || !summary || typeof model !== "string" || !model || typeof product !== "string" || !product) return null;
  if (!isTime(value.runTime) || (value.eventTime !== undefined && !isTime(value.eventTime))) return null;
  if (typeof forecastHours !== "number" || !Number.isInteger(forecastHours) || forecastHours <= 0) return null;
  if (!Array.isArray(bbox) || bbox.length !== 4 || bbox.some((item) => typeof item !== "number" || !Number.isFinite(item))) return null;
  if (!Array.isArray(variables) || variables.length === 0 || variables.some((item) => typeof item !== "string" || !/^[a-z0-9_]+$/.test(item))) return null;
  const parts = Array.isArray(value.parts)
    ? value.parts.map((part: unknown) => {
        const entry = record(part);
        const label = text(entry?.label);
        return typeof entry?.case === "string" && SLUG.test(entry.case) && label ? { case: entry.case, label } : null;
      })
    : null;
  const row: CaseRow = {
    id: value.id,
    title,
    summary,
    model,
    product,
    runTime: value.runTime as string,
    forecastHours,
    bbox: bbox as Box,
    variables: variables as string[],
    tags: Array.isArray(value.tags) ? value.tags.filter((tag): tag is string => typeof tag === "string" && SLUG.test(tag)) : [],
    markers: (Array.isArray(value.markers) ? value.markers : []).map(marker).filter((item): item is CaseMarker => item !== null),
  };
  if (value.eventTime !== undefined) row.eventTime = value.eventTime as string;
  if (isZone(value.timeZone)) row.timeZone = value.timeZone;
  if (typeof value.credit === "string" && value.credit.trim()) row.credit = value.credit.trim().replace(/\s+/g, " ");
  // A set the viewer would not offer (`partsBlock`) is listed apart, as there.
  if (parts && parts.length >= 2 && parts.every((part) => part !== null) && parts.some((part) => part!.case === row.id))
    row.parts = parts as NonNullable<CaseRow["parts"]>;
  return row;
}

/** The catalog's rows a page can be written for; a row it cannot read is
 * left out, as the showcase list leaves it out. */
export function parseCaseCatalog(json: string): CaseCatalog {
  const value = record(JSON.parse(json));
  if (value?.schemaVersion !== 1) throw new Error("showcase catalog: unsupported schema version");
  if (!Array.isArray(value.cases)) throw new Error("showcase catalog: no case list");
  const day = typeof value.generatedAt === "string" ? value.generatedAt.slice(0, 10) : "";
  return {
    day: /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : null,
    cases: value.cases.map(caseRow).filter((row): row is CaseRow => row !== null),
  };
}

/** The cases with a page: every one but a part of another's set
 * (`listedCases` in `showcase-catalog.ts`). */
export function listedRows(cases: readonly CaseRow[]): CaseRow[] {
  const parts = new Set(cases.flatMap((row) => (row.parts ?? []).filter((part) => part.case !== row.id).map((part) => part.case)));
  return cases.filter((row) => !parts.has(row.id));
}

// --- words -------------------------------------------------------------------

const DICTIONARIES: Record<PageLang, Record<MessageKey, string>> = { en, zh, "zh-Hant": zhHant, ja, ko };

interface Words {
  open: string;
  openPart: string;
  dataset: string;
  credit: string;
  tags: string;
  time: string;
  allCases: string;
  showcase: string;
  languages: string;
}

const WORDS: Record<PageLang, Words> = {
  en: {
    open: "Open the case on the map",
    openPart: "Open on the map",
    dataset: "Dataset",
    credit: "Data",
    tags: "Tags",
    time: "Time",
    allCases: "All cases",
    showcase: "The cases with previews",
    languages: "Languages",
  },
  zh: {
    open: "在地图上打开这个个例",
    openPart: "在地图上打开",
    dataset: "数据集",
    credit: "数据",
    tags: "标签",
    time: "时间",
    allCases: "全部个例",
    showcase: "带预览的个例列表",
    languages: "语言",
  },
  "zh-Hant": {
    open: "在地圖上開啟這個個例",
    openPart: "在地圖上開啟",
    dataset: "資料集",
    credit: "資料",
    tags: "標籤",
    time: "時間",
    allCases: "全部個例",
    showcase: "附預覽的個例列表",
    languages: "語言",
  },
  ja: {
    open: "地図でこの事例を開く",
    openPart: "地図で開く",
    dataset: "データセット",
    credit: "データ",
    tags: "タグ",
    time: "時刻",
    allCases: "事例一覧",
    showcase: "プレビュー付きの事例一覧",
    languages: "言語",
  },
  ko: {
    open: "지도에서 이 사례 열기",
    openPart: "지도에서 열기",
    dataset: "데이터셋",
    credit: "데이터",
    tags: "태그",
    time: "시각",
    allCases: "전체 사례",
    showcase: "미리보기가 있는 사례 목록",
    languages: "언어",
  },
};

function say(lang: PageLang, key: MessageKey, params: Record<string, string | number> = {}): string {
  return DICTIONARIES[lang][key].replace(/\{(\w+)\}/g, (match, name: string) => (name in params ? String(params[name]) : match));
}

/** A case string in `lang`, falling back as the viewer does
 * (`localizedText`): Traditional Chinese to Simplified, then English. */
export function caseText(strings: Text, lang: PageLang): string {
  const chain = lang === "zh-Hant" ? [lang, "zh", "en"] : [lang, "en"];
  for (const candidate of chain) if (strings[candidate]) return strings[candidate]!;
  return Object.values(strings)[0] ?? "";
}

/** "2 m temperature (tmp2m)": the viewer's label for a plain bundle id,
 * the id alone for one whose label is built from parts (a level). */
export function variableName(id: string, lang: PageLang): string {
  const key = `varLabel${id.charAt(0).toUpperCase()}${id.slice(1)}`;
  const label = (DICTIONARIES[lang] as Record<string, string>)[key];
  return label && !label.includes("{") ? `${label} (${id})` : id;
}

/** `2025-07-04 05:05 UTC−05:00 (America/Chicago)`: the case's own clock,
 * spelled the same in every language. */
export function zonedTime(iso: string, zone: string | undefined): string {
  const date = new Date(iso);
  if (!zone) return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZoneName: "longOffset",
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  );
  const offset = (parts.timeZoneName ?? "GMT").replace(/^GMT/, "UTC").replace("-", "−");
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} ${offset === "UTC" ? "UTC+00:00" : offset} (${zone})`;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

/** JSON for a `<script>` element: no `</script>` can close it early. */
function scriptJson(value: unknown): string {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

/** The summary cut for a meta description, at a word or a CJK character. */
export function shortDescription(summary: string, limit = 200): string {
  if (summary.length <= limit) return summary;
  const cut = summary.slice(0, limit);
  const space = cut.lastIndexOf(" ");
  return `${(space > limit * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,;:，、；：]+$/, "")}…`;
}

// --- addresses ----------------------------------------------------------------

/** The root-relative address of a page, as Pages serves it (no `.html`). */
export function casePagePath(lang: PageLang, id?: string): string {
  return `${pagePrefix(lang)}/cases/${id ?? ""}`;
}

/** The viewer opening the case, in the page's language. */
export function caseViewerLink(lang: PageLang, id: string): string {
  return `/?case=${encodeURIComponent(id)}${lang === "en" ? "" : `&lang=${encodeURIComponent(lang)}`}`;
}

function showcaseLink(lang: PageLang): string {
  return lang === "en" ? "/showcase" : `/showcase?lang=${encodeURIComponent(lang)}`;
}

function allPaths(id?: string): Record<PageLang, string> {
  return Object.fromEntries(PAGE_LANGS.map((item) => [item.code, casePagePath(item.code, id)])) as Record<PageLang, string>;
}

// --- the pages ------------------------------------------------------------------

interface PageShell {
  lang: PageLang;
  paths: Record<PageLang, string>;
  title: string;
  description: string;
  crumbs: { name: string; path: string }[];
  /** The node the page is about, beside the WebPage, if any. */
  about?: Record<string, unknown>;
  body: string;
}

function render(shell: PageShell): string {
  const spec = PAGE_LANGS.find((item) => item.code === shell.lang)!;
  const words = WORDS[shell.lang];
  const own = SITE_ORIGIN + shell.paths[shell.lang];
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
    "@id": own,
    url: own,
    name: shell.title,
    description: shell.description,
    inLanguage: spec.htmlLang,
    isPartOf: { "@type": "WebSite", "@id": `${SITE_ORIGIN}/#website`, url: `${SITE_ORIGIN}/`, name: SITE_NAME },
    ...(shell.about ? { mainEntity: shell.about } : {}),
  };
  const crumbs = shell.crumbs
    .map((crumb, index) => (index === shell.crumbs.length - 1 ? `<span>${escapeHtml(crumb.name)}</span>` : `<a href="${crumb.path}">${escapeHtml(crumb.name)}</a>`))
    .join(" › ");
  const switcher = PAGE_LANGS.map((item) =>
    item.code === shell.lang
      ? `<li aria-current="true">${item.endonym}</li>`
      : `<li><a href="${shell.paths[item.code]}" hreflang="${item.htmlLang}" lang="${item.htmlLang}" data-locale="${item.code}">${item.endonym}</a></li>`,
  ).join("");
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
    <link rel="stylesheet" href="/cases/pages.css">
    <script type="application/ld+json">${scriptJson(breadcrumb)}</script>
    <script type="application/ld+json">${scriptJson(page)}</script>${shell.lang === "en" ? LANGUAGE_REDIRECT : ""}
  </head>
  <body>
    <main>
      <nav class="crumbs" aria-label="Breadcrumb">${crumbs}</nav>
      ${shell.body}
      <p class="more"><a href="${casePagePath(shell.lang)}">${escapeHtml(words.allCases)}</a> · <a href="${showcaseLink(shell.lang)}">${escapeHtml(words.showcase)}</a></p>
      <nav class="langs" aria-label="${words.languages}"><ul>${switcher}</ul></nav>
      <footer><a href="/">${SITE_NAME}</a></footer>
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

function definitions(rows: readonly (readonly [string, string])[]): string {
  return rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

function position(lat: number, lon: number): string {
  return `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"} ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? "E" : "W"}`;
}

/** The end of a case's axis, ISO, for the dataset's temporal coverage. */
function axisEnd(row: CaseRow): string {
  return new Date(Date.parse(row.runTime) + row.forecastHours * 3600_000).toISOString().replace(/\.000Z$/, "Z");
}

function factRows(row: CaseRow, lang: PageLang): [string, string][] {
  const words = WORDS[lang];
  return [
    ...(row.eventTime ? [[say(lang, "showcaseEventLabel"), zonedTime(row.eventTime, row.timeZone)] as [string, string]] : []),
    [say(lang, "showcaseSeriesLabel"), formatUtcHour(row.runTime)],
    [say(lang, "showcaseSpanLabel"), say(lang, "showcaseHours", { count: row.forecastHours })],
    [words.dataset, `${row.model} · ${row.product}`],
    [say(lang, "showcaseRegionLabel"), formatRegion(row.bbox)],
    ...(row.credit ? [[words.credit, row.credit] as [string, string]] : []),
  ];
}

function markerItem(item: CaseMarker, row: CaseRow, lang: PageLang): string {
  const kind = say(lang, item.kind === "place" ? "markerPlace" : "markerEvent");
  const time = item.time ? ` · ${zonedTime(item.time, row.timeZone)}` : "";
  const sources = item.sources.length
    ? `<p class="note">${escapeHtml(say(lang, "markerSources"))}: ${item.sources
        .map((source) => `<a href="${escapeHtml(source.url)}" rel="noopener">${escapeHtml(source.title)}</a>`)
        .join(" · ")}</p>`
    : "";
  return `<li><h3>${escapeHtml(caseText(item.label, lang))}</h3><p class="note">${escapeHtml(kind)} · ${position(item.lat, item.lon)}${escapeHtml(time)}</p><p>${escapeHtml(caseText(item.note, lang))}</p>${sources}</li>`;
}

export function renderCasePage(row: CaseRow, catalog: readonly CaseRow[], lang: PageLang): { path: string; html: string } {
  const words = WORDS[lang];
  const paths = allPaths(row.id);
  const title = caseText(row.title, lang);
  const summary = caseText(row.summary, lang);
  const variables = row.variables.map((id) => variableName(id, lang));
  const byId = new Map(catalog.map((item) => [item.id, item]));
  const parts = (row.parts ?? []).filter((part) => byId.has(part.case));
  const partsSection =
    parts.length > 1
      ? `<section><h2>${escapeHtml(say(lang, "casePartsAria"))}</h2><ol class="parts">${parts
          .map((part) => {
            const other = byId.get(part.case)!;
            // The lead's own summary is the page's lead paragraph already.
            const summary = other.id === row.id ? "" : `<p>${escapeHtml(caseText(other.summary, lang))}</p>`;
            return `<li><h3>${escapeHtml(caseText(part.label, lang))}</h3><p class="note">${escapeHtml(caseText(other.title, lang))} · ${escapeHtml(other.model)} · ${formatUtcHour(other.runTime)} · ${escapeHtml(say(lang, "showcaseHours", { count: other.forecastHours }))}</p>${summary}<p><a href="${caseViewerLink(lang, other.id)}">${escapeHtml(words.openPart)} →</a></p></li>`;
          })
          .join("")}</ol></section>`
      : "";
  const markers = row.markers.length
    ? `<section><h2>${escapeHtml(say(lang, "caseMarkersAria"))}</h2><ul class="markers">${row.markers.map((item) => markerItem(item, row, lang)).join("")}</ul></section>`
    : "";
  const tags = row.tags.length ? `<p class="tags"><span class="note">${escapeHtml(words.tags)}</span> ${row.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join(" ")}</p>` : "";
  const body = `<header>
        <p class="eyebrow">${escapeHtml(row.model)}${row.eventTime ? ` · ${row.eventTime.slice(0, 10)}` : ""}</p>
        <h1>${escapeHtml(title)}</h1>
        <p class="lead">${escapeHtml(summary)}</p>
        <p><a class="open" href="${caseViewerLink(lang, row.id)}">${escapeHtml(words.open)} →</a></p>
      </header>
      <dl class="facts">${definitions(factRows(row, lang))}</dl>
      <section><h2>${escapeHtml(say(lang, "showcaseVariablesLabel"))}</h2><ul class="fields">${variables.map((name) => `<li>${escapeHtml(name)}</li>`).join("")}</ul></section>
      ${markers}
      ${partsSection}
      ${tags}`;
  const [west, south, east, north] = row.bbox;
  const dataset = {
    "@type": "Dataset",
    name: title,
    description: summary,
    url: SITE_ORIGIN + caseViewerLink(lang, row.id),
    temporalCoverage: `${row.runTime}/${axisEnd(row)}`,
    spatialCoverage: { "@type": "Place", geo: { "@type": "GeoShape", box: `${south} ${west} ${north} ${east}` } },
    variableMeasured: variables,
    measurementTechnique: `${row.model} ${row.product}`,
    ...(row.tags.length ? { keywords: row.tags } : {}),
    ...(row.credit ? { creditText: row.credit } : {}),
    isAccessibleForFree: true,
  };
  return {
    path: paths[lang],
    html: render({
      lang,
      paths,
      title,
      description: shortDescription(summary),
      crumbs: [
        { name: SITE_NAME, path: "/" },
        { name: say(lang, "showcaseLink"), path: casePagePath(lang) },
        { name: title, path: paths[lang] },
      ],
      about: dataset,
      body,
    }),
  };
}

export function renderCaseIndex(rows: readonly CaseRow[], lang: PageLang): { path: string; html: string } {
  const paths = allPaths();
  const heading = say(lang, "showcaseHeading");
  const intro = say(lang, "showcaseIntro");
  const items = rows
    .map((row) => {
      const when = (row.eventTime ?? row.runTime).slice(0, 10);
      return `<li><a href="${casePagePath(lang, row.id)}"><span class="code">${escapeHtml(row.model)}</span> <span class="name">${escapeHtml(caseText(row.title, lang))}</span> <span class="meta">${when}</span></a></li>`;
    })
    .join("");
  return {
    path: paths[lang],
    html: render({
      lang,
      paths,
      title: heading,
      description: intro,
      crumbs: [
        { name: SITE_NAME, path: "/" },
        { name: say(lang, "showcaseLink"), path: paths[lang] },
      ],
      body: `<header><h1>${escapeHtml(heading)}</h1><p class="lead">${escapeHtml(intro)}</p></header><ul class="storms">${items}</ul>`,
    }),
  };
}

/** The storm pages' stylesheet and the few rules the case pages add. */
export const CASE_PAGES_CSS = `${PAGES_CSS}.fields,.markers,.parts{margin:0;padding-left:18px}.fields li{margin:2px 0}
.markers li,.parts li{margin:0 0 14px}h3{margin:0;font-size:15px}.markers p,.parts p{margin:2px 0}
.tags span:not(.note){display:inline-block;margin:0 4px 4px 0;padding:0 8px;border:1px solid var(--line);border-radius:10px;font:11px ui-monospace,monospace}
.more{margin-top:28px;font-size:13px}
`;

export interface CaseSite {
  files: { fileName: string; source: string }[];
  /** Absolute addresses, for the sitemap. */
  urls: string[];
  /** The sitemap's `lastmod` per address: the day the catalog was written,
   * which every page is derived from. */
  lastmod: Record<string, string>;
}

/** Every case page and index in every language, and their stylesheet. */
export function buildCaseSite(catalog: CaseCatalog): CaseSite {
  const files: CaseSite["files"] = [{ fileName: "cases/pages.css", source: CASE_PAGES_CSS }];
  const urls: string[] = [];
  const lastmod: Record<string, string> = {};
  const rows = listedRows(catalog.cases);
  const add = (page: { path: string; html: string }) => {
    files.push({ fileName: pageFile(page.path), source: page.html });
    const url = SITE_ORIGIN + page.path;
    urls.push(url);
    if (catalog.day) lastmod[url] = catalog.day;
  };
  if (rows.length === 0) return { files: [], urls, lastmod };
  for (const lang of PAGE_LANGS.map((item) => item.code)) {
    add(renderCaseIndex(rows, lang));
    for (const row of rows) add(renderCasePage(row, catalog.cases, lang));
  }
  return { files, urls, lastmod };
}
