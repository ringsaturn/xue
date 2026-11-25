/** What a dataset's own page (`/<id>`, `modelpaths.ts`) says about itself
 * in the head: its title, its description and a schema.org `Dataset`.
 *
 * Read by the build, which writes each page's static head from it
 * (`web/tooling/modelpages.ts`), and by the shell, which keeps the head true
 * once the view is known (`pagemeta.ts`). Every statement is read off the
 * dataset table (`manifest.ts`, whose identity is the registry's): the
 * label, the product, whether it is a forecast, an observation window, a
 * mosaic or daily imagery, the frame step, the region, the core layers and
 * the live pointer; the producer and grid are the title block's line
 * (`MODEL_EYEBROW`), and the producers, the licence and the longer English
 * description come from the source's STAC prose, which the build passes in
 * (`DatasetProvenance`). Nothing else is written. */

import { localeHtmlLang, localeMessages, type Locale, type MessageKey } from "./i18n";
import { FORECAST_MODELS, modelRailCore, mosaicMembers, type ForecastModelId } from "./manifest";
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

/** Where a dataset is: its region's corners, or worldwide for one with none
 * (the table's mark of a global grid). */
function coverageText(region: readonly [number, number, number, number] | undefined, locale: Locale): string {
  if (region === undefined) return localeMessages(locale).metaWorldwide;
  const [west, south, east, north] = region;
  const lon = (value: number) => degrees(wrapLongitude(value), "E", "W");
  return `${lon(west)}–${lon(east)}, ${degrees(south, "N", "S")}–${degrees(north, "N", "S")}`;
}

/** The schema.org box, "south west north east". */
function coverageBox(region: readonly [number, number, number, number] | undefined): string {
  if (region === undefined) return "-90 -180 90 180";
  const [west, south, east, north] = region;
  return `${south} ${wrapLongitude(west)} ${north} ${wrapLongitude(east)}`;
}

type Kind = "forecast" | "observation" | "nowcast" | "mosaic" | "imagery";

function kindOf(id: ForecastModelId): Kind {
  const info = FORECAST_MODELS[id];
  if (info.imagery) return "imagery";
  if (info.mosaic) return "mosaic";
  if (info.leadsClock) return "nowcast";
  return info.observation ? "observation" : "forecast";
}

const TITLE_KEY: Record<Kind, MessageKey> = {
  forecast: "pageTitleModelForecast",
  observation: "pageTitleModelObservation",
  nowcast: "pageTitleModelNowcast",
  mosaic: "pageTitleModelMosaic",
  imagery: "pageTitleModelImagery",
};

/** Each dataset's producer, name and grid, as the title block shows it. */
const MODEL_EYEBROW: Record<ForecastModelId, string> = {
  gfs: "NOAA / GFS (0.25°)",
  ecmwf: "ECMWF / IFS (0.25°)",
  ifs: "ECMWF / IFS (0.1°)",
  aifs: "ECMWF / AIFS SINGLE (0.25°)",
  ifshres: "ECMWF / IFS HRES (0.1°)",
  cfs: "NOAA / CFSv2 SEASONAL (0.94°)",
  sflux: "NOAA / GFS SFLUX (13 KM)",
  hrrr: "NOAA / HRRR CONUS (3 KM)",
  rrfs: "NOAA / RRFS CONUS (3 KM)",
  woofnam: "RECAST / WOOF-NAM NORTH AMERICA (12 KM)",
  iconeu: "DWD / ICON-EU EUROPE (0.0625°)",
  arpege: "MÉTÉO-FRANCE / ARPEGE (0.25°)",
  cmageps: "CMA / GRAPES GEPS ENSEMBLE (0.25°)",
  woof: "RECAST / WOOF WRF-ARW NEST FUJI",
  efas: "COPERNICUS EMS / EFAS v5 HRES (1′)",
  efaswb: "COPERNICUS EMS / EFAS v5 WATER BALANCE (1′)",
  era5: "COPERNICUS C3S / ECMWF ERA5 REANALYSIS (0.25°)",
  cams: "COPERNICUS CAMS / ECMWF AEROSOL ANALYSIS (0.4°)",
  gefsaero: "NOAA / GEFS-AEROSOLS (0.25°)",
  cmadust: "CMA / CMA-CW DUST ASIA (0.15°)",
  silam: "FMI / SILAM AIR QUALITY GLOBAL (0.2°)",
  cma: "CMA / RADAR MOSAIC (0.044°)",
  mrms: "NOAA / MRMS CONUS (0.02°)",
  mrms3d: "NOAA / MRMS 3D REFLECTIVITY CONUS (0.05°)",
  jma: "JMA / NOWCAST JAPAN (0.005°)",
  hkonowcast: "HKO / NOWCAST HONG KONG (0.019°)",
  himawari: "JMA / HIMAWARI-9 AHI (0.04°)",
  himawarijp: "JMA / HIMAWARI-9 AHI JAPAN (0.01°)",
  goeseast: "NOAA / GOES-19 ABI EAST (0.04°)",
  goeswest: "NOAA / GOES-18 ABI WEST (0.04°)",
  meteosat: "EUMETSAT / METEOSAT-12 FCI (0.04°, HOURLY)",
  aurora: "NOAA SWPC / OVATION (1°)",
  geo: "GEO MOSAIC",
  gibs: "NASA GIBS / WORLDVIEW DAILY (300 M)",
};

/** The eyebrow of a dataset; one a case admitted (`registerCaseDataset`)
 * has no line here and shows its own label. */
export function modelEyebrowText(model: ForecastModelId): string {
  return (MODEL_EYEBROW as Partial<Record<string, string>>)[model] ?? FORECAST_MODELS[model].label;
}

/** A field's name in `locale` (`varLabel…`), or null for one whose label is
 * composed at run time (a level, a probability, a unit). */
export function fieldLabel(id: string, locale: Locale): string | null {
  const key = `varLabel${id.charAt(0).toUpperCase()}${id.slice(1)}`;
  const label = (localeMessages(locale) as Record<string, string>)[key];
  return label === undefined || label.includes("{") ? null : label;
}

const DESCRIPTION_KEY: Record<Kind, MessageKey> = {
  forecast: "metaModelForecast",
  observation: "metaModelObservation",
  nowcast: "metaModelNowcast",
  mosaic: "metaModelMosaic",
  imagery: "metaModelImagery",
};

function describe(id: ForecastModelId, kind: Kind, locale: Locale): string {
  const info = FORECAST_MODELS[id];
  const params: Record<string, string> = {
    name: modelEyebrowText(id),
    where: coverageText(info.region, locale),
    // Every observation window has a cadence (`SourceSpec.cadence_seconds`).
    minutes: String(Math.round((info.cadenceSeconds ?? 0) / 60)),
    members: mosaicMembers(id).map((member) => member.label).join(", "),
    layers: modelRailCore(id).map((field) => fieldLabel(field, locale)).filter((label) => label !== null).join(" · "),
  };
  return localeMessages(locale)[DESCRIPTION_KEY[kind]].replace(/\{(\w+)\}/g, (match, key: string) => params[key] ?? match);
}

/** Who produced a dataset and under what licence, as its STAC prose says
 * (`xuebuild/stac_prose.json`, read by the build tooling: the shell never
 * bundles it, and needs only the description). */
export interface DatasetProvenance {
  /** The licences' URLs, where the prose names them; a mosaic's members may
   * each have their own. */
  licenses: readonly string[];
  /** The providers with the `producer` role. */
  producers: readonly { name: string; url?: string }[];
  /** The prose's own description, English. */
  description?: string;
}

/** The schema.org properties a dataset's provenance states: its producers
 * as `creator`, its licence, and Xue, which processes and hosts the data,
 * as `publisher` and `provider`. Without one Xue is the creator and no
 * licence is claimed. */
export function provenanceNodes(provenance: DatasetProvenance | undefined): Record<string, unknown> {
  const producers = provenance?.producers ?? [];
  const licenses = provenance?.licenses ?? [];
  return {
    creator:
      producers.length > 0
        ? producers.map(({ name, url }) => ({ "@type": "Organization", name, ...(url === undefined ? {} : { url }) }))
        : { "@id": PERSON_ID },
    publisher: { "@id": PERSON_ID },
    ...(provenance ? { provider: { "@id": PERSON_ID } } : {}),
    ...(licenses.length === 0 ? {} : { license: licenses.length === 1 ? licenses[0] : licenses }),
  };
}

/** The head of a dataset's page in `locale`. */
export function modelPageHead(id: ForecastModelId, provenance?: DatasetProvenance, locale: Locale = "en"): ModelPageHead {
  const info = FORECAST_MODELS[id];
  const kind = kindOf(id);
  const path = `/${id}`;
  const url = `${SITE_ORIGIN}${path}`;
  const title = localeMessages(locale)[TITLE_KEY[kind]].replace("{model}", info.label);
  const description = describe(id, kind, locale);
  const dataset: Record<string, unknown> = {
    "@type": "Dataset",
    "@id": `${url}#dataset`,
    name: `${info.label} (${info.product}) on ${SITE_NAME}`,
    description: locale === "en" && provenance?.description ? `${description} ${provenance.description}` : description,
    url,
    isAccessibleForFree: true,
    isPartOf: { "@id": SITE_DATASET_ID },
    ...provenanceNodes(provenance),
    inLanguage: localeHtmlLang(locale),
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
