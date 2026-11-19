/** Which dataset a viewer URL names, and where a URL naming it the old way
 * belongs.
 *
 * The live viewer's dataset is the first path segment — `/hrrr?type=wind`,
 * `/himawari` — with `/` the home on the default model. A link from before
 * names it as `?model=<alias>` on `/`; the Pages Function at the site root
 * (`functions/_middleware.ts`) answers such a request with a 301 to the
 * path, and the shell does the same with `replaceState` where no function
 * runs (the dev server, a preview, an outage). Both call
 * `legacyModelLocation` below, so the two can never disagree.
 *
 * Nothing here imports at run time: the function bundles this module on
 * its own, and the manifest's graph (the registry, the locales) has no
 * business in a request handler. */

import type { ForecastModelId } from "./manifest";

/** The datasets in the model switch's order: the twelve live forecasts, the
 * eleven rolling observation windows (MRMS and its 3D volume, the JMA
 * and HKO nowcasts, the CMA mosaic, the four geostationary imagers and Himawari's
 * Japan box, and the SWPC aurora probability) and the geostationary
 * mosaic, a view over the imagers with no feed of its own. The switch
 * offers the ones index.html has a button for. A dataset with no pointer
 * that is not a mosaic (the WOOF nest, the two EFAS runs) is absent: it is
 * reached through its showcase cases alone. Each one here but `NO_PAGE_MODEL_IDS` has a
 * path of its own (`/<id>`) on a build that serves it. */
export const FORECAST_MODEL_IDS: readonly ForecastModelId[] = [
  "gfs",
  "sflux",
  "ecmwf",
  "ifs",
  "aifs",
  "ifshres",
  "cfs",
  "hrrr",
  "rrfs",
  "woofnam",
  "iconeu",
  "arpege",
  "cmageps",
  "gefsaero",
  "cmadust",
  "silam",
  "mrms",
  "mrms3d",
  "jma",
  "hkonowcast",
  "cma",
  "himawari",
  "himawarijp",
  "goeseast",
  "goeswest",
  "meteosat",
  "aurora",
  "geo",
  "gibs",
];

/** Accepted spellings for each model in `?model=`. The target is the
 * canonical id, the only spelling a URL (and a path) is written with.
 * Matching is case-insensitive. */
export const MODEL_ALIASES: Readonly<Record<string, ForecastModelId>> = {
  gfs: "gfs",
  ecmwf: "ecmwf",
  ifs: "ifs",
  "ifs-0p1": "ifs",
  aifs: "aifs",
  "aifs-single": "aifs",
  ifshres: "ifshres",
  hres: "ifshres",
  ifs9km: "ifshres",
  cfs: "cfs",
  cfsv2: "cfs",
  sflux: "sflux",
  "gfs-sflux": "sflux",
  hrrr: "hrrr",
  rrfs: "rrfs",
  woofnam: "woofnam",
  "woof-nam": "woofnam",
  iconeu: "iconeu",
  "icon-eu": "iconeu",
  arpege: "arpege",
  cmageps: "cmageps",
  "cma-geps": "cmageps",
  // The WOOF nest is cases only; its spellings are known so a link naming
  // it beside `?case=` reads as the case, not as a bad model.
  woof: "woof",
  wrf: "woof",
  // The two EFAS runs are cases only too, for the same reason.
  efas: "efas",
  "efas-v5": "efas",
  efaswb: "efaswb",
  "efas-wb": "efaswb",
  "efas-v5-wb": "efaswb",
  // And the ERA5 reanalysis and the CAMS aerosol analyses.
  era5: "era5",
  cams: "cams",
  gefsaero: "gefsaero",
  "gefs-aerosols": "gefsaero",
  gefsaerosols: "gefsaero",
  "gefs-chem": "gefsaero",
  cmadust: "cmadust",
  "cma-dust": "cmadust",
  "cma-cw-dust": "cmadust",
  silam: "silam",
  "silam-aq": "silam",
  mrms: "mrms",
  mrms3d: "mrms3d",
  "mrms-3d": "mrms3d",
  jma: "jma",
  hrpns: "jma",
  hkonowcast: "hkonowcast",
  "hko-nowcast": "hkonowcast",
  hko: "hkonowcast",
  cma: "cma",
  "cma-radar": "cma",
  radar: "cma",
  himawari: "himawari",
  "himawari-9": "himawari",
  himawari9: "himawari",
  ahi: "himawari",
  himawarijp: "himawarijp",
  "himawari-jp": "himawarijp",
  "himawari-japan": "himawarijp",
  goeseast: "goeseast",
  "goes-east": "goeseast",
  "goes-19": "goeseast",
  goes19: "goeseast",
  goeswest: "goeswest",
  "goes-west": "goeswest",
  "goes-18": "goeswest",
  goes18: "goeswest",
  meteosat: "meteosat",
  "meteosat-12": "meteosat",
  meteosat12: "meteosat",
  mtg: "meteosat",
  "mtg-i1": "meteosat",
  fci: "meteosat",
  aurora: "aurora",
  ovation: "aurora",
  // The geostationary mosaic, a view over the imagers above.
  geo: "geo",
  mosaic: "geo",
  gibs: "gibs",
  worldview: "gibs",
  geostationary: "geo",
};

/** The canonical id a spelling names, or null for one no dataset owns. */
export function modelForAlias(alias: string): ForecastModelId | null {
  const key = alias.trim().toLowerCase();
  return Object.hasOwn(MODEL_ALIASES, key) ? MODEL_ALIASES[key]! : null;
}

/** True for the home's own addresses: `/` and the `index.html` Pages
 * redirects to it. */
export function isHomePathname(pathname: string): boolean {
  return pathname === "/" || pathname === "/index.html";
}

/** Live datasets with no page of their own, reached as `/?model=<id>` on
 * the home and through their cases. */
export const NO_PAGE_MODEL_IDS: ReadonlySet<ForecastModelId> = new Set<ForecastModelId>([
  // MRMS 3D: its live publishing is paused, so a page would index an empty view.
  "mrms3d",
]);

/** True when a live dataset is written at a path of its own (`/<id>`). */
export function hasModelPage(model: ForecastModelId): boolean {
  return !NO_PAGE_MODEL_IDS.has(model);
}

/** The dataset a path names, or null when it names none of `pathIds`: the
 * one segment Pages serves a dataset's page at, its canonical id. Any
 * other path answers with the 404 page and never reaches the shell. */
export function modelFromPathname(pathname: string, pathIds: ReadonlySet<string>): ForecastModelId | null {
  const segment = /^\/([a-z0-9]+)$/.exec(pathname)?.[1];
  return segment !== undefined && pathIds.has(segment) ? (segment as ForecastModelId) : null;
}

/** The path a live view of `model` is written at. */
export function modelPathname(model: ForecastModelId): string {
  return `/${model}`;
}

/** Where a request naming its dataset as `?model=` belongs, as a
 * root-relative path and query, or null when it is fine where it is.
 *
 * Only the home reads the parameter: `/?model=hrrr&type=wind` belongs at
 * `/hrrr?type=wind`. A dataset with no page (`NO_PAGE_MODEL_IDS`) is
 * opened by the parameter itself, so its link is left alone. A spelling of
 * any other dataset outside `pathIds` — unknown, or one reached only
 * through its cases — belongs at `/` without it, where the default opens,
 * as it always did. A case (`?case=`) pins its own dataset and ignores
 * `model`, so a case link is left alone. Every other parameter is kept in
 * order; the fragment is the browser's to keep. */
export function legacyModelLocation(pathname: string, search: string, pathIds: ReadonlySet<string>): string | null {
  if (!isHomePathname(pathname)) return null;
  const params = new URLSearchParams(search);
  const model = params.get("model");
  if (model === null || params.has("case")) return null;
  const id = modelForAlias(model);
  if (id !== null && !hasModelPage(id)) return null;
  params.delete("model");
  const rest = params.toString();
  const query = rest === "" ? "" : `?${rest}`;
  return id !== null && pathIds.has(id) ? `${modelPathname(id)}${query}` : `/${query}`;
}
