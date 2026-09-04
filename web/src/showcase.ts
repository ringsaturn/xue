import "@fontsource/barlow-condensed/500.css";
import "@fontsource/barlow-condensed/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "./style.css";

import { applyStaticMessages, locale, t, toggleLocale } from "./i18n";
import { isObservationModel, parseBundleMetadata, type ForecastBundleId, type PosterDescriptor } from "./manifest";
import { buildPalette } from "./palettes";
import { fetchPoster, isPosterSupported } from "./poster";
import { fetchCaseManifest, fetchCatalog, localizedText, type ShowcaseCase } from "./showcase-catalog";

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

const list = document.getElementById("showcase-list") as HTMLUListElement;
const status = document.getElementById("showcase-status") as HTMLParagraphElement;
document.getElementById("lang-toggle")?.addEventListener("click", () => toggleLocale());

function dataBaseUrl(): string {
  return import.meta.env.VITE_DATA_BASE_URL || "data/";
}

/** Variable codes, matching the viewer's own switch labels. */
const VARIABLE_CODE: Record<ForecastBundleId, string> = {
  tmp2m: "TEMP",
  prate: "PRECIP",
  dswrf: "SOLAR",
  cref: "RADAR",
  wind10m: "WIND",
};

/** Compact UTC stamp. Cards line several of these up in narrow columns, so
 * they stay in the fixed ISO-like shape rather than a locale long form. */
function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "--";
  const pad = (item: number) => String(item).padStart(2, "0");
  return `${parsed.getUTCFullYear()}-${pad(parsed.getUTCMonth() + 1)}-${pad(parsed.getUTCDate())} ${pad(parsed.getUTCHours())}Z`;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.round(bytes / 1e3)} KB`;
}

function formatDegrees(value: number, axis: "NS" | "EW"): string {
  const hemisphere = axis === "NS" ? (value >= 0 ? "N" : "S") : value >= 0 ? "E" : "W";
  return `${Math.abs(value).toFixed(Math.abs(value) % 1 === 0 ? 0 : 1)}°${hemisphere}`;
}

function regionLabel(showcaseCase: ShowcaseCase): string {
  const [west, south, east, north] = showcaseCase.bbox;
  return `${formatDegrees(north, "NS")} ${formatDegrees(west, "EW")} → ${formatDegrees(south, "NS")} ${formatDegrees(east, "EW")}`;
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

function buildCard(showcaseCase: ShowcaseCase): { item: HTMLLIElement; canvas: HTMLCanvasElement } {
  const item = document.createElement("li");
  item.className = "showcase-card";
  const link = document.createElement("a");
  link.href = `./?case=${encodeURIComponent(showcaseCase.id)}`;
  link.setAttribute("aria-label", t("showcaseCaseAria", { title: localizedText(showcaseCase.title, locale) }));

  const figure = document.createElement("div");
  figure.className = "showcase-thumb";
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-hidden", "true");
  figure.append(canvas);

  const body = document.createElement("div");
  body.className = "showcase-body";

  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = `${showcaseCase.model} · ${showcaseCase.variables.map((id) => VARIABLE_CODE[id]).join(" / ")}`;

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
  if (showcaseCase.eventTime) facts.append(definition(t("showcaseEventLabel"), formatDate(showcaseCase.eventTime)));
  facts.append(
    definition(t(observations ? "showcaseSeriesLabel" : "showcaseRunLabel"), formatDate(showcaseCase.runTime)),
    definition(
      t(observations ? "showcaseSpanLabel" : "showcaseRangeLabel"),
      t("showcaseHours", { count: showcaseCase.forecastHours }),
    ),
    definition(t("showcaseRegionLabel"), regionLabel(showcaseCase), true),
    definition(t("showcaseGridLabel"), `${showcaseCase.grid.width}×${showcaseCase.grid.height}`),
    definition(t("showcaseSizeLabel"), formatBytes(showcaseCase.byteLength)),
  );

  body.append(eyebrow, title, summary, facts);
  if (showcaseCase.tags?.length) {
    const tags = document.createElement("ul");
    tags.className = "showcase-tags";
    for (const tag of showcaseCase.tags) {
      const tagItem = document.createElement("li");
      tagItem.textContent = tag;
      tags.append(tagItem);
    }
    body.append(tags);
  }

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

async function render(): Promise<void> {
  try {
    const catalog = await fetchCatalog(dataBaseUrl());
    if (catalog.cases.length === 0) {
      status.textContent = t("showcaseEmpty");
      return;
    }
    status.hidden = true;
    const cards = catalog.cases.map((showcaseCase) => ({ showcaseCase, ...buildCard(showcaseCase) }));
    list.replaceChildren(...cards.map((card) => card.item));
    // Thumbnails are posters — under a kilobyte each — so the handful a
    // catalog holds can all be fetched at once.
    await Promise.allSettled(cards.map((card) => paintThumbnail(card.showcaseCase, card.canvas)));
  } catch (error) {
    status.hidden = false;
    status.classList.add("is-error");
    status.textContent = t("showcaseLoadFailed", {
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

void render();
