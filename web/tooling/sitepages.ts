/** The shell's pages in every language: one copy of each built page per
 * locale, at its path (`sitepaths.ts`) — English at the root, the others
 * under their prefix — each with its head and its static copy in that
 * language, and the pages the build derives from the viewer: one per
 * dataset (`/hrrr`, `modelpages.ts`) and one per showcase case
 * (`/showcase/<id>`), the viewer under the case's head with its case panel
 * already filled in. The list (`/showcase`) carries the catalog's cards.
 *
 * ```
 * index.html            ja/index.html             /        /ja/
 * hrrr.html             ja/hrrr.html              /hrrr    /ja/hrrr
 * showcase.html         ja/showcase.html          /showcase
 * showcase/<id>.html    ja/showcase/<id>.html     /showcase/<id>
 * compare.html …        ja/compare.html …         /compare /metrics /typhoon /workbench /api
 * ```
 *
 * A copy sits up to three segments deep, where the build's relative
 * `./assets/…` would resolve under its directory, so every page this writes
 * names its assets root-absolute. The shell rewrites the head again once it
 * knows the view (`pagemeta.ts`) and fills the case panel and the cards
 * from the catalog it reads itself, replacing what the build wrote.
 *
 * Node-only: imported by `discovery.ts` and vite.config.ts, never by a
 * page. */

import { existsSync, statSync } from "node:fs";
import { join, normalize } from "node:path";
import type { Connect, Plugin } from "vite";

import { airportIndexPath } from "../src/airports/pages";
import { MARKER_GLYPHS } from "../src/casemarkers";
import { formatBytes, formatRegion, formatUtcHour } from "../src/format";
import { localeHtmlLang, localeMessages, localeOgLocale, type MessageKey } from "../src/i18n";
import { FORECAST_MODELS, isObservationModel, modelDefaultVariable, type ForecastModelId } from "../src/manifest";
import {
  fieldLabel,
  modelEyebrowText,
  modelPageHead,
  PERSON_ID,
  provenanceNodes,
  SITE_DATASET_ID,
  type DatasetProvenance,
  type ModelPageHead,
} from "../src/modelpage";
import { FORECAST_MODEL_IDS, modelFromPathname } from "../src/modelpaths";
import { alternateUrls, pageUrl } from "../src/pagemeta";
import { caseParts, listedCases, localizedText, type ShowcaseCase, type ShowcaseMarker } from "../src/showcase-catalog";
import { SITE_NAME, SITE_ORIGIN } from "../src/site";
import {
  caseFromPath,
  casePathname,
  isShellPath,
  LANGUAGE_CHOOSER,
  LOCALE_CODES,
  LOCALE_PREFIXES,
  localizedPath,
  SHELL_PAGES,
  SHOWCASE_PATH,
  splitLocalePath,
  type Locale,
} from "../src/sitepaths";
import { archivePagePath, isStaticPagePath, pagePrefix } from "../src/tc/pages";

// --- words -------------------------------------------------------------------

/** A message in `locale`, its `{name}` parameters filled; null when one
 * stays unfilled (a unit the shell fills at run time). */
function say(locale: Locale, key: MessageKey, params: Record<string, string | number> = {}): string {
  return localeMessages(locale)[key].replace(/\{(\w+)\}/g, (match, name: string) => (name in params ? String(params[name]) : match));
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
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

// --- the markup ----------------------------------------------------------------

/** Replace exactly one match, or fail: a page the build cannot find its way
 * around must not ship half-rewritten. */
export function replaceOnce(html: string, pattern: RegExp, replace: (match: string) => string, what: string): string {
  const flags = pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`;
  const matches = html.match(new RegExp(pattern.source, flags));
  if (!matches || matches.length !== 1) throw new Error(`site pages: expected one ${what}, found ${matches?.length ?? 0}`);
  return html.replace(new RegExp(pattern.source, pattern.flags.replace("g", "")), replace);
}

function setAttribute(tag: string, name: string, value: string): string {
  const escaped = escapeHtml(value);
  const pattern = new RegExp(`\\s${name}="[^"]*"`);
  if (pattern.test(tag)) return tag.replace(pattern, ` ${name}="${escaped}"`);
  return tag.replace(/\s*\/?>$/, (end) => ` ${name}="${escaped}"${end}`);
}

/** The built page with every asset and link the build wrote relative
 * (`./assets/…`, `./`) made root-absolute, so the copy works at any depth. */
export function absolutizeUrls(html: string): string {
  return html.replace(/(\s(?:src|href))="\.\/([^"]*)"/g, '$1="/$2"');
}

/** The page's static copy in `locale`, as the shell's
 * `applyStaticMessages` would write it: the text of each `data-i18n`
 * element (one with plain text only, so the markup around it survives), the
 * `aria-label`, `content` and `data-tip` the other attributes name, `<html
 * lang>` and the links to the shell's pages. A message with a parameter the
 * shell fills at run time (a unit) keeps the English. */
export function translateMarkup(html: string, locale: Locale): string {
  const year = new Date().getUTCFullYear();
  const words = (key: string): string | null => {
    const messages = localeMessages(locale) as Record<string, string>;
    if (!(key in messages)) return null;
    const text = say(locale, key as MessageKey, { year });
    return text.includes("{") ? null : text;
  };
  let out = html.replace(/<html lang="[^"]*"/, `<html lang="${localeHtmlLang(locale)}"`);
  out = out.replace(/(<([a-z][a-z0-9]*)\b[^>]*\sdata-i18n="([A-Za-z0-9]+)"[^>]*>)([^<]*)(<\/\2>)/g, (match, open: string, _tag, key: string, _text, close: string) => {
    const text = words(key);
    return text === null ? match : `${open}${escapeHtml(text)}${close}`;
  });
  out = out.replace(/<[a-z][a-z0-9]*\b[^>]*\sdata-i18n-aria="([A-Za-z0-9]+)"[^>]*>/g, (tag, key: string) => {
    const text = words(key);
    return text === null ? tag : setAttribute(tag, "aria-label", text);
  });
  out = out.replace(/<[a-z][a-z0-9]*\b[^>]*\sdata-i18n-content="([A-Za-z0-9]+)"[^>]*>/g, (tag, key: string) => {
    const text = words(key);
    return text === null ? tag : setAttribute(tag, "content", text);
  });
  out = out.replace(/<[a-z][a-z0-9]*\b[^>]*\sdata-i18n-tip="([A-Za-z0-9]+)"[^>]*>/g, (tag, key: string) => {
    const text = words(key);
    return text === null ? tag : setAttribute(tag, "data-tip", text);
  });
  return localizeLinks(out, locale);
}

const DATASET_PATH_IDS: ReadonlySet<string> = new Set(FORECAST_MODEL_IDS);

/** Every `<a href="/…">` to one of the site's pages, in `locale`, as
 * `i18n.ts::localizedHref` has it: a storm or airport page is the English
 * one in a language without its own. */
export function localizeLinks(html: string, locale: Locale): string {
  return html.replace(/(<a\b[^>]*\shref=")(\/[^"]*)"/g, (match, open: string, href: string) => {
    if (href.startsWith("//")) return match;
    const url = new URL(href, SITE_ORIGIN);
    const { path } = splitLocalePath(url.pathname);
    if (isStaticPagePath(path)) return `${open}${escapeHtml(`${pagePrefix(locale)}${path}`)}${url.search}${url.hash}"`;
    if (!isShellPath(path, DATASET_PATH_IDS)) return match;
    return `${open}${escapeHtml(localizedPath(locale, path))}${url.search}${url.hash}"`;
  });
}

function setMeta(html: string, attribute: "name" | "property", key: string, content: string): string {
  return replaceOnce(
    html,
    new RegExp(`<meta ${attribute}="${key}"[^>]*>`),
    (tag) => setAttribute(tag.replace(/ data-i18n-content="[^"]*"/, ""), "content", content),
    `${key} meta`,
  );
}

interface Head {
  /** Root-relative path within a language: `/`, `/hrrr`, `/showcase/<id>`. */
  path: string;
  locale: Locale;
  /** The full title, ` · Xue` included; the page's own when absent. */
  title?: string;
  /** The page's own (translated) when absent. */
  description?: string;
  /** JSON-LD documents added to the head. */
  jsonLd?: readonly unknown[];
}

/** The head of one language's copy of a page: canonical to itself, the
 * whole hreflang set, `og:url` and `og:locale`, the title and description
 * where given, and the language chooser on the English copy only. A page
 * kept out of the index (no canonical link, the API console) gets no
 * hreflang set either. */
export function writeHead(html: string, head: Head): string {
  const own = pageUrl(head.path, head.locale);
  const indexed = /<link rel="canonical" href="[^"]*" \/>/.test(html);
  let out = indexed
    ? replaceOnce(html, /<link rel="canonical" href="[^"]*" \/>/, () => `<link rel="canonical" href="${escapeHtml(own)}" />`, "canonical link")
    : html;
  out = out.replace(/\n[ \t]*<link rel="alternate" hreflang="[^"]*" href="[^"]*" \/>/g, "");
  const alternates = (indexed ? alternateUrls(head.path) : [])
    .map(({ hreflang, href }) => `\n    <link rel="alternate" hreflang="${hreflang}" href="${escapeHtml(href)}" />`)
    .join("");
  out = out.replace(/(<link rel="canonical" href="[^"]*" \/>)/, `$1${alternates}`);
  out = out.replace(/<meta property="og:url"[^>]*>/, (tag) => setAttribute(tag, "content", own));
  if (!indexed) {
    // No social card either.
  } else if (/<meta property="og:locale"[^>]*>/.test(out)) {
    out = out.replace(/<meta property="og:locale"[^>]*>/, (tag) => setAttribute(tag, "content", localeOgLocale(head.locale)));
  } else {
    out = out.replace(/(<meta property="og:type"[^>]*>)/, `$1\n    <meta property="og:locale" content="${localeOgLocale(head.locale)}" />`);
  }
  if (head.title !== undefined) {
    const title = head.title;
    out = replaceOnce(out, /<title>[^<]*<\/title>/, () => `<title>${escapeHtml(title)}</title>`, "title");
    out = out.replace(/<meta (?:property="og:title"|name="twitter:title")[^>]*>/g, (tag) => setAttribute(tag, "content", title));
  }
  if (head.description !== undefined) {
    out = setMeta(out, "name", "description", head.description);
    out = out.replace(/<meta (?:property="og:description"|name="twitter:description")[^>]*>/g, (tag) =>
      setAttribute(tag, "content", head.description!),
    );
  }
  if (head.locale !== "en") out = out.replace(/\n?[ \t]*<script data-locale-chooser[^>]*>[\s\S]*?<\/script>/, "");
  for (const document of head.jsonLd ?? []) {
    out = out.replace(/<\/head>/, `  <script type="application/ld+json">${scriptJson(document)}</script>\n  </head>`);
  }
  return out;
}

// --- the shell's own pages ---------------------------------------------------------

/** The shell's pages besides the viewer, by built file: the path each is
 * at, and the message that titles it in another language (the English
 * title is the markup's; a page with no message keeps it). */
const SHELL_PAGE_FILES: readonly { file: string; path: string; title: MessageKey | null }[] = [
  { file: "compare.html", path: "/compare", title: "compareTitle" },
  { file: "metrics.html", path: "/metrics", title: "metricsHeading" },
  { file: "typhoon.html", path: "/typhoon", title: "tcHistoryHeading" },
  { file: "workbench.html", path: "/workbench", title: "wbTitle" },
  { file: "api.html", path: "/api", title: "apiConsoleTitle" },
];

/** The built file a page in a language is written to: `ja/compare.html`,
 * `ja/index.html` for the Japanese home, `showcase/<id>.html`. */
export function pageFileName(locale: Locale, path: string): string {
  const prefix = LOCALE_PREFIXES[locale].slice(1);
  const dir = prefix === "" ? "" : `${prefix}/`;
  return path === "/" ? `${dir}index.html` : `${dir}${path.slice(1)}.html`;
}

/** The copy of a page that only needs its language: the static copy, the
 * head and, for another page than the home, the JSON-LD's own address. */
function localizedPage(builtHtml: string, locale: Locale, path: string, titleKey: MessageKey | null): string {
  let html = locale === "en" ? builtHtml : translateMarkup(builtHtml, locale);
  if (path !== "/") {
    // A page's JSON-LD names its own address: this language's.
    html = html.split(`"${SITE_ORIGIN}${path}"`).join(`"${pageUrl(path, locale)}"`);
  }
  // The home leads with the site's name, as its English markup does.
  const words = titleKey === null ? null : say(locale, titleKey);
  const title = locale === "en" || words === null ? undefined : path === "/" ? `${SITE_NAME} · ${words}` : `${words} · ${SITE_NAME}`;
  return writeHead(html, { path, locale, ...(title === undefined ? {} : { title }) });
}

// --- the datasets' pages ---------------------------------------------------------

/** The nodes of the home's JSON-LD graph that describe the whole site: the
 * catalogue of live datasets, the application and its source code. A
 * dataset's or a case's page names them by `@id` and carries none of them,
 * so each is stated once. */
const HOME_NODE_TYPES: ReadonlySet<unknown> = new Set(["WebApplication", "SoftwareSourceCode", "Dataset"]);

/** The home's JSON-LD graph, less the home's own nodes, plus `nodes`. */
function writeGraph(html: string, nodes: readonly Record<string, unknown>[]): string {
  return replaceOnce(
    html,
    /(<script type="application\/ld\+json">)([\s\S]*?)(<\/script>)/,
    (block) => {
      const [, open, json, close] = /(<script type="application\/ld\+json">)([\s\S]*?)(<\/script>)/.exec(block)!;
      const document = JSON.parse(json!) as { "@graph"?: Record<string, unknown>[] };
      const graph = document["@graph"];
      if (!Array.isArray(graph)) throw new Error("dataset page: index.html's JSON-LD has no @graph");
      for (const needed of [PERSON_ID, SITE_DATASET_ID]) {
        if (!graph.some((node) => node["@id"] === needed)) throw new Error(`dataset page: index.html's JSON-LD has no ${needed}`);
      }
      document["@graph"] = [...graph.filter((node) => !HOME_NODE_TYPES.has(node["@type"])), ...nodes];
      // `<` inside a script would end it early in a hostile string; none of
      // the table's strings has one, but the escape costs nothing.
      const body = JSON.stringify(document, null, 2).replace(/</g, "\\u003c");
      return `${open}\n${body}\n    ${close}`;
    },
    "JSON-LD block",
  );
}

/** The title block (`#model-trigger`) as it opens on a dataset: the field
 * by its label, the dataset's own line. The shell writes both again once
 * the run is open, and they stand when it never is. */
function writeTitleBlock(html: string, id: ForecastModelId, variable: string, locale: Locale): string {
  let out = replaceOnce(html, /<span id="model-eyebrow">[^<]*<\/span>/, () => `<span id="model-eyebrow">${escapeHtml(modelEyebrowText(id))}</span>`, "#model-eyebrow");
  // Daily imagery has no field of its own to name.
  const label = FORECAST_MODELS[id].imagery ? FORECAST_MODELS[id].label : fieldLabel(variable, locale);
  if (label !== null) out = replaceOnce(out, /<span id="variable-title">[^<]*<\/span>/, () => `<span id="variable-title">${escapeHtml(label)}</span>`, "#variable-title");
  return replaceOnce(
    out,
    /<span id="variable-code" class="title-code">[^<]*<\/span>/,
    () => `<span id="variable-code" class="title-code">${escapeHtml(FORECAST_MODELS[id].label)}</span>`,
    "#variable-code",
  );
}

/** The dataset sheet's row of the dataset on screen, marked; none on a
 * case, which the sheet does not open on. */
function markCurrentDataset(html: string, id: ForecastModelId | null): string {
  return html.replace(/<a data-model="([a-z0-9]+)" href="([^"]*)"(?: aria-current="true")?>/g, (_row, own: string, href: string) =>
    `<a data-model="${own}" href="${href}"${own === id ? ' aria-current="true"' : ""}>`,
  );
}

const NOSCRIPT_NOTE = /<noscript>\s*<div class="noscript-note">[\s\S]*?<\/noscript>/;

/** The datasets the sheet offers, in its order: a dataset kept off the sheet
 * is not linked from the others' pages either. */
function sheetDatasets(html: string, ids: readonly ForecastModelId[]): ForecastModelId[] {
  return [...html.matchAll(/<a data-model="([a-z0-9]+)"/g)].map((row) => row[1] as ForecastModelId).filter((id) => ids.includes(id));
}

/** The no-script note of a dataset's page: what it is, who makes it, and
 * the site's other datasets and pages. */
function modelNoscript(head: ModelPageHead, ids: readonly ForecastModelId[], own: ForecastModelId, provenance: DatasetProvenance | undefined, locale: Locale): string {
  const link = (path: string, text: string) => `<a href="${escapeHtml(path)}">${escapeHtml(text)}</a>`;
  const producers = (provenance?.producers ?? []).map(({ name, url }) => (url === undefined ? escapeHtml(name) : `<a href="${escapeHtml(url)}" rel="noopener">${escapeHtml(name)}</a>`)).join(" · ");
  const licenses = (provenance?.licenses ?? []).map((url) => `<a href="${escapeHtml(url)}" rel="license noopener">${escapeHtml(url)}</a>`).join(" · ");
  const datasets = ids.filter((id) => id !== own).map((id) => link(localizedPath(locale, `/${id}`), FORECAST_MODELS[id].label)).join(" · ");
  const pages = [
    link(localizedPath(locale, SHOWCASE_PATH), say(locale, "showcaseHeading")),
    link(airportIndexPath(locale), say(locale, "airportIndexHeading")),
    link(archivePagePath(locale), say(locale, "tcHistoryHeading")),
    link("/llms.txt", "llms.txt"),
  ].join(" · ");
  return `<noscript>
      <div class="noscript-note">
        <p><strong>${escapeHtml(head.title)}</strong></p>
        <p>${escapeHtml(head.description)}</p>${locale === "en" && provenance?.description ? `\n        <p>${escapeHtml(provenance.description)}</p>` : ""}${producers || licenses ? `\n        <p>${[producers, licenses].filter(Boolean).join(" · ")}</p>` : ""}
        <p>${datasets}</p>
        <p>${pages}</p>
      </div>
    </noscript>`;
}

/** The viewer as one dataset's page in a language: its title,
 * description, canonical, hreflang set and social card name the dataset's
 * path, its `Dataset` joins the page's JSON-LD graph, and the title block
 * and the no-script note say which dataset it is. The description loses
 * its i18n key, so the shell's static-message pass leaves the dataset's
 * own in place (the shell rewrites the head itself once it knows the
 * view). */
export function renderModelPage(
  indexHtml: string,
  id: ForecastModelId,
  provenance?: DatasetProvenance,
  locale: Locale = "en",
  ids: readonly ForecastModelId[] = [id],
): string {
  const head = modelPageHead(id, provenance, locale);
  let html = locale === "en" ? indexHtml : translateMarkup(indexHtml, locale);
  html = writeTitleBlock(html, id, modelDefaultVariable(id, "prate"), locale);
  html = markCurrentDataset(html, id);
  html = replaceOnce(html, NOSCRIPT_NOTE, () => modelNoscript(head, sheetDatasets(indexHtml, ids), id, provenance, locale), "noscript note");
  html = writeHead(html, { path: head.path, locale, title: `${head.title} · ${SITE_NAME}`, description: head.description });
  return writeGraph(html, [head.dataset]);
}

// --- the cases' pages ------------------------------------------------------------

/** `2025-07-04 05:05 UTC−05:00 (America/Chicago)`: the case's own clock,
 * spelled the same in every language. */
export function zonedTime(time: string | number, zone: string | undefined): string {
  const date = new Date(time);
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

function position(lat: number, lon: number): string {
  return `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"} ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? "E" : "W"}`;
}

/** The case's facts as the showcase card states them (`showcase.ts`). */
function caseFacts(row: ShowcaseCase, locale: Locale): [string, string][] {
  const observations = isObservationModel(row.modelId);
  return [
    ...(row.eventTime ? [[say(locale, "showcaseEventLabel"), formatUtcHour(row.eventTime)] as [string, string]] : []),
    [say(locale, observations ? "showcaseSeriesLabel" : "showcaseRunLabel"), formatUtcHour(row.runTime)],
    [say(locale, observations ? "showcaseSpanLabel" : "showcaseRangeLabel"), say(locale, "showcaseHours", { count: row.forecastHours })],
    [say(locale, "showcaseRegionLabel"), formatRegion(row.bbox)],
    [say(locale, "showcaseGridLabel"), `${row.grid.width}×${row.grid.height}`],
    [say(locale, "showcaseSizeLabel"), formatBytes(row.byteLength)],
  ];
}

function definitions(rows: readonly (readonly [string, string])[], wide: ReadonlySet<number> = new Set()): string {
  return rows
    .map(([label, value], index) => `<div${wide.has(index) ? ' class="is-wide"' : ""}><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("");
}

/** A marker in words, for the page's no-script copy: its kind, place,
 * time, note and sources, as its card on the map says them. */
function markerText(marker: ShowcaseMarker, row: ShowcaseCase, locale: Locale): string {
  const kind = say(locale, marker.kind === "place" ? "markerPlace" : "markerEvent");
  const time = marker.time === undefined ? "" : ` · ${zonedTime(marker.time, row.timeZone)}`;
  const sources = marker.sources.length
    ? `<br>${escapeHtml(say(locale, "markerSources"))}: ${marker.sources
        .map((source) => `<a href="${escapeHtml(source.url)}" rel="noopener">${escapeHtml(source.title)}</a>`)
        .join(" · ")}`
    : "";
  return `<li><strong>${escapeHtml(localizedText(marker.label, locale))}</strong> — ${escapeHtml(kind)} · ${position(marker.lat, marker.lon)}${escapeHtml(time)}<br>${escapeHtml(localizedText(marker.note, locale))}${sources}</li>`;
}

/** Fill an element the markup ships empty (`<tag id="…" …></tag>`), and
 * show it when it was hidden. */
function fillElement(html: string, id: string, inner: string, show: boolean): string {
  return replaceOnce(
    html,
    new RegExp(`<([a-z0-9]+)([^>]*\\sid="${id}"[^>]*)>(</\\1>)`),
    (match) => {
      const [, tag, attributes] = new RegExp(`<([a-z0-9]+)([^>]*\\sid="${id}"[^>]*)>`).exec(match)!;
      const shown = show ? attributes!.replace(/\shidden(?=[\s>]|$)/, "") : attributes!;
      return `<${tag}${shown}>${inner}</${tag}>`;
    },
    `#${id}`,
  );
}

/** The viewer's case panel (`index.html`'s `#case-banner`, filled by
 * `main.ts::updateCasePresentation`) in the build's hands: the title, the
 * summary, the region, the markers' buttons and the event's parts, so the
 * page reads as the case before the catalog loads; the shell writes the
 * same elements again from the catalog. */
function prerenderCasePanel(html: string, row: ShowcaseCase, parts: readonly { case: string; label: Record<string, string> }[] | null, locale: Locale): string {
  let out = replaceOnce(openingAsCase(html), /<div class="case-banner" id="case-banner" hidden>/, () => '<div class="case-banner" id="case-banner">', "case banner");
  out = fillElement(out, "case-title", escapeHtml(localizedText(row.title, locale)), false);
  out = fillElement(out, "case-summary", escapeHtml(localizedText(row.summary, locale)), false);
  out = fillElement(out, "case-region", escapeHtml(formatRegion(row.bbox)), false);
  const markers = row.markers ?? [];
  out = fillElement(
    out,
    "case-markers",
    markers
      .map(
        (marker) =>
          `<li><button type="button" data-kind="${marker.kind}"><span class="case-marker-glyph" aria-hidden="true">${MARKER_GLYPHS[marker.kind]}</span><span>${escapeHtml(localizedText(marker.label, locale))}</span></button></li>`,
      )
      .join(""),
    markers.length > 0,
  );
  out = fillElement(
    out,
    "case-parts",
    (parts ?? [])
      .map(
        (part) =>
          `<a href="${escapeHtml(localizedPath(locale, casePathname(part.case)))}"${part.case === row.id ? ' aria-current="page"' : ""}>${escapeHtml(localizedText(part.label, locale))}</a>`,
      )
      .join(""),
    (parts ?? []).length > 0,
  );
  return out;
}

/** The page opens as a case: the live controls stay hidden until the
 * shell lifts the flag (`index.html`'s inline script says the same from
 * the path). */
function openingAsCase(html: string): string {
  return html.replace(/<html lang="([^"]*)"/, '<html lang="$1" data-case-opening=""');
}

/** The no-script note of a case's page: what the case is, its facts and its
 * markers in words — what the map shows once it runs. */
function caseNoscript(row: ShowcaseCase, parts: readonly { case: string; label: Record<string, string> }[] | null, locale: Locale): string {
  const facts = caseFacts(row, locale);
  const markers = (row.markers ?? []).map((marker) => markerText(marker, row, locale)).join("");
  const partLinks = (parts ?? [])
    .filter((part) => part.case !== row.id)
    .map((part) => `<a href="${escapeHtml(localizedPath(locale, casePathname(part.case)))}">${escapeHtml(localizedText(part.label, locale))}</a>`)
    .join(" · ");
  return `<noscript>
      <div class="noscript-note">
        <p><strong>${escapeHtml(localizedText(row.title, locale))}</strong></p>
        <p>${escapeHtml(localizedText(row.summary, locale))}</p>
        <dl>${definitions(facts)}</dl>${markers ? `\n        <ul>${markers}</ul>` : ""}${partLinks ? `\n        <p>${partLinks}</p>` : ""}
        <p><a href="${escapeHtml(localizedPath(locale, SHOWCASE_PATH))}">${escapeHtml(say(locale, "showcaseBack"))}</a> · <a href="/llms.txt">llms.txt</a></p>
      </div>
    </noscript>`;
}

/** The end of a case's axis, ISO, for the dataset's temporal coverage. */
function axisEnd(row: ShowcaseCase): string {
  return new Date(Date.parse(row.runTime) + row.forecastHours * 3600_000).toISOString().replace(/\.000Z$/, "Z");
}

/** The viewer as one case's page in a language: the case's head (its
 * title, its summary as the description, canonical, hreflang, a
 * BreadcrumbList and a `Dataset`), the case panel filled in and the
 * no-script note saying what the case is. */
export function renderCasePage(
  indexHtml: string,
  row: ShowcaseCase,
  catalog: readonly ShowcaseCase[],
  locale: Locale,
  provenance?: DatasetProvenance,
): string {
  const path = casePathname(row.id);
  const title = localizedText(row.title, locale);
  const summary = localizedText(row.summary, locale);
  const parts = caseParts(catalog, row.id);
  let html = locale === "en" ? indexHtml : translateMarkup(indexHtml, locale);
  html = writeTitleBlock(html, row.modelId, row.defaultVariable, locale);
  html = markCurrentDataset(html, null);
  html = writeGraph(html, []);
  html = prerenderCasePanel(html, row, parts, locale);
  html = replaceOnce(html, NOSCRIPT_NOTE, () => caseNoscript(row, parts, locale), "noscript note");
  const [west, south, east, north] = row.bbox;
  const breadcrumb = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { name: SITE_NAME, path: "/" },
      { name: say(locale, "showcaseLink"), path: SHOWCASE_PATH },
      { name: title, path },
    ].map((crumb, index) => ({ "@type": "ListItem", position: index + 1, name: crumb.name, item: pageUrl(crumb.path, locale) })),
  };
  const dataset = {
    "@context": "https://schema.org",
    "@type": "Dataset",
    "@id": `${pageUrl(path, locale)}#dataset`,
    name: title,
    description: summary,
    url: pageUrl(path, locale),
    ...provenanceNodes(provenance),
    inLanguage: localeHtmlLang(locale),
    temporalCoverage: `${row.runTime}/${axisEnd(row)}`,
    spatialCoverage: { "@type": "Place", geo: { "@type": "GeoShape", box: `${south} ${west} ${north} ${east}` } },
    variableMeasured: row.variables,
    measurementTechnique: `${row.model} ${row.product}`,
    ...(row.tags?.length ? { keywords: row.tags } : {}),
    ...(row.credit ? { creditText: row.credit } : {}),
    isAccessibleForFree: true,
    isPartOf: { "@id": SITE_DATASET_ID },
  };
  return writeHead(html, {
    path,
    locale,
    title: `${title} · ${SITE_NAME}`,
    description: shortDescription(summary),
    jsonLd: [breadcrumb, dataset],
  });
}

// --- the list -----------------------------------------------------------------------

/** One case's card, as `showcase.ts::buildCard` builds it (the thumbnail
 * is painted at run time). */
function caseCard(row: ShowcaseCase, locale: Locale): string {
  const title = localizedText(row.title, locale);
  const summary = localizedText(row.summary, locale);
  const href = localizedPath(locale, casePathname(row.id));
  return `<li class="showcase-card"><a href="${escapeHtml(href)}" aria-label="${escapeHtml(say(locale, "showcaseCaseAria", { title }))}"><div class="showcase-thumb"><canvas aria-hidden="true"></canvas><span class="showcase-code">${escapeHtml(row.model)}</span></div><div class="showcase-body"><h2>${escapeHtml(title)}</h2><p class="showcase-summary" title="${escapeHtml(summary)}">${escapeHtml(summary)}</p><dl class="showcase-facts">${definitions(caseFacts(row, locale), new Set([row.eventTime ? 3 : 2]))}</dl><div class="showcase-foot"><span class="showcase-play" aria-hidden="true"></span></div></div></a></li>`;
}

/** The list in a language with the catalog's cards in it, and the cards'
 * ItemList, as `showcase.ts` writes them once the catalog loads. */
export function renderShowcaseList(showcaseHtml: string, cases: readonly ShowcaseCase[], locale: Locale): string {
  let html = localizedPage(showcaseHtml, locale, SHOWCASE_PATH, "showcaseHeading");
  if (cases.length === 0) return html;
  html = replaceOnce(
    html,
    /(<ul id="showcase-list"[^>]*>)(<\/ul>)/,
    (match) => match.replace(/><\/ul>$/, `>${cases.map((row) => caseCard(row, locale)).join("")}</ul>`),
    "showcase list",
  );
  html = html.replace(/(<p id="showcase-status"[^>]*?)>/, "$1 hidden>");
  const list = {
    "@context": "https://schema.org",
    "@type": "ItemList",
    "@id": `${pageUrl(SHOWCASE_PATH, locale)}#cases`,
    name: `Historical Weather Cases · ${SITE_NAME}`,
    numberOfItems: cases.length,
    itemListElement: cases.map((row, index) => ({
      "@type": "ListItem",
      position: index + 1,
      url: pageUrl(casePathname(row.id), locale),
      name: localizedText(row.title, locale),
      description: localizedText(row.summary, locale),
    })),
  };
  return html.replace(/<\/head>/, `  <script type="application/ld+json" id="showcase-cases">${scriptJson(list)}</script>\n  </head>`);
}

// --- the whole site ---------------------------------------------------------------------

export interface SitePagesInput {
  /** The built pages, by file name: `index.html`, `showcase.html`, … */
  built: Readonly<Record<string, string>>;
  /** The datasets with a page, and each one's provenance. */
  models: readonly { id: ForecastModelId; provenance?: DatasetProvenance }[];
  /** Any dataset's provenance, a case-only one's included. */
  provenanceOf?: (id: ForecastModelId) => DatasetProvenance | undefined;
  /** The published catalog's cases (`validateCatalog`), or none. */
  cases: readonly ShowcaseCase[];
  /** The day the catalog was written (`YYYY-MM-DD`), for `lastmod`. */
  catalogDay: string | null;
}

export interface SitePages {
  files: { fileName: string; source: string }[];
  /** Absolute addresses, for the sitemap, in its order: per page, every
   * language. */
  urls: string[];
  lastmod: Record<string, string>;
}

/** The day what the datasets', the cases' and the list's pages say last
 * changed by the build's own hand: a title, a description, the structured
 * data, the links. Moved with such a change, as the sitemap's `lastmod` is
 * worth only what it is true. */
export const PAGES_REVISED = "2026-11-25";

/** Every language's copy of every shell page. */
export function buildSitePages({ built, models, provenanceOf, cases, catalogDay }: SitePagesInput): SitePages {
  const page = (name: string): string => {
    const html = built[name];
    if (html === undefined) throw new Error(`site pages: the build emitted no ${name}`);
    return absolutizeUrls(html);
  };
  const index = page("index.html");
  const showcase = page("showcase.html");
  const listed = listedCases(cases);
  const files: SitePages["files"] = [];
  const urls: string[] = [];
  const lastmod: Record<string, string> = {};
  const add = (path: string, render: (locale: Locale) => string, day: string | null = null) => {
    for (const locale of LOCALE_CODES) {
      files.push({ fileName: pageFileName(locale, path), source: render(locale) });
      const url = pageUrl(path, locale);
      urls.push(url);
      if (day !== null) lastmod[url] = day;
    }
  };
  const modelIds = models.map(({ id }) => id);
  // A case's page changes with the catalog and with the build.
  const revised = catalogDay !== null && catalogDay > PAGES_REVISED ? catalogDay : PAGES_REVISED;
  add("/", (locale) => localizedPage(index, locale, "/", "pageTitleHome"));
  for (const { id, provenance } of models) add(`/${id}`, (locale) => renderModelPage(index, id, provenance, locale, modelIds), PAGES_REVISED);
  add(SHOWCASE_PATH, (locale) => renderShowcaseList(showcase, listed, locale), revised);
  for (const row of cases) add(casePathname(row.id), (locale) => renderCasePage(index, row, cases, locale, provenanceOf?.(row.modelId)), revised);
  for (const { file, path, title } of SHELL_PAGE_FILES) add(path, (locale) => localizedPage(page(file), locale, path, title));
  return { files, urls, lastmod };
}

/** The pages the sitemap leaves out: the API console is a tool, not a
 * page a search lands on. */
export const UNLISTED_PATHS: ReadonlySet<string> = new Set(["/api"]);

// --- the servers ----------------------------------------------------------------------------

/** Vite plugin: the language chooser (`sitepaths.ts::LANGUAGE_CHOOSER`)
 * right after every page's `<meta charset>` (which must stay in the first
 * kilobyte), before anything else loads; the other languages' copies drop
 * it (`writeHead`). */
export function languageChooser(): Plugin {
  return {
    name: "xue:language-chooser",
    transformIndexHtml: {
      order: "pre",
      handler(html) {
        // A page without the shell's charset line (the skew-T dev page) is
        // a tool with no languages of its own.
        return html.replace(/<meta charset="UTF-8" \/>/, (charset) => `${charset}\n    <script data-locale-chooser>${LANGUAGE_CHOOSER}</script>`);
      },
    },
  };
}

/** The source page (dev) or built file (preview) a path is served from, as
 * Pages serves it, or null for a path the servers answer themselves. */
export function sitePathTarget(pathname: string, modelIds: ReadonlySet<string>, built: boolean): string | null {
  const { locale, path, prefixed } = splitLocalePath(pathname);
  const viewer = path === "/" || path === "/index.html" || caseFromPath(path) !== null || modelFromPathname(path, modelIds) !== null;
  const shellPage = SHELL_PAGES.includes(path);
  if (!viewer && !shellPage) return null;
  if (!prefixed && (path === "/" || path === "/index.html")) return null;
  if (!built) return viewer ? "/index.html" : `${path}.html`;
  return `/${pageFileName(locale, path === "/index.html" ? "/" : path)}`;
}

/** Vite plugin: serve every language's copy of a page, a dataset's path
 * and a case's on the dev and preview servers the way Pages does — the
 * source page on the dev server, the built copy on the preview server
 * (the viewer's own for a case the build had no catalog entry for). */
export function sitePathRewrite(modelIds: readonly string[]): Plugin {
  const ids = new Set(modelIds);
  const rewrite =
    (built: boolean, outDir: string | null): Connect.NextHandleFunction =>
    (req, _res, next) => {
      if (req.url) {
        const url = new URL(req.url, "http://localhost");
        let target = sitePathTarget(url.pathname, ids, built);
        if (target !== null && outDir !== null) {
          const file = normalize(join(outDir, target));
          if (!existsSync(file) || !statSync(file).isFile()) target = caseFromPath(splitLocalePath(url.pathname).path) !== null ? "/index.html" : target;
        }
        if (target !== null) req.url = `${target}${url.search}`;
      }
      next();
    };
  return {
    name: "xue:site-paths",
    configureServer(server) {
      server.middlewares.use(rewrite(false, null));
    },
    configurePreviewServer(server) {
      server.middlewares.use(rewrite(true, join(server.config.root, server.config.build.outDir)));
    },
  };
}
