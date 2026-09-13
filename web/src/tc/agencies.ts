/** Who says what about a storm: the frontend's copy of the encoder's
 * registry (`xuebuild/tc/registry.py`), pinned to
 * `tests/fixtures/tc-registry.json` by `tests/web/tc.test.ts` so a color
 * or an id cannot drift between the two.
 *
 * The product admits any `^[a-z][a-z0-9]*$` key: a centre or a model this
 * table does not know is still drawn, in the neutral ink, under its own
 * id. The names here are the instrument codes (`NHC`, `JTWC`), English in
 * every locale like the rest of the panel's codes. */

export interface AgencyInfo {
  id: string;
  /** The code the map and the panel print. */
  code: string;
  /** English name, for a tooltip. */
  name: string;
  /** Fixed line color — Okabe–Ito, never the theme's. */
  color: string;
  /** ATCF basins it forecasts in. */
  basins: readonly string[];
  /** Whose intensity classes its `class` strings are. */
  scale: string;
}

export interface ModelInfo {
  id: string;
  code: string;
  name: string;
  ensemble: boolean;
}

export const TC_AGENCIES: Record<string, AgencyInfo> = {
  nhc: {
    id: "nhc",
    code: "NHC",
    name: "National Hurricane Center",
    color: "#d55e00",
    basins: ["AL", "EP", "CP"],
    scale: "sshs",
  },
  jtwc: {
    id: "jtwc",
    code: "JTWC",
    name: "Joint Typhoon Warning Center",
    color: "#e69f00",
    basins: ["WP", "IO", "SH"],
    scale: "sshs",
  },
  jma: {
    id: "jma",
    code: "JMA",
    name: "Japan Meteorological Agency",
    color: "#0072b2",
    basins: ["WP"],
    scale: "jma",
  },
  cma: {
    id: "cma",
    code: "CMA",
    name: "China Meteorological Administration",
    color: "#009e73",
    basins: ["WP"],
    scale: "cma",
  },
  cwa: {
    id: "cwa",
    code: "CWA",
    name: "Central Weather Administration",
    color: "#cc79a7",
    basins: ["WP"],
    scale: "cwa",
  },
  hko: {
    id: "hko",
    code: "HKO",
    name: "Hong Kong Observatory",
    color: "#56b4e9",
    basins: ["WP"],
    scale: "hko",
  },
  kma: {
    id: "kma",
    code: "KMA",
    name: "Korea Meteorological Administration",
    color: "#f0e442",
    basins: ["WP"],
    scale: "kma",
  },
  imd: {
    id: "imd",
    code: "IMD",
    name: "India Meteorological Department",
    color: "#8a8a8a",
    basins: ["IO"],
    scale: "imd",
  },
  mfr: {
    id: "mfr",
    code: "MFR",
    name: "Météo-France La Réunion",
    color: "#8a8a8a",
    basins: ["SH"],
    scale: "mfr",
  },
  bom: {
    id: "bom",
    code: "BOM",
    name: "Bureau of Meteorology",
    color: "#8a8a8a",
    basins: ["SH"],
    scale: "bom",
  },
  fms: {
    id: "fms",
    code: "FMS",
    name: "Fiji Meteorological Service",
    color: "#8a8a8a",
    basins: ["SH"],
    scale: "fms",
  },
  mnz: {
    id: "mnz",
    code: "MNZ",
    name: "MetService New Zealand",
    color: "#8a8a8a",
    basins: ["SH"],
    scale: "mnz",
  },
};

export const TC_MODELS: Record<string, ModelInfo> = {
  gfs: { id: "gfs", code: "GFS", name: "GFS", ensemble: false },
  gefs: { id: "gefs", code: "GEFS", name: "GEFS", ensemble: true },
  ecmwf: { id: "ecmwf", code: "ECMWF", name: "ECMWF IFS", ensemble: false },
  ecmwfens: { id: "ecmwfens", code: "ENS", name: "ECMWF ENS", ensemble: true },
};

/** The IBTrACS best-track keys and the centre each stands for; `usa` is
 * the NHC / JTWC joint best track and takes the neutral ink. */
export const TC_BEST_AGENCY: Record<string, string | null> = {
  usa: null,
  jma: "jma",
  cma: "cma",
  hko: "hko",
  kma: "kma",
  imd: "imd",
  mfr: "mfr",
  bom: "bom",
  fms: "fms",
  mnz: "mnz",
};

/** The neutral ink for anything without a centre's color: model tracks,
 * the US best track, an unknown key. Set per ground by the caller. */
export const NEUTRAL_LIGHT_GROUND = "#333333";
export const NEUTRAL_DARK_GROUND = "#d8d8d8";

export function agencyColor(id: string): string | null {
  return TC_AGENCIES[id]?.color ?? null;
}

export function agencyCode(id: string): string {
  return TC_AGENCIES[id]?.code ?? TC_MODELS[id]?.code ?? id.toUpperCase();
}

export const TC_BASINS: Record<string, string> = {
  AL: "North Atlantic",
  EP: "Eastern North Pacific",
  CP: "Central North Pacific",
  WP: "Western North Pacific",
  IO: "North Indian Ocean",
  SH: "Southern Hemisphere",
};
