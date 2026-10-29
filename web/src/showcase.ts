import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./style.css";

import {
  applyStaticMessages,
  locale,
  onLocaleChange,
  t,
  type MessageKey,
} from "./i18n";
import { mountLanguagePicker } from "./sheet";
import { applyTheme, toggleTheme } from "./theme";
import { isObservationModel, parseBundleMetadata, type PosterDescriptor } from "./manifest";
import { buildPalette } from "./palettes";
import { fetchPoster, isPosterSupported } from "./poster";
import { applyPageMeta, pageUrl } from "./pagemeta";
import { fetchCaseManifest, fetchCatalog, listedCases, localizedText, type ShowcaseCase } from "./showcase-catalog";
import { dataBaseUrl, SITE_NAME } from "./site";
import { formatBytes, formatRegion, formatUtcHour } from "./format";
import { TcArchive, type ArchiveCatalog } from "./tc/archive";
import { archivePagePath } from "./tc/pages";
import { paintSeason } from "./tc/poster";

/**
 * The historical showcase list.
 *
 * A separate, static page: it reads the mutable `showcase.json` catalog and
 * renders one card per case, each linking into the ordinary viewer at
 * `./?case=<id>`. No decoder, no map — the only heavy thing it touches is
 * each case's first-frame poster, which it paints as the card's thumbnail
 * through the same `DecompressionStream` path the viewer uses (a poster is
 * under a kilobyte, so a card costs about as much as an icon).
 */

applyStaticMessages();
applyTheme();
// Title and description stay the markup's; this pins a `?lang=` rendering's
// canonical to itself and completes the hreflang set.
applyPageMeta({ path: "/showcase.html" });

const list = document.getElementById("showcase-list") as HTMLUListElement;
const status = document.getElementById("showcase-status") as HTMLParagraphElement;
document.getElementById("theme-toggle")?.addEventListener("click", () => toggleTheme());

// The language picker: the viewer's sheet, the viewer's rows, on this page's
// own trigger. A pick re-renders the cards in place; the thumbnails, already
// painted, are carried over rather than fetched again.
const langTrigger = document.getElementById("lang-toggle");
const langSheet = document.getElementById("lang-sheet");
const langList = document.getElementById("lang-list");
if (langTrigger && langSheet && langList) {
  onLocaleChange(mountLanguagePicker(langTrigger, langSheet, langList));
}

function definition(label: string, value: string, wide = false): HTMLElement {
  const item = document.createElement("div");
  if (wide) item.className = "is-wide";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  item.append(term, description);
  return item;
}

/** One card. The canvas is the thumbnail's, and a card rebuilt for another
 * language takes the one already painted. */
function buildCard(
  showcaseCase: ShowcaseCase,
  canvas: HTMLCanvasElement = document.createElement("canvas"),
): { item: HTMLLIElement; canvas: HTMLCanvasElement } {
  const item = document.createElement("li");
  item.className = "showcase-card";
  const link = document.createElement("a");
  link.href = `./?case=${encodeURIComponent(showcaseCase.id)}`;
  link.setAttribute("aria-label", t("showcaseCaseAria", { title: localizedText(showcaseCase.title, locale) }));

  const figure = document.createElement("div");
  figure.className = "showcase-thumb";
  canvas.setAttribute("aria-hidden", "true");
  // The dataset rides on the thumbnail, the way a contact sheet is stamped
  // rather than captioned; the case page lists its variables.
  const code = document.createElement("span");
  code.className = "showcase-code";
  code.textContent = showcaseCase.model;
  figure.append(canvas, code);

  const body = document.createElement("div");
  body.className = "showcase-body";

  const title = document.createElement("h2");
  title.textContent = localizedText(showcaseCase.title, locale);

  const summary = document.createElement("p");
  summary.className = "showcase-summary";
  summary.textContent = localizedText(showcaseCase.summary, locale);
  // The card shows two lines of it; the whole of it on hover.
  summary.title = summary.textContent;

  const facts = document.createElement("dl");
  facts.className = "showcase-facts";
  // Observations have no run cycle and no lead time: the run line is where
  // the series starts, and the range is how long it runs.
  const observations = isObservationModel(showcaseCase.modelId);
  if (showcaseCase.eventTime) facts.append(definition(t("showcaseEventLabel"), formatUtcHour(showcaseCase.eventTime)));
  facts.append(
    definition(t(observations ? "showcaseSeriesLabel" : "showcaseRunLabel"), formatUtcHour(showcaseCase.runTime)),
    definition(
      t(observations ? "showcaseSpanLabel" : "showcaseRangeLabel"),
      t("showcaseHours", { count: showcaseCase.forecastHours }),
    ),
    definition(t("showcaseRegionLabel"), formatRegion(showcaseCase.bbox), true),
    definition(t("showcaseGridLabel"), `${showcaseCase.grid.width}×${showcaseCase.grid.height}`),
    definition(t("showcaseSizeLabel"), formatBytes(showcaseCase.byteLength)),
  );

  body.append(title, summary, facts);
  // The foot pins the play affordance to the card's bottom edge, so a row of
  // cards ends on one line however long their summaries run.
  const foot = document.createElement("div");
  foot.className = "showcase-foot";
  const play = document.createElement("span");
  play.className = "showcase-play";
  play.setAttribute("aria-hidden", "true");
  foot.append(play);
  body.append(foot);

  link.append(figure, body);
  item.append(link);
  return { item, canvas };
}

/** The storm archive's card: always the list's first, opening the storm
 * map. Its thumbnail is the season running now, every track in its
 * intensity colors (`tc/poster.ts`); its facts are the archive's own. */
function buildStormCard(
  catalog: ArchiveCatalog,
  canvas: HTMLCanvasElement = document.createElement("canvas"),
): { item: HTMLLIElement; canvas: HTMLCanvasElement } {
  const item = document.createElement("li");
  item.className = "showcase-card is-pinned";
  const link = document.createElement("a");
  link.href = "./typhoon.html";
  link.setAttribute("aria-label", t("showcaseCaseAria", { title: t("tcHistoryHeading") }));
  const seasons = catalog.seasons.map((entry) => entry.season);
  const span = seasons.length ? `${Math.min(...seasons)}–${Math.max(...seasons)}` : "";

  const figure = document.createElement("div");
  figure.className = "showcase-thumb";
  canvas.setAttribute("aria-hidden", "true");
  const code = document.createElement("span");
  code.className = "showcase-code";
  code.textContent = span;
  figure.append(canvas, code);

  const body = document.createElement("div");
  body.className = "showcase-body";
  const title = document.createElement("h2");
  title.textContent = t("tcHistoryHeading");
  const summary = document.createElement("p");
  summary.className = "showcase-summary";
  summary.textContent = t("tcHistoryIntro");
  const facts = document.createElement("dl");
  facts.className = "showcase-facts";
  facts.append(
    definition(t("tcCardSeasons"), span),
    definition(t("tcCardStorms"), String(catalog.seasons.reduce((sum, entry) => sum + entry.storms, 0))),
    definition(t("tcPageForecasts"), String(catalog.months.reduce((sum, entry) => sum + entry.records, 0))),
  );
  body.append(title, summary, facts);
  const foot = document.createElement("div");
  foot.className = "showcase-foot";
  // The tags' place holds the link to the static season pages, a sibling
  // of the card's own link (a link cannot hold another) laid over it.
  const spacer = document.createElement("span");
  const play = document.createElement("span");
  play.className = "showcase-play";
  play.setAttribute("aria-hidden", "true");
  foot.append(spacer, play);
  body.append(foot);
  link.append(figure, body);
  const browse = document.createElement("a");
  browse.className = "showcase-card-link";
  browse.href = archivePagePath(locale);
  browse.textContent = `${t("tcCardBrowse")} →`;
  item.append(link, browse);
  return { item, canvas };
}

/** The archive, once its catalog has loaded, and the storm card's
 * thumbnail, kept across a language switch like the cases'. */
const stormArchive = new TcArchive(dataBaseUrl());
let stormCatalog: ArchiveCatalog | null = null;
let stormCanvas: HTMLCanvasElement | null = null;

/** Paint the season running now (not the southern one already named after
 * next year), else the latest; best effort, like the posters. */
async function paintStormCard(canvas: HTMLCanvasElement, catalog: ArchiveCatalog): Promise<void> {
  const year = new Date().getUTCFullYear();
  const seasons = catalog.seasons.map((entry) => entry.season).sort((a, b) => b - a);
  const season = seasons.find((item) => item <= year) ?? seasons[0];
  if (season === undefined) return;
  const overview = await stormArchive.overview(season);
  if (!overview) return;
  canvas.width = 640;
  canvas.height = 360;
  if (paintSeason(canvas, overview)) canvas.classList.add("is-painted");
}

/** Paint one case's first frame into its card. Best effort: a card without a
 * thumbnail is still a complete card, so any failure just leaves the
 * placeholder in place. */
async function paintThumbnail(showcaseCase: ShowcaseCase, canvas: HTMLCanvasElement): Promise<void> {
  if (!isPosterSupported()) return;
  const loaded = await fetchCaseManifest(dataBaseUrl(), showcaseCase);
  // The wind bundle ships no poster, so a wind-first case falls back to
  // whichever bundle has one.
  const bundle =
    loaded.manifest.bundles.find((item) => item.variable === showcaseCase.defaultVariable && item.poster) ??
    loaded.manifest.bundles.find((item) => item.poster);
  const poster = bundle?.poster as PosterDescriptor | undefined;
  if (!poster) return;
  const url = new URL(poster.path, loaded.manifestUrl);
  url.searchParams.set("v", poster.crc32);
  const [codes, metadata] = [await fetchPoster(url.href, poster), parseBundleMetadata(poster.metadataJson)];
  const palette = buildPalette(metadata.variables[0]!);
  const context = canvas.getContext("2d");
  if (!context) return;
  canvas.width = poster.width;
  canvas.height = poster.height;
  const image = context.createImageData(poster.width, poster.height);
  for (let index = 0; index < codes.length; index += 1) {
    const entry = codes[index]! * 4;
    image.data[index * 4] = palette[entry]!;
    image.data[index * 4 + 1] = palette[entry + 1]!;
    image.data[index * 4 + 2] = palette[entry + 2]!;
    image.data[index * 4 + 3] = palette[entry + 3]!;
  }
  context.putImageData(image, 0, 0);
  canvas.classList.add("is-painted");
}

/** The cases as structured data — an ItemList of the pages they open — so
 * an indexer that ran the page sees the same list the cards show. One
 * element per load, rewritten when the cards are (a language switch). */
let caseListScript: HTMLScriptElement | null = null;

function publishCaseList(cases: readonly ShowcaseCase[]): void {
  const script = caseListScript ?? document.createElement("script");
  script.type = "application/ld+json";
  script.textContent = JSON.stringify({
    "@context": "https://schema.org",
    "@type": "ItemList",
    "@id": `${pageUrl("/showcase.html", null)}#cases`,
    name: `Historical Weather Cases · ${SITE_NAME}`,
    numberOfItems: cases.length,
    itemListElement: cases.map((showcaseCase, index) => ({
      "@type": "ListItem",
      position: index + 1,
      url: pageUrl(`/?case=${encodeURIComponent(showcaseCase.id)}`, null),
      name: localizedText(showcaseCase.title, locale),
      description: localizedText(showcaseCase.summary, locale),
    })),
  });
  if (!caseListScript) {
    caseListScript = script;
    document.head.appendChild(script);
  }
}

/** The catalog once it has loaded, and each case's thumbnail canvas, so
 * the cards can be built again in another language. */
let cases: readonly ShowcaseCase[] = [];
const thumbnails = new Map<string, HTMLCanvasElement>();

/** The status line's message, kept so a language switch can say it again. */
let statusMessage: { key: MessageKey; params?: Record<string, string | number> } | null = null;

function setStatus(key: MessageKey, params?: Record<string, string | number>): void {
  statusMessage = { key, params };
  status.textContent = t(key, params);
}

function renderCards(): void {
  const cards = cases.map((showcaseCase) => ({ showcaseCase, ...buildCard(showcaseCase, thumbnails.get(showcaseCase.id)) }));
  for (const card of cards) thumbnails.set(card.showcaseCase.id, card.canvas);
  const pinned = stormCatalog ? buildStormCard(stormCatalog, stormCanvas ?? undefined) : null;
  if (pinned) stormCanvas = pinned.canvas;
  list.replaceChildren(...(pinned ? [pinned.item] : []), ...cards.map((card) => card.item));
  publishCaseList(cases);
}

async function render(): Promise<void> {
  try {
    const catalog = await fetchCatalog(dataBaseUrl());
    if (catalog.cases.length === 0) {
      setStatus("showcaseEmpty");
      return;
    }
    status.hidden = true;
    cases = listedCases(catalog.cases);
    renderCards();
    // Thumbnails are posters — under a kilobyte each — so the handful a
    // catalog holds can all be fetched at once.
    await Promise.allSettled(cases.map((showcaseCase) => paintThumbnail(showcaseCase, thumbnails.get(showcaseCase.id)!)));
  } catch (error) {
    status.hidden = false;
    status.classList.add("is-error");
    setStatus("showcaseLoadFailed", { message: error instanceof Error ? error.message : String(error) });
  }
}

onLocaleChange(() => {
  applyPageMeta({ path: "/showcase.html" });
  if (statusMessage) setStatus(statusMessage.key, statusMessage.params);
  if (cases.length > 0 || stormCatalog) renderCards();
});

void render();
// The storm card stands apart from the case catalog: it is placed when the
// archive answers, and a data root without one simply has no such card.
stormArchive
  .catalog()
  .then(async (catalog) => {
    stormCatalog = catalog;
    renderCards();
    await paintStormCard(stormCanvas!, catalog);
  })
  .catch((error: unknown) => console.warn("tc archive card not shown", error));
