/** What a dataset's own page (`/<id>`, `modelpaths.ts`) says about itself
 * in the head: its title, its description and a schema.org `Dataset`.
 *
 * Read by the build, which writes each page's static head from it
 * (`web/tooling/modelpages.ts`), and by the shell, which keeps the head true
 * once the view is known (`pagemeta.ts`). Every statement is read off the
 * dataset table (`manifest.ts`, whose identity is the registry's): the
 * label, the product, whether it is a forecast, an observation window, a
 * mosaic or daily imagery, the frame step, the region and the live pointer;
 * the producers and the licence come from the source's STAC prose, which
 * the build passes in (`DatasetProvenance`). Nothing else is written — no
 * resolution, no claim the two do not hold — and the copy is English, like
 * the static head of every page. */

import { FORECAST_MODELS, mosaicMembers, type ForecastModelId } from "./manifest";
import { DATA_ORIGIN, SITE_NAME, SITE_ORIGIN } from "./site";

/** The site's author node in the static JSON-LD graph (`index.html`), which
 * every dataset page keeps: the build checks the graph still carries it. */
export const PERSON_ID = "https://github.com/ringsaturn#person";
/** The site-wide dataset node in the same graph, which each page's dataset
 * is a part of. */
export const SITE_DATASET_ID = `${SITE_ORIGIN}/#data`;

export interface ModelPageHead {
  /** Root-relative path: `/<id>`. */
  path: string;
  /** The page title before ` · Xue`. */
  title: string;
  description: string;
  /** A schema.org `Dataset` node, without `@context`: the build adds it to
   * the page's `@graph`. */
  dataset: Record<string, unknown>;
}

function degrees(value: number, positive: string, negative: string): string {
  const rounded = Math.round(Math.abs(value) * 10) / 10;
  return `${rounded}°${value < 0 ? negative : positive}`;
}

/** A longitude in −180…180: a region past the antimeridian is spelled in
 * the grid's own copy of the world (200.7°E). */
function wrapLongitude(lon: number): number {
  if (lon >= -180 && lon <= 180) return lon;
  // Rounded to the table's own precision: the modulo leaves float dust.
  return Math.round((((((lon + 180) % 360) + 360) % 360) - 180) * 1e6) / 1e6;
}

/** Where a dataset is, in words: its region's corners, or worldwide for
 * one with none (the table's mark of a global grid). */
function coverageText(region: readonly [number, number, number, number] | undefined): string {
  if (region === undefined) return "worldwide";
  const [west, south, east, north] = region;
  const lon = (value: number) => degrees(wrapLongitude(value), "E", "W");
  return `over ${lon(west)}–${lon(east)}, ${degrees(south, "N", "S")}–${degrees(north, "N", "S")}`;
}

/** The schema.org box, "south west north east". */
function coverageBox(region: readonly [number, number, number, number] | undefined): string {
  if (region === undefined) return "-90 -180 90 180";
  const [west, south, east, north] = region;
  return `${south} ${wrapLongitude(west)} ${north} ${wrapLongitude(east)}`;
}

function stepText(seconds: number): string {
  if (seconds % 3600 === 0) return seconds === 3600 ? "hour" : `${seconds / 3600} hours`;
  if (seconds % 60 === 0) return seconds === 60 ? "minute" : `${seconds / 60} minutes`;
  return `${seconds} seconds`;
}

type Kind = "forecast" | "observation" | "nowcast" | "mosaic" | "imagery";

function kindOf(id: ForecastModelId): Kind {
  const info = FORECAST_MODELS[id];
  if (info.imagery) return "imagery";
  if (info.mosaic) return "mosaic";
  if (info.leadsClock) return "nowcast";
  return info.observation ? "observation" : "forecast";
}

const TITLE_NOUN: Record<Kind, string> = {
  forecast: "live forecast maps",
  observation: "live observation maps",
  nowcast: "live nowcast maps",
  mosaic: "live satellite mosaic",
  imagery: "daily imagery map",
};

function describe(id: ForecastModelId, kind: Kind): string {
  const info = FORECAST_MODELS[id];
  const where = coverageText(info.region);
  const tail = "played back as an animated map in your browser.";
  switch (kind) {
    case "imagery":
      return `${info.label} daily imagery on an interactive map in your browser, one picture a UTC day, ${where}.`;
    case "mosaic": {
      const members = mosaicMembers(id).map((member) => member.label);
      return `${members.join(", ")} as one picture: the latest frames of each disk, clipped to the longitudes it sees best, ${tail}`;
    }
    case "nowcast":
      return `The latest ${info.label} nowcast ${where}, its frames ahead of the clock, ${tail}`;
    case "observation": {
      const step = info.cadenceSeconds === undefined ? "" : `, a frame every ${stepText(info.cadenceSeconds)}`;
      return `The latest ${info.label} observations ${where}${step}, ${tail}`;
    }
    case "forecast":
      return `The newest ${info.label} forecast run ${where}, frame by frame, ${tail}`;
  }
}

/** Who produced a dataset and under what licence, as its STAC prose says
 * (`xuebuild/stac_prose.json`, read by the build tooling: the shell never
 * bundles it, and needs only the description). */
export interface DatasetProvenance {
  /** The licence's URL, when the prose names one. */
  license?: string;
  /** The providers with the `producer` role. */
  producers: readonly { name: string; url?: string }[];
}

/** The head of a dataset's page. With `provenance`, the producers are the
 * `creator` and the licence is named; Xue, which processes and hosts the
 * data, stays its `publisher` and `provider`. */
export function modelPageHead(id: ForecastModelId, provenance?: DatasetProvenance): ModelPageHead {
  const info = FORECAST_MODELS[id];
  const kind = kindOf(id);
  const path = `/${id}`;
  const url = `${SITE_ORIGIN}${path}`;
  const title = `${info.label} ${TITLE_NOUN[kind]}`;
  const description = describe(id, kind);
  const dataset: Record<string, unknown> = {
    "@type": "Dataset",
    "@id": `${url}#dataset`,
    name: `${info.label} (${info.product}) on ${SITE_NAME}`,
    description,
    url,
    isAccessibleForFree: true,
    isPartOf: { "@id": SITE_DATASET_ID },
    creator:
      provenance && provenance.producers.length > 0
        ? provenance.producers.map(({ name, url: home }) => ({
            "@type": "Organization",
            name,
            ...(home === undefined ? {} : { url: home }),
          }))
        : { "@id": PERSON_ID },
    publisher: { "@id": PERSON_ID },
    ...(provenance ? { provider: { "@id": PERSON_ID } } : {}),
    ...(provenance?.license === undefined ? {} : { license: provenance.license }),
    spatialCoverage: { "@type": "Place", geo: { "@type": "GeoShape", box: coverageBox(info.region) } },
    ...(info.latestFilename === undefined
      ? {}
      : {
          distribution: [
            {
              "@type": "DataDownload",
              name: `${info.label} live pointer`,
              encodingFormat: "application/json",
              contentUrl: new URL(info.latestFilename, DATA_ORIGIN).href,
            },
          ],
        }),
  };
  return { path, title, description, dataset };
}
