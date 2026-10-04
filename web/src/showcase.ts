import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
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
import { isObservationModel, parseBundleMetadata, type ForecastBundleId, type PosterDescriptor } from "./manifest";
import { buildPalette } from "./palettes";
import { fetchPoster, isPosterSupported } from "./poster";
import { applyPageMeta, pageUrl } from "./pagemeta";
import { fetchCaseManifest, fetchCatalog, localizedText, type ShowcaseCase } from "./showcase-catalog";
import { dataBaseUrl, SITE_NAME } from "./site";
import { formatRegion, formatUtcHour } from "./format";
import { variableSpec } from "./variables";

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

/** The contact-sheet code of one bundle (`VariableSpec.showcaseCode`,
 * matching the viewer's own switch labels): the surface fields have a
 * word, every isobaric field is its family and level ("HGT 500MB",
 * "T 850MB"); a bundle the table lacks is written under its own id. */
function variableCode(id: ForecastBundleId): string {
  return variableSpec(id)?.showcaseCode ?? id.toUpperCase();
}

function formatBytes(bytes: number): string {
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.round(bytes / 1e3)} KB`;
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
  // The dataset code rides on the thumbnail, the way a contact sheet is
  // stamped rather than captioned.
  const code = document.createElement("span");
  code.className = "showcase-code";
  code.textContent = `${showcaseCase.model} · ${showcaseCase.variables.map((id) => variableCode(id)).join(" / ")}`;
  figure.append(canvas, code);

  const body = document.createElement("div");
  body.className = "showcase-body";

  const title = document.createElement("h2");
  title.textContent = localizedText(showcaseCase.title, locale);

  const summary = document.createElement("p");
  summary.className = "showcase-summary";
  summary.textContent = localizedText(showcaseCase.summary, locale);

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
  // The foot pins the tags and the play affordance to the card's bottom edge,
  // so a row of cards ends on one line however long their summaries run.
  const foot = document.createElement("div");
  foot.className = "showcase-foot";
  const tags = document.createElement("ul");
  tags.className = "showcase-tags";
  for (const tag of showcaseCase.tags ?? []) {
    const tagItem = document.createElement("li");
    tagItem.textContent = tag;
    tags.append(tagItem);
  }
  const play = document.createElement("span");
  play.className = "showcase-play";
  play.setAttribute("aria-hidden", "true");
  foot.append(tags, play);
  body.append(foot);

  link.append(figure, body);
  item.append(link);
  return { item, canvas };
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
  list.replaceChildren(...cards.map((card) => card.item));
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
    cases = catalog.cases;
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
  if (cases.length > 0) renderCards();
});

void render();
