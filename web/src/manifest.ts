import { HRRR_DOMAIN, type LambertDomain } from "./domain";
import { fetchImmutable } from "./fetchimmutable";
import { t } from "./i18n";
// Type-only: both modules import this one at runtime.
import type { GeoGrid } from "./probe";
import type { ViewportBounds } from "./tiles";

export type ForecastVariableId = "tmp2m" | "prate";

/** Datasets this app can tune to. Each is its own dataset: its own
 * immutable run directories and its own manifest identity, and — when it has
 * a live feed — its own mutable live pointer at the data root. GFS uses the
 * bare ``latest.json``; the other live models use ``latest-<model>.json``.
 * The CMA radar mosaic has no live feed: it is an observation archive that
 * reaches the app only as showcase cases. The MRMS mosaic and the JMA
 * nowcast are observations *and* live, each a rolling window. */
export type ForecastModelId =
  | "gfs"
  | "ecmwf"
  | "aifs"
  | "ifshres"
  | "cfs"
  | "sflux"
  | "hrrr"
  | "gefsaero"
  | "cma"
  | "mrms"
  | "jma"
  | "himawari"
  | "goeseast"
  | "goeswest"
  | "meteosat"
  | "geo";

export interface ForecastModelInfo {
  id: ForecastModelId;
  /** Display label and the manifest/pointer ``model`` string. */
  label: string;
  /** The manifest/pointer ``product`` string. */
  product: string;
  /** Mutable live pointer filename at the data root, absent for a dataset
   * with no live feed. On an observation dataset the pointer follows a
   * rolling window: the run is the window's first hour and moves on every
   * hour, and the manifest it names is rebuilt every few minutes into a
   * round of its own (`<model>.<run>/<HHMM>/manifest.json`), so what says
   * "new" is the manifest's crc, not the run id. */
  latestFilename?: string;
  /** True when the dataset is observations rather than a forecast. It has no
   * run cycle and no lead time — its `runTime` is when the series starts and
   * a frame's offset is time elapsed since — so the viewer labels it as
   * such (mirrors `SourceSpec.observation` in xue/sources.py). */
  observation?: boolean;
  /** The bundles a complete run of the dataset must ship — what a live
   * manifest is refused without: the tmp2m/prate pair on a forecast, the
   * reflectivity on a radar mosaic (mirrors `SourceSpec.core_bundle_ids`).
   * Absent means the forecast pair, `FORECAST_VARIABLE_IDS`. */
  coreBundles?: readonly string[];
  /** The layer the dataset opens on when nothing asked for one — no
   * `?type=`, no tile pressed this session: the reflectivity on a radar
   * mosaic, whose precipitation rate is the derived product. Absent means
   * the app's default, precipitation. */
  defaultVariable?: ForecastBundleId;
  /** The fields the layer rail always carries a tile for, in rail order
   * (each shown only when the run ships it): the forecast core of
   * temperature, precipitation, wind and cloud, the reflectivity and rate
   * on a radar mosaic. Every other field is reached through the rail's
   * MORE sheet and takes a tile of its own only while on screen. Absent
   * means `FORECAST_RAIL_CORE`. */
  railCore?: readonly ForecastBundleId[];
  /** The part of the world a regional model covers, as [west, south, east,
   * north] in degrees: where the camera goes when the model is opened on a
   * view that shows none of it. A global model has none. */
  region?: readonly [number, number, number, number];
  /** The model's own grid on its map projection, for a model the encoder
   * resampled onto the regular grid the bundles carry (`domain.ts`): the
   * renderers clip to it, since the rectangle around a conic footprint
   * holds corners the model never forecast. */
  domain?: LambertDomain;
  /** For a geostationary imager, its sub-satellite longitude in degrees
   * east, 0–360: where the mosaic (`mosaic.ts`) draws the seam between it
   * and its neighbours. */
  subLongitude?: number;
  /** For an observation window, the step between its frames in seconds
   * (mirrors `SourceSpec.cadence_seconds`): what says which member of a
   * mosaic drives the timeline. */
  cadenceSeconds?: number;
  /** A mosaic is not a dataset but a view over several: it has no pointer
   * and no manifest of its own, and opens the published run of each member
   * that answers. */
  mosaic?: boolean;
  /** The datasets a mosaic composes, each clipped to its own band. */
  members?: readonly ForecastModelId[];
}

/** The rail's core tiles on a forecast model (`ForecastModelInfo.railCore`). */
export const FORECAST_RAIL_CORE: readonly ForecastBundleId[] = ["tmp2m", "prate", "wind10m", "tcdc"];

export const FORECAST_MODELS: Record<ForecastModelId, ForecastModelInfo> = {
  gfs: { id: "gfs", label: "GFS", product: "pgrb2.0p25", latestFilename: "latest.json" },
  // GFS surface flux on the native ~13 km grid; the only source that ships
  // the dswrf solar-radiation bundle, which takes the cloud's place among
  // its core tiles: it publishes exactly four fields.
  sflux: {
    id: "sflux",
    label: "GFS-SFLUX",
    product: "sfluxgrb",
    latestFilename: "latest-sflux.json",
    railCore: ["tmp2m", "prate", "wind10m", "dswrf"],
  },
  ecmwf: { id: "ecmwf", label: "ECMWF", product: "ifs-0p25", latestFilename: "latest-ecmwf.json" },
  // ECMWF's data-driven model, AIFS Single, from the same open data service
  // on the same 0.25° grid: six-hourly to 360 hours from every cycle.
  aifs: { id: "aifs", label: "AIFS", product: "aifs-single-0p25", latestFilename: "latest-aifs.json" },
  // ECMWF's operational IFS HRES on its native ~9 km grid, resampled to
  // 0.1° by Open-Meteo's forwarding of the centre's real-time archive:
  // surface fields only, hourly to F090 and out to F360, from the 00Z and
  // 12Z cycles alone.
  ifshres: { id: "ifshres", label: "ECMWF-HRES", product: "ifs-hres-0p1", latestFilename: "latest-ifshres.json" },
  // NOAA CFSv2, the coupled seasonal model: one nine-month forecast from
  // the 00Z and 12Z cycles, six-hourly on the model's own T126 Gaussian
  // grid. Its axis is the long one: 1092 frames (F6 to F6552) over 39 weeks, which the
  // track reads in months rather than in days (`timeline.ts`). Surface
  // fields only, so the rail opens on the medium-range core with the sea
  // surface beside it rather than the cloud.
  cfs: {
    id: "cfs",
    label: "CFSv2",
    product: "time-grib-01",
    latestFilename: "latest-cfs.json",
    railCore: ["tmp2m", "prate", "wind10m", "tmpsfc"],
  },
  // NOAA HRRR, 3 km over the contiguous United States, a new cycle every
  // hour: a regional model, resampled by the encoder from its Lambert
  // conformal grid onto a 0.03° one over the domain's footprint.
  hrrr: {
    id: "hrrr",
    label: "HRRR",
    product: "wrfsfc",
    latestFilename: "latest-hrrr.json",
    region: [-134.1, 21.12, -60.9, 52.62],
    domain: HRRR_DOMAIN,
  },
  // NOAA GEFS-Aerosols, the global aerosol member of the GEFS: aerosol
  // optical depth at 550 nm for the whole column and by species, and the
  // surface PM2.5 / PM10 concentrations, on the GFS's 0.25° grid,
  // three-hourly to F120 from every cycle. No temperature and no
  // precipitation: the run's core is the total optical depth, and the rail
  // carries the two family tiles (optical depth, particulate matter) whose
  // species and size cuts are the sheet's chips.
  gefsaero: {
    id: "gefsaero",
    label: "GEFS-AEROSOLS",
    product: "chem-a2d-0p25",
    latestFilename: "latest-gefsaero.json",
    coreBundles: ["aod"],
    defaultVariable: "aod",
    railCore: ["aod", "pm25"],
  },
  // CMA weather radar level-3 mosaic composite reflectivity: the national
  // composite every six minutes on the data portal's plate carrée tile grid
  // (0.0439°, 67.5°E–146.25°E and 11.25°N–56.25°N), read by the encoder out
  // of a daily Zarr archive of the mosaics. Observations and live, like
  // MRMS: a rolling window of the last two to three hours, some forty
  // minutes behind real time (the portal publishes late and the archive
  // syncs every twenty minutes). The region is where the camera goes when
  // it is opened from elsewhere. The id was `radar` while the source was
  // cases only; the manifest identity CMA-RADAR is unchanged.
  cma: {
    id: "cma",
    label: "CMA-RADAR",
    product: "l3-mst-cref",
    latestFilename: "latest-cma.json",
    observation: true,
    coreBundles: ["cref"],
    defaultVariable: "cref",
    railCore: ["cref"],
    region: [67.5, 11.25, 146.25, 56.25],
  },
  // NOAA MRMS, the national radar mosaic over the contiguous United States:
  // composite reflectivity and precipitation rate every two minutes on a
  // regular grid the encoder thins to 0.02°. Observations, and the one
  // dataset that is observations *and* live: the pointer names a rolling
  // window of the last three to four hours, rebuilt every five minutes.
  // The region is where the camera goes when it is opened from elsewhere.
  mrms: {
    id: "mrms",
    label: "NOAA-MRMS",
    product: "conus-cref",
    latestFilename: "latest-mrms.json",
    observation: true,
    coreBundles: ["cref"],
    defaultVariable: "cref",
    railCore: ["cref", "prate"],
    region: [-130, 20, -60, 55],
  },
  // JMA 高解像度降水ナウキャスト, the Japan Meteorological Agency's
  // precipitation nowcast analysis: precipitation intensity classes every
  // five minutes, decoded by the encoder from the agency's map tiles onto
  // a 0.005° grid over the radar coverage envelope. Not a reflectivity —
  // the agency publishes rate classes — so it ships `prate` alone, at each
  // class's representative rate, and opens on it. Observations and live,
  // like MRMS: a rolling window of the last two to three hours.
  jma: {
    id: "jma",
    label: "JMA-HRPNS",
    product: "japan-prate",
    latestFilename: "latest-jma.json",
    observation: true,
    coreBundles: ["prate"],
    defaultVariable: "prate",
    railCore: ["prate"],
    region: [121, 20.5, 149, 45.5],
  },
  // Himawari-9, the JMA geostationary imager at 140.7°E, as NOAA
  // redistributes it: the 10.4 µm infrared window as brightness
  // temperature, one full-disk scan every ten minutes, warped by the
  // encoder from the geostationary view onto a 0.04° grid over the useful
  // disk. The source is named by its orbital slot, not the spacecraft —
  // the file's `band` block carries that — so a successor changes nothing
  // here. Observations and live, like MRMS: a rolling window of the last
  // five to six hours, some fifteen to twenty minutes behind real time.
  // The region runs past the antimeridian (200.7°E), which `domain.ts` and
  // `tiles.ts` take in the grid's own copy of the world.
  himawari: {
    id: "himawari",
    label: "HIMAWARI",
    product: "ahi-fldk-0p04",
    latestFilename: "latest-himawari.json",
    observation: true,
    coreBundles: ["ir104"],
    defaultVariable: "ir104",
    railCore: ["ir104", "dustrgb", "dustcf"],
    region: [80.7, -60, 200.7, 60],
    subLongitude: 140.7,
    cadenceSeconds: 600,
  },
  // The two GOES-R imagers NOAA flies, read from NOAA's own buckets: the
  // same 10.4 µm window and the same Dust RGB as Himawari, one full-disk
  // scan every ten minutes, warped by the encoder onto a 0.04° grid over
  // each disk's useful extent. Named by the orbital slot (GOES-East at
  // 75.2°W is GOES-19 today, GOES-West at 137.0°W is GOES-18), never the
  // spacecraft, for the same reason as Himawari. GOES-West's region runs
  // past the antimeridian too — spelled 163…283 so it crosses 180 the
  // way Himawari's does, in the grid's own copy of the world.
  goeseast: {
    id: "goeseast",
    label: "GOES-EAST",
    product: "abi-fldk-0p04",
    latestFilename: "latest-goeseast.json",
    observation: true,
    coreBundles: ["ir104"],
    defaultVariable: "ir104",
    railCore: ["ir104", "dustrgb", "dustcf"],
    region: [-135.2, -60, -15.2, 60],
    subLongitude: 284.8,
    cadenceSeconds: 600,
  },
  goeswest: {
    id: "goeswest",
    label: "GOES-WEST",
    product: "abi-fldk-0p04",
    latestFilename: "latest-goeswest.json",
    observation: true,
    coreBundles: ["ir104"],
    defaultVariable: "ir104",
    railCore: ["ir104", "dustrgb", "dustcf"],
    region: [163, -60, 283, 60],
    subLongitude: 223,
    cadenceSeconds: 600,
  },
  // The four disks as one picture (`mosaic.ts`): a view, not a dataset. It
  // opens whichever members' pointers answer, clips each to the longitudes
  // it sees least obliquely, and follows one timeline by valid time. A
  // member absent from the data root leaves the basemap between its
  // neighbours' disk edges rather than failing the view.
  geo: {
    id: "geo",
    label: "GEO MOSAIC",
    product: "geo-mosaic",
    observation: true,
    coreBundles: ["ir104"],
    defaultVariable: "ir104",
    railCore: ["ir104", "dustrgb", "dustcf"],
    region: [-180, -60, 180, 60],
    mosaic: true,
    members: ["meteosat", "himawari", "goeswest", "goeseast"],
  },
  // Meteosat-12 (MTG-I1) at 0°, EUMETSAT's FCI imager, read from the
  // EUMETSAT Data Store: the same 10.4 µm window (FCI's 10.5 µm channel)
  // and the same Dust RGB, warped onto a 0.04° grid over the disk's useful
  // extent. The source publishes the hourly repeat cycle alone — the one
  // EUMETSAT's data policy releases under CC-BY-4.0 for redistribution —
  // so its window is a day of hourly frames rather than hours of
  // ten-minute ones. Named by the slot (0° is Meteosat-12 today), never
  // the spacecraft, as the other disks are.
  meteosat: {
    id: "meteosat",
    label: "METEOSAT",
    product: "fci-fldk-0p04",
    latestFilename: "latest-meteosat.json",
    observation: true,
    coreBundles: ["ir104"],
    defaultVariable: "ir104",
    railCore: ["ir104", "dustrgb"],
    region: [-60, -60, 60, 60],
    subLongitude: 0,
    cadenceSeconds: 3600,
  },
};

/** The layer a dataset opens on when nothing asked for one. */
export function modelDefaultVariable(model: ForecastModelId, fallback: ForecastBundleId): ForecastBundleId {
  return FORECAST_MODELS[model].defaultVariable ?? fallback;
}

/** The fields the rail always carries a tile for on a dataset. */
export function modelRailCore(model: ForecastModelId): readonly ForecastBundleId[] {
  return FORECAST_MODELS[model].railCore ?? FORECAST_RAIL_CORE;
}

/** True when a dataset is observations, not a forecast. */
export function isObservationModel(model: ForecastModelId): boolean {
  return FORECAST_MODELS[model].observation === true;
}

/** The model switch's entries, in order: the eight forecasts, the seven
 * rolling observation windows (MRMS, the JMA nowcast, the CMA mosaic and
 * the four geostationary imagers) and the geostationary mosaic, a view
 * over the imagers with no feed of its own. */
export const FORECAST_MODEL_IDS: readonly ForecastModelId[] = [
  "gfs",
  "sflux",
  "ecmwf",
  "aifs",
  "ifshres",
  "cfs",
  "hrrr",
  "gefsaero",
  "mrms",
  "jma",
  "cma",
  "himawari",
  "goeseast",
  "goeswest",
  "meteosat",
  "geo",
];

/** The members of a mosaic this build knows, in the mosaic's order; a
 * member id the table lacks (a dataset this shell predates) is skipped the
 * way a member whose pointer is missing is. */
export function mosaicMembers(model: ForecastModelId): ForecastModelInfo[] {
  const info = FORECAST_MODELS[model];
  if (!info.mosaic || !info.members) return [];
  const members: ForecastModelInfo[] = [];
  for (const id of info.members) {
    const member = (FORECAST_MODELS as Record<string, ForecastModelInfo | undefined>)[id];
    if (member && !member.mosaic && member.latestFilename !== undefined) members.push(member);
  }
  return members;
}

function modelForManifestString(model: unknown): ForecastModelInfo | null {
  for (const info of Object.values(FORECAST_MODELS)) {
    if (info.label === model) return info;
  }
  return null;
}

/** The eight standard isobaric surfaces every isobaric family is registered
 * on, in hPa, in the encoders' `variableId` order (mirrors
 * `ISOBARIC_LEVELS_HPA` in xuebuild/variables.py). One bundle per level —
 * container v2 orders chunks group -> tile -> variable, so packing the levels
 * together would make a viewport pull every level to read one. */
export const ISOBARIC_LEVELS = [1000, 925, 850, 700, 500, 300, 250, 200] as const;
export type IsobaricLevel = (typeof ISOBARIC_LEVELS)[number];

/** The pressure family: mean sea level pressure and geopotential height on
 * the isobaric surfaces. What the viewer draws from them is contours; see
 * pressure.ts. */
export type PressureBundleId = "prmsl" | `hgt${IsobaricLevel}`;

/** The filled isobaric scalars: temperature, relative humidity and specific
 * humidity on the same surfaces (levels.ts). */
export type IsobaricScalarBundleId =
  | `tmp${IsobaricLevel}`
  | `rh${IsobaricLevel}`
  | `spfh${IsobaricLevel}`
  | `vvel${IsobaricLevel}`
  | `thetae${IsobaricLevel}`;

/** The two-variable bundles: a u/v pair the viewer draws as a magnitude field
 * with optional particles — the 10 m wind, the 100 m wind (the turbine hub
 * height, the same parameters on surface type 103 value 100), the wind on
 * each isobaric surface, and the water vapour flux the encoder derives
 * there. */
export type VectorBundleId = "wind10m" | "wind100m" | `wind${IsobaricLevel}` | `qflux${IsobaricLevel}` | "wave";

/** The three-variable bundles: a colour composite a producer derived from
 * several satellite channels, whose variables are the three guns the viewer
 * draws straight as red, green and blue (the classic Dust RGB). */
export type CompositeBundleId = "dustrgb";

/** A bundle-level id in a manifest.
 *
 * Deliberately a plain string: the manifest is not a registry. What a bundle
 * *is* lives in the file, as the GRIB2 parameter block on each of its
 * variables (identity.ts); what the manifest carries is a name, admitted on
 * its shape alone (`isBundleVariableId`). A run may therefore publish a
 * bundle this shell has never heard of and still validate — it renders
 * generically rather than taking the whole manifest down with it.
 *
 * The ids the shell has chart knowledge about are `KnownBundleId`, and they
 * are a naming convention (`<family><level>`, `tmp2m`, `wind10m`), not a
 * closed set. */
export type ForecastBundleId = string;

/** The bundle ids this shell draws with a palette, legend and ceiling of
 * its own. Used for typing the chart registries, never for admission. */
export type KnownBundleId =
  | ForecastVariableId
  | "dswrf"
  | "cref"
  | SurfaceDiagnosticId
  | OceanId
  | SatelliteId
  | AerosolId
  | PressureBundleId
  | IsobaricScalarBundleId
  | VectorBundleId;

/** The surface diagnostics — wind gust, cloud cover (the total and the
 * three layers), surface-based CAPE and its twin convective inhibition,
 * visibility, 2 m dew point and 2 m apparent temperature, precipitable
 * water, planetary boundary layer height and the categorical precipitation
 * type — single layers like `dswrf`, held to the encoders by
 * `tests/fixtures/surface-registry.json`. The order is the registry's own
 * (`xuebuild/quantize.py::SURFACE_VARIABLE_IDS`), which `tests/web/surface.test.ts`
 * holds the list to. */
export type SurfaceDiagnosticId =
  | "gust"
  | "tcdc"
  | "cape"
  | "cin"
  | "vis"
  | "dpt2m"
  | "aptmp2m"
  | "lcdc"
  | "mcdc"
  | "hcdc"
  | "pwat"
  | "hpbl"
  | "ptype";
export const SURFACE_DIAGNOSTIC_IDS: readonly SurfaceDiagnosticId[] = [
  "gust",
  "tcdc",
  "cape",
  "cin",
  "vis",
  "dpt2m",
  "aptmp2m",
  "lcdc",
  "mcdc",
  "hcdc",
  "pwat",
  "hpbl",
  "ptype",
];

/** The ocean set — the surface (skin) temperature, which is the SST over
 * water, sea ice cover and thickness, and the GFS-Wave significant wave
 * height, primary wave period and direction — single layers held to the
 * encoders by `tests/fixtures/ocean-registry.json`. The same registry
 * carries the components of the `wave` vector bundle the encoders derive
 * from the height and direction (`WAVE_COMPONENT_IDS`). */
export type OceanId = "tmpsfc" | "icec" | "icetk" | "htsgw" | "perpw" | "dirpw";
export const OCEAN_IDS: readonly OceanId[] = ["tmpsfc", "icec", "icetk", "htsgw", "perpw", "dirpw"];

/** The satellite bundles — brightness temperature in the 10.4 µm infrared
 * window, one bundle per channel, named by nominal wavelength and
 * instrument-neutral (AHI band 13 and ABI channel 13 are both `ir104`; the
 * file's `band` block says which), the Dust RGB composite a producer
 * derives from four infrared channels (`dustrgb`, a `CompositeBundleId`),
 * and the DEBRA dust confidence the same producer derives from five
 * (`dustcf`: one scalar in 0–1, painted with a ramp like a channel, not a
 * composite) — held to the encoders by
 * `tests/fixtures/satellite-registry.json`. */
export type SatelliteId = "ir104" | CompositeBundleId | "dustcf";
export const SATELLITE_IDS: readonly SatelliteId[] = ["ir104", "dustrgb", "dustcf"];

/** The aerosol set — the aerosol optical depth at 550 nm, for the whole
 * column and for each of five species, and the surface particulate matter
 * concentrations (PM2.5, PM10, and the dust part of PM10) — single layers
 * whose parameter block alone does not tell them apart: every optical
 * depth is one WMO parameter on the entire atmosphere, and the two PM10
 * fields one NCEP-local number on the ground, so each carries the
 * `aerosol` block beside its parameter (`BundleAerosol`), the species and
 * the size cut. Held to the encoders by
 * `tests/fixtures/aerosol-registry.json`. */
export type AerosolId = "aod" | "aoddust" | "aodsalt" | "aodsulf" | "aodorg" | "aodbc" | "pm25" | "pm10" | "pm10dust";
export const AEROSOL_IDS: readonly AerosolId[] = ["aod", "aoddust", "aodsalt", "aodsulf", "aodorg", "aodbc", "pm25", "pm10", "pm10dust"];

/** A well-formed bundle/variable name: lowercase alphanumeric, starting with
 * a letter. This is the whole admission rule — a manifest is rejected for
 * naming a bundle badly, never for naming one this build does not know. */
export function isBundleVariableId(value: unknown): value is ForecastBundleId {
  return typeof value === "string" && /^[a-z][a-z0-9]*$/.test(value);
}

/** The component variables a vector bundle carries, both on one time axis. */
export type VectorComponentId =
  | "ugrd10m"
  | "vgrd10m"
  | "ugrd100m"
  | "vgrd100m"
  | `ugrd${IsobaricLevel}`
  | `vgrd${IsobaricLevel}`
  | `uqflx${IsobaricLevel}`
  | `vqflx${IsobaricLevel}`
  | "uwave"
  | "vwave";

/** The component variables a composite bundle carries: the three guns of
 * the Dust RGB, in the order the renderer draws them. */
export type CompositeComponentId = "dustr" | "dustg" | "dustb";

/** Data-level variable ids that can appear inside bundle metadata. A plain
 * string for the same reason `ForecastBundleId` is: a file names its own
 * variables, and the parameter block — not the name — says what they are. */
export type DataVariableId = string;

/** The data-level ids this shell knows by name (the v1/v2 fallback in
 * identity.ts, and the component pairs below). */
export type KnownDataVariableId =
  | ForecastVariableId
  | "dswrf"
  | "cref"
  | SurfaceDiagnosticId
  | OceanId
  | AerosolId
  | PressureBundleId
  | IsobaricScalarBundleId
  | VectorComponentId
  | CompositeComponentId;

export const FORECAST_VARIABLE_IDS: readonly ForecastVariableId[] = ["tmp2m", "prate"];

function perLevel<Prefix extends string>(prefix: Prefix): `${Prefix}${IsobaricLevel}`[] {
  return ISOBARIC_LEVELS.map((level) => `${prefix}${level}` as `${Prefix}${IsobaricLevel}`);
}

/** Every bundle id this shell has chart knowledge about, in the rail's
 * order (every scalar, then every vector — the order the encoders write).
 * This is what the layer rail and the level row are built from — *not* a
 * list a manifest is checked against. */
export const KNOWN_BUNDLE_IDS: readonly KnownBundleId[] = [
  "tmp2m",
  "prate",
  "dswrf",
  "cref",
  ...SURFACE_DIAGNOSTIC_IDS,
  ...OCEAN_IDS,
  ...SATELLITE_IDS,
  ...AEROSOL_IDS,
  "prmsl",
  ...perLevel("hgt"),
  ...perLevel("tmp"),
  ...perLevel("rh"),
  ...perLevel("spfh"),
  ...perLevel("vvel"),
  ...perLevel("thetae"),
  "wind10m",
  "wind100m",
  ...perLevel("wind"),
  ...perLevel("qflux"),
  "wave",
];
export const WIND_COMPONENT_IDS: readonly KnownDataVariableId[] = ["ugrd10m", "vgrd10m"];
/** The wave vector's pair: the significant wave height laid along the
 * direction the waves travel, in metres, in the wind's u/v convention. */
export const WAVE_COMPONENT_IDS: readonly ["uwave", "vwave"] = ["uwave", "vwave"];

/** The u/v component pair of every vector bundle. */
export const VECTOR_BUNDLES: Record<VectorBundleId, readonly [VectorComponentId, VectorComponentId]> = {
  wind10m: ["ugrd10m", "vgrd10m"],
  // The 100 m pair: the same parameters on the turbine hub height, the
  // `wind100m` bundle (xuebuild/binconvert.py::WIND_100M_COMPONENT_IDS).
  wind100m: ["ugrd100m", "vgrd100m"],
  ...Object.fromEntries(ISOBARIC_LEVELS.map((level) => [`wind${level}`, [`ugrd${level}`, `vgrd${level}`]])),
  ...Object.fromEntries(ISOBARIC_LEVELS.map((level) => [`qflux${level}`, [`uqflx${level}`, `vqflx${level}`]])),
  wave: WAVE_COMPONENT_IDS,
} as unknown as Record<VectorBundleId, readonly [VectorComponentId, VectorComponentId]>;

/** True when a bundle *named* by the convention carries a u/v pair rather
 * than one scalar. A guess from the id string, for use before the bundle is
 * open; an open session answers this from its variables' parameter blocks
 * instead (`VariableSession.vector`). */
export function isVectorBundle(id: ForecastBundleId): id is VectorBundleId {
  return id in VECTOR_BUNDLES;
}

/** The two components a conventionally named vector bundle carries, or null
 * for a scalar. Also a naming-convention guess; an open bundle names its own
 * components. */
export function vectorComponents(id: ForecastBundleId): readonly [VectorComponentId, VectorComponentId] | null {
  return isVectorBundle(id) ? VECTOR_BUNDLES[id] : null;
}

/** The three guns of every composite bundle, red, green, blue — the order
 * the encoders number them (1, 2, 3) and the renderer's RGB texture takes
 * them in. */
export const COMPOSITE_BUNDLES: Record<CompositeBundleId, readonly [CompositeComponentId, CompositeComponentId, CompositeComponentId]> = {
  dustrgb: ["dustr", "dustg", "dustb"],
};

/** True when a bundle *named* by the convention carries three colour guns
 * rather than one scalar or a u/v pair. A guess from the id string like
 * `isVectorBundle`; an open session answers from its variables' parameter
 * and producer blocks instead (`VariableSession.composite`). */
export function isCompositeBundle(id: ForecastBundleId): id is CompositeBundleId {
  return id in COMPOSITE_BUNDLES;
}

/** The three components a conventionally named composite bundle carries,
 * or null for anything else. */
export function compositeComponents(id: ForecastBundleId): readonly [CompositeComponentId, CompositeComponentId, CompositeComponentId] | null {
  return isCompositeBundle(id) ? COMPOSITE_BUNDLES[id] : null;
}

export interface VideoBundleDescriptor {
  streamPath: string;
  indexPath: string;
  byteLength: number;
  crc32: string;
  codec: string;
  width: number;
  height: number;
  gop: number;
  frameCount: number;
  /** Same shape as the metadata embedded in the .xue file, scoped to this
   * variable — lets the video path skip fetching the .xue just for grid,
   * time axis, and quantization info. Parse with parseBundleMetadata(). */
  metadataJson: string;
}

export interface PosterDescriptor {
  path: string;
  width: number;
  height: number;
  byteLength: number;
  crc32: string;
  /** Bundle-shaped metadata scoped to this variable, with the POSTER grid
   * (half resolution) — enough to configure the WebGL layer and palette
   * before any bundle byte arrives. Parse with parseBundleMetadata(). */
  metadataJson: string;
}

/** One reduced-resolution rendition of a variable's bundle (HLS
 * STREAM-INF semantics). The bundle's own top-level path stays the
 * canonical full-resolution tier; variants are alternates the client may pick
 * by viewport need and network quality. */
export interface VariantDescriptor {
  width: number;
  height: number;
  /** Average bits per second needed to sustain 12 fps playback while
   * downloading the whole tier — the STREAM-INF BANDWIDTH analogue. */
  bandwidth: number;
  /** This tier as a Zarr store, when the build wrote one. */
  zarr?: ZarrStoreDescriptor;
  /** This tier as a `.xue` container — `path`, `byteLength` and `crc32`
   * are one unit, present whole or absent whole. A tier ships the
   * container, the store, or both; the validator refuses neither. */
  path?: string;
  byteLength?: number;
  crc32?: string;
}

/** The `.xue` container of a bundle or a tier, once the validator has
 * established the entry carries one. */
export interface ContainerDescriptor {
  path: string;
  byteLength: number;
  crc32: string;
}

/** The container fields of an entry, or null when it ships only its store. */
export function containerOf(entry: { path?: string; byteLength?: number; crc32?: string }): ContainerDescriptor | null {
  if (entry.path === undefined || entry.byteLength === undefined || entry.crc32 === undefined) return null;
  return { path: entry.path, byteLength: entry.byteLength, crc32: entry.crc32 };
}

/** What an entry weighs on the wire: the container's bytes, or the store's
 * on an entry that ships only the store. */
export function deliveryBytes(entry: { byteLength?: number; zarr?: ZarrStoreDescriptor }): number {
  return entry.byteLength ?? entry.zarr?.byteLength ?? 0;
}

/** The same bundle as a Zarr v3 store (docs/zarr-profile.md): `path` is the
 * store's root directory (relative, `.zarr`), `byteLength` the sum of every
 * object in it, and `crc32` the CRC-32 of its root `zarr.json` — the one
 * value a client appends as `?v=` to every object it fetches from the store.
 * Optional on a bundle and on each of its variants; a run that ships none is
 * complete without it. */
export interface ZarrStoreDescriptor {
  path: string;
  byteLength: number;
  crc32: string;
}

export interface VariableBundleDescriptor {
  variable: ForecastBundleId;
  /** The canonical full-resolution `.xue` container — one unit like a
   * tier's, and absent as a whole on a bundle that ships only its store. */
  path?: string;
  byteLength?: number;
  crc32?: string;
  /** Reduced-resolution renditions. */
  variants?: VariantDescriptor[];
  /** Alternate WebCodecs-decodable artifact, present per variable when the
   * build had ffmpeg available. */
  video?: VideoBundleDescriptor;
  /** Tiny first-frame artifact for instant paint on variable switch. */
  poster?: PosterDescriptor;
  /** The bundle as a Zarr store, when the build derived one. */
  zarr?: ZarrStoreDescriptor;
}

export interface ForecastManifest {
  schemaVersion: 5;
  model: string;
  product: string;
  runTime: string;
  /** Last forecast hour of the run (120-hour and 240-hour runs both exist). */
  forecastHours: number;
  bundles: VariableBundleDescriptor[];
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("manifest node must be an object");
  }
  return value as Record<string, unknown>;
}

function timestamp(value: unknown, label: string): number {
  if (typeof value !== "string") throw new Error(`${label} timestamp missing`);
  const result = Date.parse(value);
  if (!Number.isFinite(result)) throw new Error(`${label} timestamp invalid`);
  return result;
}

const BUNDLE_SUFFIX = ".xue";

function relativePath(value: unknown, suffix: string | string[], paths: Set<string>, label: string): string {
  const suffixes = Array.isArray(suffix) ? suffix : [suffix];
  if (
    typeof value !== "string" ||
    !suffixes.some((candidate) => value.endsWith(candidate)) ||
    value.startsWith("/") ||
    value.startsWith("http:") ||
    value.startsWith("https:") ||
    value.split("/").includes("..")
  ) {
    throw new Error(`invalid ${label} path`);
  }
  if (paths.has(value)) throw new Error("duplicate bundle path");
  paths.add(value);
  return value;
}

function metadataJsonField(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`invalid ${label} metadata`);
  try {
    JSON.parse(value);
  } catch {
    throw new Error(`${label} metadata is not valid JSON`);
  }
  return value;
}

/** The `.xue` container fields of a bundle or a variant: `path`,
 * `byteLength` and `crc32` are one unit. Since the Zarr store the unit may
 * be absent as a whole — an entry names the container, the store, or both,
 * never neither — and a `path` that is present is still held to `.xue`. */
function validateContainerFields(entry: Record<string, unknown>, paths: Set<string>, label: string): void {
  if (entry.path === undefined) {
    if (entry.byteLength !== undefined || entry.crc32 !== undefined) {
      throw new Error(`${label} carries container fields without a path`);
    }
    if (entry.zarr === undefined) throw new Error(`${label} has neither a bundle path nor a zarr store`);
    return;
  }
  relativePath(entry.path, BUNDLE_SUFFIX, paths, label);
  if (typeof entry.byteLength !== "number" || !Number.isInteger(entry.byteLength) || entry.byteLength <= 0) {
    throw new Error(`invalid ${label} byteLength`);
  }
  if (typeof entry.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(entry.crc32)) {
    throw new Error(`invalid ${label} crc32`);
  }
}

function validateZarrDescriptor(input: unknown, paths: Set<string>): ZarrStoreDescriptor {
  const store = object(input);
  relativePath(store.path, ".zarr", paths, "zarr store");
  if (typeof store.byteLength !== "number" || !Number.isInteger(store.byteLength) || store.byteLength <= 0) {
    throw new Error("invalid zarr store byteLength");
  }
  if (typeof store.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(store.crc32)) {
    throw new Error("invalid zarr store crc32");
  }
  return store as unknown as ZarrStoreDescriptor;
}

function validateVariantDescriptor(input: unknown, paths: Set<string>): VariantDescriptor {
  const variant = object(input);
  validateContainerFields(variant, paths, "variant");
  for (const key of ["width", "height", "bandwidth"] as const) {
    if (typeof variant[key] !== "number" || !Number.isInteger(variant[key]) || (variant[key] as number) <= 0) {
      throw new Error(`invalid variant ${key}`);
    }
  }
  if (variant.zarr !== undefined) validateZarrDescriptor(variant.zarr, paths);
  return variant as unknown as VariantDescriptor;
}

function validatePosterDescriptor(input: unknown, paths: Set<string>): PosterDescriptor {
  const poster = object(input);
  relativePath(poster.path, ".poster.bin", paths, "poster");
  for (const key of ["width", "height", "byteLength"] as const) {
    if (typeof poster[key] !== "number" || !Number.isInteger(poster[key]) || (poster[key] as number) <= 0) {
      throw new Error(`invalid poster ${key}`);
    }
  }
  if (typeof poster.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(poster.crc32)) {
    throw new Error("invalid poster crc32");
  }
  metadataJsonField(poster.metadataJson, "poster");
  return poster as unknown as PosterDescriptor;
}

function validateVideoDescriptor(input: unknown, paths: Set<string>): VideoBundleDescriptor {
  const video = object(input);
  relativePath(video.streamPath, ".h264", paths, "video stream");
  relativePath(video.indexPath, ".h264.index.json", paths, "video index");
  if (typeof video.byteLength !== "number" || !Number.isInteger(video.byteLength) || video.byteLength <= 0) {
    throw new Error("invalid video byteLength");
  }
  if (typeof video.crc32 !== "string" || !/^[0-9a-f]{8}$/.test(video.crc32)) {
    throw new Error("invalid video crc32");
  }
  if (typeof video.codec !== "string" || video.codec.length === 0) throw new Error("invalid video codec string");
  for (const key of ["width", "height", "gop", "frameCount"] as const) {
    if (typeof video[key] !== "number" || !Number.isInteger(video[key]) || (video[key] as number) <= 0) {
      throw new Error(`invalid video ${key}`);
    }
  }
  metadataJsonField(video.metadataJson, "video");
  return video as unknown as VideoBundleDescriptor;
}

export interface ManifestValidationOptions {
  /** Whether the manifest must ship its dataset's core bundles
   * (`ForecastModelInfo.coreBundles`: the tmp2m and prate pair on a
   * forecast). True for a live run, which always covers every core
   * variable; false for a showcase case, which ships only the bundles its
   * event is about. */
  requireCoreVariables?: boolean;
}

export function validateManifest(
  input: unknown,
  expectedModel?: ForecastModelId,
  options: ManifestValidationOptions = {},
): ForecastManifest {
  const value = object(input);
  if (value.schemaVersion !== 5) throw new Error("unsupported manifest schema version");
  const modelInfo = modelForManifestString(value.model);
  if (!modelInfo || value.product !== modelInfo.product) throw new Error("unsupported manifest product");
  if (expectedModel !== undefined && modelInfo.id !== expectedModel) throw new Error("manifest model does not match the request");
  // A year of lead time. The bound is a sanity check on a number the shell
  // sizes readouts from, not a statement about what a run may publish: it
  // was 384 while the deepest horizon was a 16-day medium-range run, and
  // the seasonal model reaches 6552 hours (39 weeks) in one run. Widening
  // it is a two-sided deploy — an old cached shell refuses the new range —
  // so the shell goes out before a run in it does.
  if (
    typeof value.forecastHours !== "number" ||
    !Number.isInteger(value.forecastHours) ||
    value.forecastHours <= 0 ||
    value.forecastHours > 8760
  ) {
    throw new Error("invalid manifest forecast range");
  }
  timestamp(value.runTime, "runTime");

  if (!Array.isArray(value.bundles) || value.bundles.length === 0) throw new Error("manifest has no bundle list");
  const variables: string[] = [];
  const paths = new Set<string>();
  for (const item of value.bundles) {
    const bundle = object(item);
    // Structural, not a registry lookup: a name this build has never seen is
    // a layer it renders generically, not a manifest it refuses. Order is
    // not constrained either — the rail has its own.
    if (!isBundleVariableId(bundle.variable)) throw new Error("malformed bundle variable name");
    if (variables.includes(bundle.variable)) throw new Error("manifest contains duplicate variable bundles");
    validateContainerFields(bundle, paths, "bundle");
    if (bundle.variants !== undefined) {
      if (!Array.isArray(bundle.variants) || bundle.variants.length === 0) {
        throw new Error("invalid bundle variant list");
      }
      for (const variant of bundle.variants) validateVariantDescriptor(variant, paths);
    }
    if (bundle.video !== undefined) {
      validateVideoDescriptor(bundle.video, paths);
    }
    if (bundle.poster !== undefined) {
      validatePosterDescriptor(bundle.poster, paths);
    }
    if (bundle.zarr !== undefined) {
      validateZarrDescriptor(bundle.zarr, paths);
    }
    variables.push(bundle.variable);
  }
  if (options.requireCoreVariables ?? true) {
    for (const id of modelInfo.coreBundles ?? FORECAST_VARIABLE_IDS) {
      if (!variables.includes(id)) throw new Error(`manifest has no bundle for variable ${id}`);
    }
  }
  return value as unknown as ForecastManifest;
}

/** True when the manifest ships the given optional bundle (wind10m, dswrf). */
export function hasBundle(manifest: ForecastManifest, id: ForecastBundleId): boolean {
  return manifest.bundles.some((bundle) => bundle.variable === id);
}

/** True when the manifest ships the optional two-variable wind bundle. */
export function hasWindBundle(manifest: ForecastManifest): boolean {
  return hasBundle(manifest, "wind10m");
}

/** What the session asked for on the resolution ladder: let the viewport and
 * the connection decide (the default), or pin one end of it. `?res=` carries
 * this; see urlstate.ts. */
export type ResolutionPreference = "auto" | "half" | "full";

/** What one frame of a tier may cost in memory, for `pickBundleVariant`:
 * `cells` is the ceiling in grid cells per plane, `fullGrid` the canonical
 * tier's dimensions (the descriptor names those of every variant but not
 * of the bundle itself), and `visibleShare` the fraction of the grid the
 * view shows, in [0, 1] — a streaming session decodes only the viewport's
 * tiles, so what a frame costs on a zoomed-in view is that share of the
 * plane, though the plane itself is always allocated whole. */
export interface VariantBudget {
  cells: number;
  fullGrid: { width: number; height: number };
  visibleShare: number;
}

/** Tier selection (pure so it can be unit-tested): pick the reduced
 * rendition to load instead of the canonical full-resolution bundle, or null
 * to stay on full resolution.
 *
 * - A pinned `preference` short-circuits the heuristic: "full" never takes a
 *   variant, "half" always takes the smallest tier offered. A dataset that
 *   ships no variants at all (showcase cases) has only full resolution, so
 *   "half" gets it too rather than failing.
 * - On a constrained network the smallest tier always wins — the ladder
 *   exists exactly so those clients stop paying for pixels they cannot see.
 * - Otherwise pick the smallest tier that still covers `neededGridWidth`,
 *   the horizontal grid samples the current view can actually display
 *   across the whole world (world CSS width x devicePixelRatio); if no
 *   tier suffices, use full. A variant's width covers only the grid's own
 *   longitude span — 360° on a global grid, 70° on the MRMS mosaic — so
 *   the need is scaled to `longitudeSpan` before the comparison: a
 *   regional grid compared against the world's width would never take the
 *   half tier, however far out the view.
 * - Then, given a `budget`, the choice must also fit in memory: a tier
 *   costs `width × height × visibleShare` cells per plane, and one that
 *   costs more than `budget.cells` gives way to the largest rung that
 *   fits, or to the smallest rung when none does. The satellite sources
 *   are why: a 3000 × 3000 disk is nine million cells a plane, three
 *   planes a Dust RGB frame, and the view's pixel count alone would take
 *   the full tier at any zoom a phone can show — but zoomed in on a storm
 *   the view covers a sliver of the grid, the session decodes only that
 *   sliver's tiles, and the full tier is still the right answer. */
export function pickBundleVariant(
  variants: VariantDescriptor[] | undefined,
  neededGridWidth: number,
  constrained: boolean,
  preference: ResolutionPreference = "auto",
  longitudeSpan = 360,
  budget?: VariantBudget,
): VariantDescriptor | null {
  if (preference === "full") return null;
  if (!variants || variants.length === 0) return null;
  const sorted = [...variants].sort((a, b) => a.width - b.width);
  if (preference === "half" || constrained) return sorted[0] ?? null;
  const needed = (neededGridWidth * Math.min(360, Math.max(0, longitudeSpan))) / 360;
  const choice = sorted.find((variant) => variant.width >= needed) ?? null;
  if (!budget) return choice;
  const share = Math.min(1, Math.max(0, budget.visibleShare));
  const cost = (tier: VariantDescriptor | null): number =>
    (tier ? tier.width * tier.height : budget.fullGrid.width * budget.fullGrid.height) * share;
  if (cost(choice) <= budget.cells) return choice;
  // Only the rungs no wider than the choice are candidates: a wider one
  // was already more than the view could show.
  const candidates = choice ? sorted.filter((variant) => variant.width <= choice.width) : sorted;
  for (let index = candidates.length - 1; index >= 0; index -= 1) {
    const rung = candidates[index]!;
    if (cost(rung) <= budget.cells) return rung;
  }
  return sorted[0] ?? null;
}

/** Cells in a whole plane below which a grid counts as coarse: 512 × 512.
 * A global 0.25° grid (1440 × 721) is four times this and every finer grid
 * further still, so only the degree-scale grids fall under it. */
export const COARSE_GRID_CELLS = 512 * 512;

/** True when a bundle's canonical grid is coarse enough that stepping down
 * the ladder throws away detail the renderer needs rather than pixels the
 * view cannot show. `fullGrid` is `VariantBudget.fullGrid`, undefined when
 * the run says nothing about the grid — then the grid is treated as fine,
 * which is what every source before the degree-scale ones was. */
export function isCoarseGrid(fullGrid?: { width: number; height: number } | null): boolean {
  if (!fullGrid) return false;
  const cells = fullGrid.width * fullGrid.height;
  return cells > 0 && cells <= COARSE_GRID_CELLS;
}

/** The tier preference an overlay session opens on. An overlay draws over
 * a field the viewer is reading, so it normally takes the smallest rung and
 * leaves the bytes to the primary — contour lines are smoothed in the
 * shader anyway. On a coarse grid that reasoning inverts: a 1° field's half
 * tier is 0.5 samples per 2° of sky, and contours traced from it are a
 * staircase no amount of smoothing recovers, while the whole plane is a
 * rounding error against the budget. So a coarse grid's overlay opens full.
 * `?res=` is a pin and wins either way. */
export function overlayResolutionPreference(
  preference: ResolutionPreference,
  fullGrid?: { width: number; height: number } | null,
): ResolutionPreference {
  if (preference !== "auto") return preference;
  return isCoarseGrid(fullGrid) ? "full" : "half";
}

/** The tier a session already open on `current` should be on now that the
 * view has moved: `pickBundleVariant` again, with a dead band so a camera
 * resting near a boundary does not reopen the session on every nudge.
 * A change is taken only when it survives the inputs leaning against it
 * by `margin`: a finer rung must still be picked with the view needing
 * fewer columns and the budget tighter, a coarser one with the view
 * needing more and the budget looser. The pins and a constrained
 * connection pick a constant, so they never move. `current` is the
 * session's own descriptor, or null on the full tier; the answer is the
 * rung to be on, `current` itself when nothing changes. */
export function settleBundleVariant(
  current: VariantDescriptor | null,
  variants: VariantDescriptor[] | undefined,
  neededGridWidth: number,
  constrained: boolean,
  preference: ResolutionPreference = "auto",
  longitudeSpan = 360,
  budget?: VariantBudget,
  margin = 0.2,
): VariantDescriptor | null {
  const pick = (lean: number): VariantDescriptor | null =>
    pickBundleVariant(
      variants,
      neededGridWidth * lean,
      constrained,
      preference,
      longitudeSpan,
      budget ? { ...budget, cells: budget.cells * lean } : undefined,
    );
  // The full tier is the widest of all; a rung is as fine as it is wide.
  const width = (tier: VariantDescriptor | null): number => (tier ? tier.width : Number.POSITIVE_INFINITY);
  const plain = pick(1);
  if (width(plain) === width(current)) return current;
  const finer = width(plain) > width(current);
  const confirmed = pick(finer ? 1 - margin : 1 + margin);
  if (finer ? width(confirmed) > width(current) : width(confirmed) < width(current)) return confirmed;
  return current;
}

/** The fraction of a grid the map bounds show, in [0, 1] — the longitude
 * overlap times the latitude overlap, each over the grid's own extent — for
 * `VariantBudget.visibleShare`. The bounds are MapLibre's: `east ≥ west`,
 * and either may lie outside −180..180 once the map has been panned across
 * the antimeridian or zoomed out past one world; a grid may cross it too
 * (Himawari runs 80.7° to 200.7°). Longitude is therefore measured on the
 * circle: the view arc is laid down at every whole-turn offset that can
 * touch the grid arc and the overlaps summed, which is exact because a view
 * narrower than a turn never meets its own copy. A view a turn or wider
 * shows every longitude. A degenerate grid or bounds count as fully shown,
 * the answer that spends the least memory. */
export function visibleGridShare(
  grid: Pick<GeoGrid, "width" | "height" | "firstLongitude" | "firstLatitude" | "longitudeStep" | "latitudeStep">,
  bounds: ViewportBounds,
): number {
  const longitudeSpan = Math.abs(grid.width * grid.longitudeStep);
  const latitudeSpan = Math.abs(grid.height * grid.latitudeStep);
  const viewSpan = bounds.east - bounds.west;
  const viewHeight = bounds.north - bounds.south;
  if (
    !(longitudeSpan > 0) ||
    !(latitudeSpan > 0) ||
    !Number.isFinite(viewSpan) ||
    !Number.isFinite(viewHeight) ||
    viewSpan < 0 ||
    viewHeight < 0
  ) {
    return 1;
  }
  const gridWest = Math.min(grid.firstLongitude, grid.firstLongitude + grid.width * grid.longitudeStep);
  const gridEast = gridWest + longitudeSpan;
  let longitudeVisible = 0;
  if (viewSpan >= 360) {
    longitudeVisible = longitudeSpan;
  } else {
    const firstTurn = Math.floor((gridWest - bounds.east) / 360);
    const lastTurn = Math.ceil((gridEast - bounds.west) / 360);
    for (let turn = firstTurn; turn <= lastTurn; turn += 1) {
      const overlap =
        Math.min(gridEast, bounds.east + turn * 360) - Math.max(gridWest, bounds.west + turn * 360);
      if (overlap > 0) longitudeVisible += overlap;
    }
  }
  const gridSouth = Math.min(grid.firstLatitude, grid.firstLatitude + grid.height * grid.latitudeStep);
  const gridNorth = gridSouth + latitudeSpan;
  const latitudeVisible = Math.max(0, Math.min(gridNorth, bounds.north) - Math.max(gridSouth, bounds.south));
  const share = (longitudeVisible / Math.min(360, longitudeSpan)) * (latitudeVisible / latitudeSpan);
  return Math.min(1, Math.max(0, share));
}

// ---------------------------------------------------------------------------
// Live pointer (latest.json). Two-layer delivery: the
// only mutable object is this tiny pointer; the run manifest and every heavy
// artifact it names are immutable and cache-busted via ?v=<crc32>.

export interface LatestPointer {
  schemaVersion: 1;
  model: string;
  run: string;
  runTime: string;
  manifestPath: string;
  manifestCrc32: string;
}

export function validateLatestPointer(input: unknown, expectedModel?: ForecastModelId): LatestPointer {
  const value = object(input);
  if (value.schemaVersion !== 1) throw new Error("unsupported live pointer schema version");
  const modelInfo = modelForManifestString(value.model);
  if (!modelInfo || value.product !== modelInfo.product) throw new Error("unsupported live pointer product");
  if (expectedModel !== undefined && modelInfo.id !== expectedModel) throw new Error("live pointer model does not match the request");
  if (typeof value.run !== "string" || !/^\d{10}$/.test(value.run)) throw new Error("invalid live pointer run id");
  timestamp(value.runTime, "live pointer");
  relativePath(value.manifestPath, "manifest.json", new Set(), "manifest");
  if (typeof value.manifestCrc32 !== "string" || !/^[0-9a-f]{8}$/.test(value.manifestCrc32)) {
    throw new Error("invalid live pointer manifest crc32");
  }
  return value as unknown as LatestPointer;
}

export async function fetchLatestPointer(baseUrl: string, model: ForecastModelId = "gfs"): Promise<LatestPointer> {
  const latestFilename = FORECAST_MODELS[model].latestFilename;
  if (latestFilename === undefined) throw new Error(`${model} has no live feed`);
  const url = new URL(`${baseUrl}${latestFilename}`, document.baseURI);
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) throw new Error(t("pointerRequestFailed", { status: response.status }));
  return validateLatestPointer(await response.json(), model);
}

export interface LoadedManifest {
  manifest: ForecastManifest;
  latest: LatestPointer;
  /** Absolute manifest URL (with its ?v=); artifact paths resolve against it. */
  manifestUrl: string;
}

/** A live manifest this build's validator refused. Carries the pointer's
 * crc32 so the shell can tell a manifest it will never accept from one a
 * newer build would: a run published in a widened schema reaches a tab
 * opened before the shell that reads it, and the tab's cure is a reload,
 * once (main.ts). */
export class ManifestRejectedError extends Error {
  constructor(
    message: string,
    readonly manifestCrc32: string,
  ) {
    super(message);
    this.name = "ManifestRejectedError";
  }
}

export async function fetchManifest(baseUrl: string, model: ForecastModelId = "gfs"): Promise<LoadedManifest> {
  const latest = await fetchLatestPointer(baseUrl, model);
  const url = new URL(latest.manifestPath, new URL(baseUrl, document.baseURI));
  url.searchParams.set("v", latest.manifestCrc32);
  const response = await fetchImmutable(url);
  if (!response.ok) throw new Error(t("manifestRequestFailed", { status: response.status }));
  const payload: unknown = await response.json();
  let manifest: ForecastManifest;
  try {
    manifest = validateManifest(payload, model);
  } catch (error) {
    throw new ManifestRejectedError(error instanceof Error ? error.message : String(error), latest.manifestCrc32);
  }
  return { manifest, latest, manifestUrl: url.href };
}

// ---------------------------------------------------------------------------
// Bundle metadata (embedded UTF-8 JSON inside the .xue file).

export interface LinearQuantization {
  type: "linear";
  offset: number;
  scale: number;
  minimumCode: number;
  maximumCode: number;
  nodataCode: number;
}

export interface LogQuantization {
  type: "log1p";
  trace: number;
  scale: number;
  maximum: number;
  minimumCode: number;
  maximumCode: number;
  zeroCode: number;
  overflowCode: number;
  nodataCode: number;
}

/** What a variable *is*, in GRIB2's own terms: the parameter triple and the
 * fixed surface it sits on, plus the statistical process a derived field
 * carries. Introduced by bundle metadata schemaVersion 3; absent below.
 * A surface with no value (entire atmosphere) writes both halves of the
 * value as null, the way GRIB2 writes them missing. */
export interface BundleParameter {
  discipline: number;
  parameterCategory: number;
  parameterNumber: number;
  typeOfFirstFixedSurface: number;
  scaleFactorOfFirstFixedSurface: number | null;
  scaledValueOfFirstFixedSurface: number | null;
  /** Code table 4.10; absent for an instantaneous field. */
  typeOfStatisticalProcessing?: number;
}

/** The spectral band a satellite image variable was measured in, in the
 * fields of GRIB2 product definition template 4.31: the spacecraft and
 * instrument by their WMO common code table numbers (C-5 and C-8) and the
 * band's central wave number, `scaledValue × 10^-scaleFactor` m⁻¹. Optional
 * beside `parameter` from schemaVersion 3; a chart classifies a channel by
 * its wave number, never by the spacecraft (docs/format.md §"Band and
 * Producer"). */
export interface BundleBand {
  satelliteSeries: number;
  satelliteNumber: number;
  instrumentType: number;
  scaleFactorOfCentralWaveNumber: number;
  scaledValueOfCentralWaveNumber: number;
}

/** The algorithm that derived a composite field from other variables; its
 * `parameter` is a local-use number that means something only together
 * with `id`. Optional beside `parameter` from schemaVersion 3. */
export interface BundleProducer {
  id: string;
  version: string;
}

/** What kind of aerosol a variable is a measure of, in the fields of GRIB2
 * product definition template 4.48: the species (code table 4.233), the
 * particle size interval and the wavelength interval, each interval a type
 * (code table 4.91) with two limits as `scaledValue × 10^-scaleFactor`
 * metres. A limit pair is both integers or both null, and an interval of
 * type 255 (missing — the particulate matter fields have no wavelength)
 * has both its pairs null. Optional beside `parameter` from schemaVersion
 * 3; the parameter alone is one number for every species' optical depth,
 * so this block is the rest of such a variable's identity (docs/format.md,
 * the aerosol block beside `band` and `producer`). */
export interface BundleAerosol {
  aerosolType: number;
  typeOfSizeInterval: number;
  scaleFactorOfFirstSize: number | null;
  scaledValueOfFirstSize: number | null;
  scaleFactorOfSecondSize: number | null;
  scaledValueOfSecondSize: number | null;
  typeOfWavelengthInterval: number;
  scaleFactorOfFirstWavelength: number | null;
  scaledValueOfFirstWavelength: number | null;
  scaleFactorOfSecondWavelength: number | null;
  scaledValueOfSecondWavelength: number | null;
}

export interface BundleVariable {
  numericId: number;
  id: DataVariableId;
  label: string;
  unit: string;
  /** Present from schemaVersion 3 onwards. */
  parameter?: BundleParameter;
  /** A satellite channel's band, when the variable is one. */
  band?: BundleBand;
  /** The producer of a derived (composite) field, when the variable is one. */
  producer?: BundleProducer;
  /** The species, size and wavelength of an aerosol field, when the
   * variable is one. */
  aerosol?: BundleAerosol;
  quantization: LinearQuantization | LogQuantization;
}

/** The bundle time axis.
 *
 * Metadata schemaVersion 3 states it in units it names: `unitSeconds` (an
 * hour for every forecast source, six minutes for the radar mosaic) plus
 * offsets in that unit, exactly one of `frameStep` (uniform) and
 * `frameOffsets` (listed outright). Versions 1 and 2 used a whole-hour axis
 * with `firstForecastHour` plus `stepHours` or `hours`; both shapes are
 * still read, and `axisUnitSeconds`/`frameOffsets` normalize them
 * (docs/format.md). */
export interface BundleTimeAxis {
  frameCount: number;
  /** Schema v3. */
  unitSeconds?: number;
  firstFrameOffset?: number;
  frameStep?: number;
  frameOffsets?: number[];
  /** Schema v1 and v2. */
  firstForecastHour?: number;
  stepHours?: number;
  hours?: number[];
}

export const HOUR_SECONDS = 3600;

export interface BundleMetadata {
  schemaVersion: 1 | 2 | 3;
  model: string;
  runTime: string;
  time: BundleTimeAxis;
  grid: { width: number; height: number };
  variables: BundleVariable[];
}

/** Seconds per axis unit. A v1/v2 axis is always whole hours. */
export function axisUnitSeconds(time: BundleTimeAxis): number {
  return time.unitSeconds ?? HOUR_SECONDS;
}

/** The materialized frame-offset list of a bundle time axis. Multiply by
 * `axisUnitSeconds` for seconds from the run time. */
export function frameOffsets(time: BundleTimeAxis): number[] {
  if (time.frameOffsets) return time.frameOffsets;
  if (time.hours) return time.hours;
  const first = time.firstFrameOffset ?? time.firstForecastHour ?? 0;
  const step = time.frameStep ?? time.stepHours ?? 1;
  return Array.from({ length: time.frameCount }, (_, index) => first + index * step);
}

/** True when two time axes describe the same frames at the same instants. */
export function sameTimeAxis(a: BundleTimeAxis, b: BundleTimeAxis): boolean {
  if (a === b) return true;
  if (axisUnitSeconds(a) !== axisUnitSeconds(b)) return false;
  const offsetsA = frameOffsets(a);
  const offsetsB = frameOffsets(b);
  return offsetsA.length === offsetsB.length && offsetsA.every((offset, index) => offset === offsetsB[index]);
}

const V3_TIME_FIELDS = ["unitSeconds", "firstFrameOffset", "frameStep", "frameOffsets"] as const;

/** The lowest schema version able to express a time axis: 1 for a uniform
 * whole-hour axis declaring `stepHours`, 2 for one listing its `hours`, and 3
 * for the unit-neutral block. */
function validateTimeAxis(time: Record<string, unknown>, schemaVersion: number): 1 | 2 | 3 {
  const frameCount = time.frameCount;
  if (typeof frameCount !== "number" || !Number.isInteger(frameCount) || frameCount <= 0) {
    throw new Error("invalid bundle time axis");
  }
  if (schemaVersion >= 3) return validateOffsetAxis(time, frameCount);
  if (V3_TIME_FIELDS.some((field) => field in time)) {
    throw new Error("a unit-neutral time axis requires schema version 3");
  }
  const firstForecastHour = time.firstForecastHour;
  if (typeof firstForecastHour !== "number" || !Number.isInteger(firstForecastHour) || firstForecastHour < 0) {
    throw new Error("invalid bundle time axis");
  }
  const stepHours = time.stepHours;
  const hours = time.hours;
  // Exactly one encoding per axis: a uniform axis declares stepHours, an
  // axis that changes step lists its hours outright.
  if ((stepHours === undefined) === (hours === undefined)) throw new Error("invalid bundle time axis");
  if (stepHours !== undefined) {
    if (typeof stepHours !== "number" || !Number.isInteger(stepHours) || stepHours <= 0) {
      throw new Error("invalid bundle time axis");
    }
    return 1;
  }
  if (
    !Array.isArray(hours) ||
    hours.length !== frameCount ||
    hours[0] !== firstForecastHour ||
    hours.some(
      (hour, index) =>
        typeof hour !== "number" ||
        !Number.isInteger(hour) ||
        hour > 65534 ||
        (index > 0 && hour <= (hours[index - 1] as number)),
    )
  ) {
    throw new Error("invalid bundle time axis");
  }
  checkListedSteps(hours as number[]);
  return 2;
}

function checkListedSteps(listed: number[]): void {
  const steps = new Set<number>();
  for (let index = 1; index < listed.length; index += 1) steps.add(listed[index]! - listed[index - 1]!);
  if (steps.size < 2) throw new Error("a uniform axis must be encoded as a step");
}

function gcd(a: number, b: number): number {
  return b === 0 ? a : gcd(b, a % b);
}

/** The schemaVersion 3 time block: offsets on a declared unit. */
function validateOffsetAxis(time: Record<string, unknown>, frameCount: number): 3 {
  for (const key of Object.keys(time)) {
    if (key !== "frameCount" && !V3_TIME_FIELDS.includes(key as (typeof V3_TIME_FIELDS)[number])) {
      throw new Error("bundle time block has an unknown field");
    }
  }
  const unitSeconds = time.unitSeconds;
  if (
    typeof unitSeconds !== "number" ||
    !Number.isInteger(unitSeconds) ||
    unitSeconds < 1 ||
    unitSeconds > HOUR_SECONDS ||
    HOUR_SECONDS % unitSeconds !== 0
  ) {
    throw new Error("bundle unitSeconds must be a whole divisor of 3600");
  }
  const first = time.firstFrameOffset;
  if (typeof first !== "number" || !Number.isInteger(first) || first < 0) {
    throw new Error("invalid bundle firstFrameOffset");
  }
  const frameStep = time.frameStep;
  const listed = time.frameOffsets;
  if ((frameStep === undefined) === (listed === undefined)) throw new Error("invalid bundle time axis");
  let offsets: number[];
  if (frameStep !== undefined) {
    if (typeof frameStep !== "number" || !Number.isInteger(frameStep) || frameStep <= 0) {
      throw new Error("invalid bundle frameStep");
    }
    if (first + (frameCount - 1) * frameStep > 65534) throw new Error("frame offsets exceed the u16 range");
    offsets = Array.from({ length: frameCount }, (_, index) => first + index * frameStep);
  } else {
    if (
      !Array.isArray(listed) ||
      listed.length !== frameCount ||
      listed[0] !== first ||
      listed.some(
        (offset, index) =>
          typeof offset !== "number" ||
          !Number.isInteger(offset) ||
          offset > 65534 ||
          (index > 0 && offset <= (listed[index - 1] as number)),
      )
    ) {
      throw new Error("invalid bundle time axis");
    }
    checkListedSteps(listed as number[]);
    offsets = listed as number[];
  }
  // The unit is the coarsest one that expresses every offset exactly, so an
  // axis has one encoding rather than one per divisor of its step.
  if (offsets.reduce((divisor, offset) => gcd(divisor, offset), HOUR_SECONDS / unitSeconds) !== 1) {
    throw new Error("bundle unitSeconds is finer than the axis needs");
  }
  return 3;
}

const PARAMETER_CODE_FIELDS = [
  "discipline",
  "parameterCategory",
  "parameterNumber",
  "typeOfFirstFixedSurface",
] as const;
// Present but nullable: a surface with no value writes both halves as null,
// and only a derived field carries a statistical process at all.
const PARAMETER_NULLABLE_FIELDS = [
  "scaleFactorOfFirstFixedSurface",
  "scaledValueOfFirstFixedSurface",
  "typeOfStatisticalProcessing",
] as const;

/** Validate a variable's GRIB2 identity block, which schemaVersion 3
 * introduces: required at version 3, forbidden below. */
function validateParameter(parameter: unknown, schemaVersion: number): void {
  if (schemaVersion < 3) {
    if (parameter !== undefined) throw new Error("a GRIB2 parameter block requires schema version 3");
    return;
  }
  if (typeof parameter !== "object" || parameter === null || Array.isArray(parameter)) {
    throw new Error("schema version 3 requires a parameter block on every variable");
  }
  const block = parameter as Record<string, unknown>;
  const known = new Set<string>([...PARAMETER_CODE_FIELDS, ...PARAMETER_NULLABLE_FIELDS]);
  for (const key of Object.keys(block)) {
    if (!known.has(key)) throw new Error("bundle parameter block has an unknown field");
  }
  for (const field of PARAMETER_CODE_FIELDS) {
    const code = block[field];
    if (typeof code !== "number" || !Number.isInteger(code) || code < 0 || code > 255) {
      throw new Error(`invalid bundle parameter ${field}`);
    }
  }
  if (!("scaleFactorOfFirstFixedSurface" in block) || !("scaledValueOfFirstFixedSurface" in block)) {
    throw new Error("bundle parameter fixed surface value is incomplete");
  }
  const scaleFactor = block.scaleFactorOfFirstFixedSurface;
  const scaledValue = block.scaledValueOfFirstFixedSurface;
  // GRIB2 encodes a surface with no value by writing both as missing.
  if ((scaleFactor === null) !== (scaledValue === null)) {
    throw new Error("bundle parameter fixed surface must be wholly present or wholly null");
  }
  if (scaleFactor !== null && (!Number.isInteger(scaleFactor) || !Number.isInteger(scaledValue))) {
    throw new Error("invalid bundle parameter fixed surface value");
  }
  const statistical = block.typeOfStatisticalProcessing;
  if (statistical !== undefined && (!Number.isInteger(statistical) || (statistical as number) < 0)) {
    throw new Error("invalid bundle parameter typeOfStatisticalProcessing");
  }
}

// The optional blocks beside `parameter`, each present whole or absent,
// valid from schemaVersion 3 and raising no version floor.
const BAND_FIELDS: ReadonlyArray<readonly [keyof BundleBand, number, number]> = [
  ["satelliteSeries", 0, 0xffff],
  ["satelliteNumber", 0, 0xffff],
  ["instrumentType", 0, 0xffff],
  ["scaleFactorOfCentralWaveNumber", -127, 127],
  ["scaledValueOfCentralWaveNumber", 0, 0xfffffffe],
];
const PRODUCER_ID = /^[a-z][a-z0-9]*$/;

function validateBand(variable: Record<string, unknown>, schemaVersion: number): void {
  if (!("band" in variable)) return;
  if (schemaVersion < 3) throw new Error("a band block requires schema version 3");
  const band = object(variable.band);
  if (Object.keys(band).length !== BAND_FIELDS.length) {
    throw new Error("bundle band block must carry exactly its five fields");
  }
  for (const [field, low, high] of BAND_FIELDS) {
    const value = band[field];
    if (typeof value !== "number" || !Number.isInteger(value) || value < low || value > high) {
      throw new Error(`invalid bundle band ${field}`);
    }
  }
}

function validateProducer(variable: Record<string, unknown>, schemaVersion: number): void {
  if (!("producer" in variable)) return;
  if (schemaVersion < 3) throw new Error("a producer block requires schema version 3");
  const producer = object(variable.producer);
  if (Object.keys(producer).length !== 2) {
    throw new Error("bundle producer block must carry exactly id and version");
  }
  if (typeof producer.id !== "string" || !PRODUCER_ID.test(producer.id)) {
    throw new Error("invalid bundle producer id");
  }
  if (typeof producer.version !== "string" || producer.version.length === 0) {
    throw new Error("invalid bundle producer version");
  }
}

/** The interval types of the aerosol block, each with the two limit pairs
 * it governs, in the block's own order. */
const AEROSOL_INTERVALS: ReadonlyArray<
  readonly [type: keyof BundleAerosol, pairs: ReadonlyArray<readonly [scale: keyof BundleAerosol, value: keyof BundleAerosol]>]
> = [
  [
    "typeOfSizeInterval",
    [
      ["scaleFactorOfFirstSize", "scaledValueOfFirstSize"],
      ["scaleFactorOfSecondSize", "scaledValueOfSecondSize"],
    ],
  ],
  [
    "typeOfWavelengthInterval",
    [
      ["scaleFactorOfFirstWavelength", "scaledValueOfFirstWavelength"],
      ["scaleFactorOfSecondWavelength", "scaledValueOfSecondWavelength"],
    ],
  ],
];
const AEROSOL_FIELD_COUNT = 11;
/** Code table 4.91's "missing": an interval the record does not carry. */
const MISSING_INTERVAL = 255;

function validateAerosol(variable: Record<string, unknown>, schemaVersion: number): void {
  if (!("aerosol" in variable)) return;
  if (schemaVersion < 3) throw new Error("an aerosol block requires schema version 3");
  const aerosol = object(variable.aerosol);
  if (Object.keys(aerosol).length !== AEROSOL_FIELD_COUNT) {
    throw new Error("bundle aerosol block must carry exactly its eleven fields");
  }
  const code = (field: keyof BundleAerosol, high: number): number => {
    const value = aerosol[field];
    if (typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > high) {
      throw new Error(`invalid bundle aerosol ${field}`);
    }
    return value;
  };
  code("aerosolType", 0xffff);
  for (const [typeField, pairs] of AEROSOL_INTERVALS) {
    const type = code(typeField, 0xff);
    for (const [scaleField, valueField] of pairs) {
      if (!(scaleField in aerosol) || !(valueField in aerosol)) {
        throw new Error(`bundle aerosol ${scaleField} pair is incomplete`);
      }
      const scale = aerosol[scaleField];
      const value = aerosol[valueField];
      // A limit is written the way GRIB2 writes a missing one: both halves
      // at once — and an interval the record does not carry has none.
      if ((scale === null) !== (value === null)) {
        throw new Error(`bundle aerosol ${scaleField} pair must be wholly present or wholly null`);
      }
      if (type === MISSING_INTERVAL) {
        if (scale !== null) throw new Error(`bundle aerosol ${typeField} is missing but carries limits`);
        continue;
      }
      if (
        typeof scale !== "number" ||
        !Number.isInteger(scale) ||
        scale < -127 ||
        scale > 127 ||
        typeof value !== "number" ||
        !Number.isInteger(value) ||
        value < 0 ||
        value > 0xfffffffe
      ) {
        throw new Error(`invalid bundle aerosol ${scaleField} pair`);
      }
    }
  }
}

export function parseBundleMetadata(json: string): BundleMetadata {
  const value = object(JSON.parse(json));
  const schemaVersion = value.schemaVersion;
  if (schemaVersion !== 1 && schemaVersion !== 2 && schemaVersion !== 3) {
    throw new Error("unsupported bundle metadata schema version");
  }
  const time = object(value.time);
  const grid = object(value.grid);
  const axisVersion = validateTimeAxis(time, schemaVersion);
  if (typeof grid.width !== "number" || typeof grid.height !== "number") {
    throw new Error("invalid bundle grid");
  }
  const variables = value.variables;
  if (!Array.isArray(variables) || variables.length === 0) throw new Error("bundle metadata has no variables");
  let parameters = 0;
  for (const item of variables) {
    const variable = object(item);
    if (typeof variable.numericId !== "number" || typeof variable.id !== "string") {
      throw new Error("invalid bundle variable descriptor");
    }
    validateParameter(variable.parameter, schemaVersion);
    if (variable.parameter !== undefined) parameters += 1;
    validateBand(variable, schemaVersion);
    validateProducer(variable, schemaVersion);
    validateAerosol(variable, schemaVersion);
    const quantization = object(variable.quantization);
    if (quantization.type !== "linear" && quantization.type !== "log1p") {
      throw new Error("unsupported bundle quantization");
    }
  }
  // Every axis and every variable set has exactly one valid encoding: the
  // declared version must be the lowest able to express both.
  const requiredVersion = Math.max(axisVersion, parameters > 0 ? 3 : 1);
  if (schemaVersion !== requiredVersion) {
    throw new Error(`bundle metadata must declare schema version ${requiredVersion}`);
  }
  return value as unknown as BundleMetadata;
}
