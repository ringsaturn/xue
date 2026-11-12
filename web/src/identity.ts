import type { BundleAerosol, BundleBand, BundleEnsemble, BundleParameter, BundleProducer, BundleVariable, KnownBundleId } from "./manifest";
import { REGISTRY } from "./registry";
import { SPREAD_FAMILIES, SPREAD_KEY, chartsAsQuantity, statisticKey, statisticProbability } from "./statistics";
import { isobaricChartFamily, specForIdentity, variableSpec } from "./variables";

/**
 * What a variable *is*, read off the file rather than off its name.
 *
 * From bundle metadata schemaVersion 3 every variable carries its GRIB2
 * `parameter` block — discipline / category / number plus the fixed surface —
 * and that block is the variable's real identity. The id string beside it is
 * a naming convention (`<family><level>`, plus `tmp2m` / `wind10m` / `prmsl`
 * for the surface members), useful before a bundle is open and nothing more.
 *
 * Everything the shell knows about a field as a *chart* — which palette,
 * which legend range, which contour interval, which magnitude ceiling — is
 * keyed by the `(family, level)` pair below, so a bundle named anything at
 * all still draws correctly as long as its parameters are ones we know.
 *
 * Files written at schemaVersion 1 or 2 (published runs and showcase cases,
 * never rebuilt) have no parameter block at all. `LEGACY_IDENTITIES` covers
 * exactly the ids those files can carry and nothing more.
 */

/** The chart families. Those up to `cat` are registered on isobaric
 * surfaces (the clear-air turbulence on the three jet levels only) and some
 * of them also have a near-surface member; the rest are single
 * layers (precipitation, radiation, reflectivity, the surface diagnostics:
 * gust, the four cloud covers, CAPE, visibility, dew point, apparent
 * temperature — and the ocean set: skin temperature, sea ice cover and
 * thickness, wave height, period and direction, and the wave vector, the
 * height laid along the direction of travel as a u/v pair, and the same
 * three scalars and vector for the wind sea and the first swell — and the
 * satellite channels, one family per nominal wavelength: the brightness
 * temperature parameter is the same for every infrared band, and the
 * `band` block beside it says which — the satellite composites, a
 * three-gun colour picture a producer derived from several channels, told
 * apart by the `producer` block and the local-use parameter numbers — and
 * the produced scalars, one field a producer derived from several channels
 * (the DEBRA and ZHOUYE dust confidences), told apart the same way — and
 * the aerosol
 * set: the optical depth at 550 nm, one family for the whole column and one
 * per species, and the surface particulate matter by size cut, told apart
 * by the `aerosol` block beside a parameter that is the same for every
 * species — and the hydrology set: the river discharge, and the
 * return-period class and upstream area a producer derived beside it —
 * and the two ensemble statistics that are not their quantity: the spread
 * of the pressure family's surfaces (`hgtsd`, at MSL and on the isobaric
 * levels like `hgt`), and the probability of the precipitation total past
 * a threshold (`pop`), told apart by the `ensemble` block). */
export type ChartFamily =
  | "hgt"
  | "tmp"
  | "rh"
  | "spfh"
  | "wind"
  | "wind80m"
  | "wind100m"
  | "qflux"
  | "vvel"
  | "thetae"
  | "cat"
  | "prate"
  | "dswrf"
  | "cref"
  | "orog"
  | "gust"
  | "tcdc"
  | "lcdc"
  | "mcdc"
  | "hcdc"
  | "cape"
  | "cin"
  | "vis"
  | "dpt2m"
  | "aptmp2m"
  | "pwat"
  | "hpbl"
  | "ptype"
  | "ptypeec"
  | "cpofp"
  | "fzlvl"
  | "tmpsfc"
  | "icec"
  | "icetk"
  | "htsgw"
  | "perpw"
  | "dirpw"
  | "wave"
  | "wvhgt"
  | "wvper"
  | "wvdir"
  | "swhgt"
  | "swper"
  | "swdir"
  | "windwave"
  | "swell"
  | "ir104"
  | "ir039"
  | "dustrgb"
  | "dustcf"
  | "zhouye"
  | "truecolor"
  | "aod"
  | "aoddust"
  | "aodsalt"
  | "aodsulf"
  | "aodorg"
  | "aodbc"
  | "pm25"
  | "pm10"
  | "pm10dust"
  | "dustsfc"
  | "smokesfc"
  | "dustfine"
  | "dustcoarse"
  | "aotk"
  | "aurora"
  | "clw"
  | "dis06"
  | "disrp"
  | "upstream"
  | "hgtsd"
  | "pop"
  | SoilChartFamily;

/** The soil layers, one chart family per layer like the cloud layers: the
 * same quantity at four depths, told apart by the fixed surface's value
 * rather than an isobaric level, so the layer is in the family and
 * `level` stays the hectopascals it is everywhere else. */
export type SoilChartFamily =
  | "tsoil1"
  | "tsoil2"
  | "tsoil3"
  | "tsoil4"
  | "soilw1"
  | "soilw2"
  | "soilw3"
  | "soilw4";

export interface VariableIdentity {
  family: ChartFamily;
  /** Isobaric surface in hPa, or null for a surface member (2 m temperature,
   * 10 m wind, mean sea level pressure) and for a single layer. */
  level: number | null;
  /** True when the bundle carries a u/v component pair rather than a scalar. */
  vector: boolean;
  /** The ensemble statistic the field is (`statistics.ts`: `q75`, `mean`,
   * `spread`, `p3:10:`), absent for a single forecast. Two fields that
   * differ only here are different fields. */
  statistic?: string;
}

function scalar(family: ChartFamily, level: number | null): VariableIdentity {
  return { family, level, vector: false };
}

function vector(family: ChartFamily, level: number | null): VariableIdentity {
  return { family, level, vector: true };
}

/** The composites: a family whose bundle is three colour guns rather than
 * one scalar or a u/v pair, drawn straight as red, green and blue — the
 * Dust RGB and the true colour picture.
 * An identity's `vector` flag is false for them; this is what says the
 * third texture mode is theirs. */
const COMPOSITE_FAMILIES: readonly ChartFamily[] = ["dustrgb", "truecolor"];

/** True when an identity names a colour composite (three guns). */
export function isCompositeIdentity(identity: VariableIdentity | null): boolean {
  return identity !== null && !identity.vector && COMPOSITE_FAMILIES.includes(identity.family);
}

export function sameIdentity(a: VariableIdentity | null, b: VariableIdentity | null): boolean {
  if (a === null || b === null) return a === b;
  return a.family === b.family && a.level === b.level && a.vector === b.vector && a.statistic === b.statistic;
}

/** Whether two identities draw as the same chart: the same field, or one a
 * mean or percentile of the other's quantity — the name an ensemble's
 * median is published under says nothing of its statistic, and charts as
 * the quantity. */
export function sameChart(a: VariableIdentity | null, b: VariableIdentity | null): boolean {
  if (a === null || b === null) return a === b;
  if (a.family !== b.family || a.level !== b.level || a.vector !== b.vector) return false;
  if (a.statistic === b.statistic) return true;
  return a.statistic === undefined ? chartsAsQuantity(b.statistic) : b.statistic === undefined && chartsAsQuantity(a.statistic);
}

/** The registered probability fields: a parameter on a precipitation
 * total and an `ensemble` block of the probability shape. Their
 * parameter is what a probability on the same quantity is matched by;
 * the threshold is the block's, carried in the statistic. */
const PROBABILITY_FIELDS = (Object.keys(REGISTRY.variables) as RegistryVariableId[]).filter((id) => {
  const ensemble = (REGISTRY.variables[id] as { ensemble?: object }).ensemble;
  return ensemble !== undefined && "probabilityType" in ensemble;
});

/**
 * What a field is once its `ensemble` block is read: `base` is the identity
 * of the quantity the parameter names (null when the shell has no chart for
 * it). A mean or a percentile is that quantity under its statistic; the
 * spread of a family with a spread chart is that chart; a probability on a
 * registered probability field's parameter is the probability chart; any
 * other spread or probability is a field this build does not chart, never
 * the quantity itself.
 */
export function identityForEnsemble(
  base: VariableIdentity | null,
  parameter: BundleParameter,
  ensemble: BundleEnsemble | undefined,
): VariableIdentity | null {
  if (ensemble === undefined) return base;
  const statistic = statisticKey(ensemble);
  if (statisticProbability(statistic)) {
    // The threshold is the statistic's; the interval the total is over is
    // no more the identity's than a rate's step is.
    const known = PROBABILITY_FIELDS.some((id) => isOnSurfaceOf(parameter, id));
    return known ? { family: "pop", level: null, vector: false, statistic } : null;
  }
  if (base === null) return null;
  if (statistic === SPREAD_KEY) {
    const family = SPREAD_FAMILIES[base.family];
    return family === undefined || base.vector ? null : { family, level: base.level, vector: false, statistic };
  }
  return { ...base, statistic };
}

/** The fixed surface's value in its own unit, or null where GRIB2 writes the
 * surface as having none (both halves missing — the entire atmosphere). */
function surfaceValue(parameter: BundleParameter): number | null {
  const factor = parameter.scaleFactorOfFirstFixedSurface;
  const value = parameter.scaledValueOfFirstFixedSurface;
  if (factor === null || factor === undefined || value === null || value === undefined) return null;
  return value * 10 ** -factor;
}

/** The isobaric surface in hPa, or null when the parameter does not sit on
 * one. GDAL spells an isobaric level in pascals, so the hectopascals a chart
 * is read in are that over a hundred. */
function isobaricLevel(parameter: BundleParameter): number | null {
  if (parameter.typeOfFirstFixedSurface !== 100) return null;
  const pascals = surfaceValue(parameter);
  if (pascals === null || !Number.isFinite(pascals) || pascals <= 0) return null;
  // Float noise from a negative scale factor would otherwise make 850 hPa
  // 850.0000000001 and miss every level-keyed lookup.
  return Math.round((pascals / 100) * 1e6) / 1e6;
}

function isTriple(parameter: BundleParameter, discipline: number, category: number, number_: number): boolean {
  return (
    parameter.discipline === discipline &&
    parameter.parameterCategory === category &&
    parameter.parameterNumber === number_
  );
}

type RegistryVariableId = keyof typeof REGISTRY.variables;

/** A registered variable's parameter block (`registry.ts`): the numbers
 * every rule below compares against are the encoders' own. */
function registered(id: RegistryVariableId): BundleParameter {
  return REGISTRY.variables[id].parameter;
}

/** A registered variable's discipline / category / number. */
function tripleOf(id: RegistryVariableId): readonly [number, number, number] {
  const parameter = registered(id);
  return [parameter.discipline, parameter.parameterCategory, parameter.parameterNumber];
}

/** True when a parameter is the registered variable's quantity, on any
 * surface: the level of an isobaric family is read off the parameter. */
function isQuantityOf(parameter: BundleParameter, id: RegistryVariableId): boolean {
  return isTriple(parameter, ...tripleOf(id));
}

/** True when a parameter is the registered variable's quantity on its type
 * of surface, whatever value the surface carries. */
function isOnSurfaceOf(parameter: BundleParameter, id: RegistryVariableId): boolean {
  return isQuantityOf(parameter, id) && parameter.typeOfFirstFixedSurface === registered(id).typeOfFirstFixedSurface;
}

/** True when a parameter is the registered variable's quantity on its
 * surface, the surface's value included (2 m, 10 m, the first swell). */
function isAtSurfaceOf(parameter: BundleParameter, id: RegistryVariableId): boolean {
  return isOnSurfaceOf(parameter, id) && surfaceValue(parameter) === surfaceValue(registered(id));
}

/** True on the first swell partition: WAVEWATCH III writes its swell
 * partitions on code table 4.5 type 241, "ordered sequence of data", the
 * surface's value the partition's place in the sequence. Only the first is
 * published, and the second and third would be other fields. */
function isFirstSwell(parameter: BundleParameter): boolean {
  const swell = registered("swhgt");
  return parameter.typeOfFirstFixedSurface === swell.typeOfFirstFixedSurface && surfaceValue(parameter) === surfaceValue(swell);
}

/**
 * The identity of one scalar variable, or null when its parameters are not
 * ones this shell has chart knowledge for.
 *
 * Each rule names the registered variable whose parameter block it reads
 * (`registry.ts`): the triple and the surface type are the registry's, and
 * what a rule leaves out is what the chart does not tell apart. An
 * isobaric family takes its level off the parameter, on any surface; the
 * oceanographic discipline's surface value (0 from pgrb2, 1 from
 * WAVEWATCH III, none once declared) is not part of the identity, the
 * 2 m, 10 m and first-swell values are. A satellite channel is told apart
 * by the `band` block's central wave number (10–11 µm is the infrared
 * window, `ir104`, 3.5–4.2 µm the shortwave window, `ir039`); an aerosol
 * by its `aerosol` block (`aerosolField`); a soil layer by the surface's
 * value (`soilLayer`); a cloud water level's altitude is the volume's
 * business (volume.ts), not the identity's; the river discharge is
 * accepted on whatever surface the source writes it. The statistical
 * process (code table 4.10) is not part of the identity: a parameter
 * names one chart however the step was reduced. An `ensemble` block is
 * (`identityForEnsemble`): the quantity's identity carries the statistic.
 */
export function identityForParameter(
  parameter: BundleParameter,
  band?: BundleBand,
  aerosol?: BundleAerosol,
  ensemble?: BundleEnsemble,
): VariableIdentity | null {
  if (ensemble !== undefined) return identityForEnsemble(identityForParameter(parameter, band, aerosol), parameter, ensemble);
  const surface = parameter.typeOfFirstFixedSurface;
  const level = isobaricLevel(parameter);
  const value = surfaceValue(parameter);
  if (isOnSurfaceOf(parameter, "ir104")) {
    const channel = satelliteChannel(band);
    return channel === null ? null : scalar(channel, null);
  }
  // RRFS's column optical thickness: no aerosol block, on NCEP's single-layer
  // atmosphere where the GEFS-Aerosols fields are on the entire atmosphere.
  if (isOnSurfaceOf(parameter, "aotk")) return scalar("aotk", null);
  if (AEROSOL_FIELDS.some((id) => isOnSurfaceOf(parameter, id))) {
    const field = aerosolField(parameter, aerosol);
    return field === null ? null : scalar(field, null);
  }
  if (isQuantityOf(parameter, "hgt500") && level !== null) return scalar("hgt", level);
  // Orography: the geopotential height on the ground surface (NCEP), or the
  // geopotential itself (ECMWF open data), no level of its own.
  if (isOnSurfaceOf(parameter, "orog")) return scalar("orog", null);
  // The geopotential (0,3,4) is no registered variable's own block, only
  // an input spelling of the orography, so it stays written out here.
  if (isTriple(parameter, 0, 3, 4) && surface === registered("orog").typeOfFirstFixedSurface) return scalar("orog", null);
  if (isOnSurfaceOf(parameter, "prmsl")) return scalar("hgt", null);
  if (isQuantityOf(parameter, "tmp2m")) {
    if (level !== null) return scalar("tmp", level);
    if (isAtSurfaceOf(parameter, "tmp2m")) return scalar("tmp", null);
    if (isOnSurfaceOf(parameter, "tmpsfc")) return scalar("tmpsfc", null);
  }
  if (isOnSurfaceOf(parameter, "icec")) return scalar("icec", null);
  if (isOnSurfaceOf(parameter, "icetk")) return scalar("icetk", null);
  if (isOnSurfaceOf(parameter, "htsgw")) return scalar("htsgw", null);
  if (isOnSurfaceOf(parameter, "perpw")) return scalar("perpw", null);
  if (isOnSurfaceOf(parameter, "dirpw")) return scalar("dirpw", null);
  if (isOnSurfaceOf(parameter, "wvhgt")) return scalar("wvhgt", null);
  if (isOnSurfaceOf(parameter, "wvper")) return scalar("wvper", null);
  if (isOnSurfaceOf(parameter, "wvdir")) return scalar("wvdir", null);
  if (isFirstSwell(parameter)) {
    if (isQuantityOf(parameter, "swhgt")) return scalar("swhgt", null);
    if (isQuantityOf(parameter, "swper")) return scalar("swper", null);
    if (isQuantityOf(parameter, "swdir")) return scalar("swdir", null);
  }
  if (isQuantityOf(parameter, "thetae850") && level !== null) return scalar("thetae", level);
  if (isQuantityOf(parameter, "cat300") && level !== null) return scalar("cat", level);
  if (isAtSurfaceOf(parameter, "dpt2m")) return scalar("dpt2m", null);
  if (isAtSurfaceOf(parameter, "aptmp2m")) return scalar("aptmp2m", null);
  if (isQuantityOf(parameter, "vvel850") && level !== null) return scalar("vvel", level);
  if (isQuantityOf(parameter, "rh850") && level !== null) return scalar("rh", level);
  if (isQuantityOf(parameter, "spfh850") && level !== null) return scalar("spfh", level);
  if (isOnSurfaceOf(parameter, "prate")) return scalar("prate", null);
  if (isOnSurfaceOf(parameter, "dswrf")) return scalar("dswrf", null);
  if (isOnSurfaceOf(parameter, "cref")) return scalar("cref", null);
  if (isOnSurfaceOf(parameter, "gust")) return scalar("gust", null);
  if (isOnSurfaceOf(parameter, "tcdc")) return scalar("tcdc", null);
  if (isOnSurfaceOf(parameter, "lcdc")) return scalar("lcdc", null);
  if (isOnSurfaceOf(parameter, "mcdc")) return scalar("mcdc", null);
  if (isOnSurfaceOf(parameter, "hcdc")) return scalar("hcdc", null);
  if (isOnSurfaceOf(parameter, "cape")) return scalar("cape", null);
  if (isOnSurfaceOf(parameter, "cin")) return scalar("cin", null);
  if (isOnSurfaceOf(parameter, "vis")) return scalar("vis", null);
  if (isOnSurfaceOf(parameter, "pwat")) return scalar("pwat", null);
  if (isOnSurfaceOf(parameter, "hpbl")) return scalar("hpbl", null);
  // ECMWF's type on its longer book writes the surface with no value, where the
  // one NCEP's flags derive to declares 0.
  if (isAtSurfaceOf(parameter, "ptypeec")) return scalar("ptypeec", null);
  if (isOnSurfaceOf(parameter, "ptype")) return scalar("ptype", null);
  if (isOnSurfaceOf(parameter, "cpofp")) return scalar("cpofp", null);
  if (isOnSurfaceOf(parameter, "fzlvl")) return scalar("fzlvl", null);
  if (isOnSurfaceOf(parameter, "clw500")) return scalar("clw", null);
  if (isQuantityOf(parameter, "dis06")) return scalar("dis06", null);
  if (isOnSurfaceOf(parameter, "tsoil1")) {
    const layer = soilLayer(SOIL_TEMPERATURE_LAYERS, value);
    return layer === null ? null : scalar(layer, null);
  }
  if (isOnSurfaceOf(parameter, "soilw1")) {
    const layer = soilLayer(SOIL_MOISTURE_LAYERS, value);
    return layer === null ? null : scalar(layer, null);
  }
  return null;
}

/** The soil layers by the upper bound of the layer the registry writes
 * them on (surface 106, depth below land in metres): GFS's Noah layers
 * start at 0, 0.1, 0.4 and 1 m. The IFS's layers are cut elsewhere, but the
 * encoders write them on the same four values (docs/format.md), so the
 * value names the layer's index and never its depth. Millimetres, so the
 * comparison is exact whatever float a scale factor of 1 leaves behind. */
const SOIL_TEMPERATURE_LAYERS = ["tsoil1", "tsoil2", "tsoil3", "tsoil4"] as const satisfies readonly SoilChartFamily[];
const SOIL_MOISTURE_LAYERS = ["soilw1", "soilw2", "soilw3", "soilw4"] as const satisfies readonly SoilChartFamily[];
const SOIL_LAYER_TOPS_MM: readonly number[] = SOIL_TEMPERATURE_LAYERS.map((id) =>
  Math.round((surfaceValue(registered(id)) ?? Number.NaN) * 1000),
);

function soilLayer(layers: readonly SoilChartFamily[], depth: number | null): SoilChartFamily | null {
  if (depth === null || !Number.isFinite(depth)) return null;
  const index = SOIL_LAYER_TOPS_MM.indexOf(Math.round(depth * 1000));
  return index < 0 ? null : layers[index]!;
}

/** The satellite channel a brightness temperature belongs to, by the
 * central wave number of its `band` block: the chart is the same for AHI
 * band 13 (10.41 µm) and ABI channel 13 (10.35 µm), so a channel is a
 * wavelength interval, never a spacecraft or an exact number. A
 * brightness temperature with no band, or one in a band this build has no
 * chart for (the water vapour bands, until they ship), is an unknown
 * field and renders generically. */
const SATELLITE_CHANNELS: readonly { family: ChartFamily; wavelengthMicrons: readonly [number, number] }[] = [
  { family: "ir104", wavelengthMicrons: [10.0, 11.0] },
  // The shortwave window: AHI band 7 (3.89 µm), ABI channel 7 (3.90 µm),
  // FCI's IR3.8 (3.80 µm).
  { family: "ir039", wavelengthMicrons: [3.5, 4.2] },
];

function satelliteChannel(band: BundleBand | undefined): ChartFamily | null {
  if (band === undefined) return null;
  const wavenumber = band.scaledValueOfCentralWaveNumber * 10 ** -band.scaleFactorOfCentralWaveNumber;
  if (!(wavenumber > 0)) return null;
  const microns = 1e6 / wavenumber;
  for (const channel of SATELLITE_CHANNELS) {
    const [low, high] = channel.wavelengthMicrons;
    if (microns >= low && microns < high) return channel.family;
  }
  return null;
}

/** The registered fields that carry an `aerosol` block beside their
 * parameter: every optical depth is one WMO parameter on the entire
 * atmosphere and the two PM10 cuts one NCEP-local number on the ground,
 * so the block — species (code table 4.233), size and wavelength
 * intervals (code table 4.91) — is what tells them apart. */
const AEROSOL_FIELDS = (Object.keys(REGISTRY.variables) as RegistryVariableId[]).filter(
  (id) => "aerosol" in REGISTRY.variables[id],
);

/** The registered aerosol a parameter and its block name, or null: a
 * parameter with no block, or a block naming a species, size or
 * wavelength no registered field on that parameter has, is an unknown
 * field and renders generically. Everything in the block is compared. */
function aerosolField(parameter: BundleParameter, aerosol: BundleAerosol | undefined): ChartFamily | null {
  if (aerosol === undefined) return null;
  for (const id of AEROSOL_FIELDS) {
    if (!isOnSurfaceOf(parameter, id)) continue;
    const registeredBlock = (REGISTRY.variables[id] as { aerosol: BundleAerosol }).aerosol;
    const keys = Object.keys(registeredBlock) as (keyof BundleAerosol)[];
    if (keys.every((key) => aerosol[key] === registeredBlock[key])) return id as ChartFamily;
  }
  return null;
}

/** The u and v halves of each vector family, as parameter triples. The
 * wind and the vapour flux are on isobaric surfaces (the wind also at 10 m
 * and, as its own family, at 100 m); the wave vector — Xue-local numbers in
 * the oceanographic discipline's waves category, the height along the
 * direction of travel — sits on the water surface like the wave fields it
 * is derived from, its surface value no more part of the identity than
 * theirs; the wind sea's and the first swell's vectors are the same
 * construction on the next local numbers, the swell's on its partition's
 * surface. */
const VECTOR_PAIRS: readonly { family: ChartFamily; u: readonly [number, number, number]; v: readonly [number, number, number] }[] = [
  { family: "wind", u: tripleOf("ugrd10m"), v: tripleOf("vgrd10m") },
  { family: "qflux", u: tripleOf("uqflx850"), v: tripleOf("vqflx850") },
  { family: "wave", u: tripleOf("uwave"), v: tripleOf("vwave") },
  { family: "windwave", u: tripleOf("uwindwave"), v: tripleOf("vwindwave") },
  { family: "swell", u: tripleOf("uswell"), v: tripleOf("vswell") },
];

/**
 * The identity of a two-variable bundle whose variables are a component pair
 * on one surface, or null when they are not a pair this shell knows. Order
 * matters: `u` first, `v` second — the caller tries both orders.
 */
export function identityForParameterPair(u: BundleParameter, v: BundleParameter): VariableIdentity | null {
  if (u.typeOfFirstFixedSurface !== v.typeOfFirstFixedSurface) return null;
  if (surfaceValue(u) !== surfaceValue(v)) return null;
  for (const pair of VECTOR_PAIRS) {
    if (!isTriple(u, ...pair.u) || !isTriple(v, ...pair.v)) continue;
    if (pair.family === "wind" && u.typeOfFirstFixedSurface === 103) {
      // The same parameters on a height-above-ground surface: 10 m is the
      // surface wind, 100 m the turbine hub height, its own family so it
      // does not take over the wind family's isobaric level row.
      const value = surfaceValue(u);
      if (value === 10) return vector("wind", null);
      if (value === 80) return vector("wind80m", null);
      if (value === 100) return vector("wind100m", null);
      return null;
    }
    if (pair.family === "wave" || pair.family === "windwave") {
      return u.typeOfFirstFixedSurface === 1 ? vector(pair.family, null) : null;
    }
    if (pair.family === "swell") return isFirstSwell(u) ? vector("swell", null) : null;
    const level = isobaricLevel(u);
    return level === null ? null : vector(pair.family, level);
  }
  return null;
}

/** A registered variable's producer id; a produced field is named by it. */
function producerOf(id: RegistryVariableId): string {
  const producer = (REGISTRY.variables[id] as { producer?: { id: string } }).producer;
  if (producer === undefined) throw new Error(`${id} is registered without a producer`);
  return producer.id;
}

/** A composite's guns as the registry lists them, red, green, blue. */
function compositeOf(family: "dustrgb" | "truecolor") {
  const members = REGISTRY.bundles[family].members;
  const [discipline, category] = tripleOf(members[0]);
  return {
    family,
    producer: producerOf(members[0]),
    discipline,
    category,
    guns: [tripleOf(members[0])[2], tripleOf(members[1])[2], tripleOf(members[2])[2]] as const,
  };
}

/** A produced scalar's producer and parameter, the registry's. */
function producedScalarOf(family: "dustcf" | "zhouye" | "disrp" | "upstream") {
  const [discipline, category, number] = tripleOf(family);
  return { family, producer: producerOf(family), discipline, category, number };
}

/** A local-use scalar's parameter, the registry's. */
function localScalarOf(id: RegistryVariableId) {
  const [discipline, category, number] = tripleOf(id);
  return { discipline, category, number };
}

/** The composites by producer: a produced field's `parameter` is in
 * GRIB2's local-use range (category 192 and above, docs/format.md §"Band
 * and Producer"), where a number means nothing without the `producer.id`
 * beside it, so a composite is named by the producer and the parameter
 * numbers of its guns together. The Dust RGB is shachen's local numbers
 * 1, 2 and 3 in the space-products discipline, at the top of the
 * atmosphere like the channels it is made of; the true colour picture is
 * the encoder's own recipe, producer `xue`, at numbers 7, 8 and 9 — the
 * numbers are per producer, so shachen's 7 would be something else. The
 * same producer's 10, 11 and 12 are a night microphysics picture older
 * windows carried; this build has no chart for it, so such a bundle is
 * unknown. */
const COMPOSITE_TRIPLES: readonly {
  family: ChartFamily;
  producer: string;
  discipline: number;
  category: number;
  guns: readonly [number, number, number];
}[] = [
  compositeOf("dustrgb"),
  compositeOf("truecolor"),
];

/** Whether a parameter is in a local-use category, where only a producer
 * can say what it is. */
function isLocalUse(parameter: BundleParameter): boolean {
  return parameter.parameterCategory >= 192;
}

/** The produced scalars by producer: one local-use parameter that is a
 * field of its own rather than a gun, named by the producer and the
 * number together like the composites. The DEBRA dust confidence (Miller
 * et al. 2017) is shachen's local number 4 in the space-products
 * discipline, at the top of the atmosphere with the guns it is derived
 * beside; the ZHOUYE confidence, the same chain held to the day's level
 * through the night, is its number 6 (5 is the aurora's, under no
 * producer). The hydrology set's derived fields are the encoder's own
 * (producer `xue`) in the hydrological discipline's first local category:
 * the EFAS return-period class of the discharge, number 1, and the
 * upstream area each river cell drains, number 2 — numbers per
 * producer and per discipline, so they collide with nothing above. */
const PRODUCED_SCALARS: readonly {
  family: ChartFamily;
  producer: string;
  discipline: number;
  category: number;
  number: number;
}[] = [
  producedScalarOf("dustcf"),
  producedScalarOf("zhouye"),
  producedScalarOf("disrp"),
  producedScalarOf("upstream"),
];

/**
 * The identity of a one-variable bundle whose variable is a produced
 * scalar this shell knows, or null: a local-use parameter with no
 * `producer` block, or under a producer or number this build has no chart
 * for, is unknown. A parameter outside the local-use range is not this
 * function's to answer (`identityForParameter`), and the producer's
 * version is not part of the identity.
 */
export function identityForProducedScalar(variable: { parameter?: BundleParameter; producer?: BundleProducer }): VariableIdentity | null {
  const { parameter, producer } = variable;
  if (!parameter || !producer || !isLocalUse(parameter)) return null;
  for (const entry of PRODUCED_SCALARS) {
    if (entry.producer !== producer.id) continue;
    if (isTriple(parameter, entry.discipline, entry.category, entry.number)) return scalar(entry.family, null);
  }
  return null;
}

/** Local-use scalars carrying no `producer` block: the source's own
 * documentation names them, and no producer algorithm derived them. The
 * SWPC OVATION aurora probability is the first — a space-weather field the
 * encoder writes under a local-use number in the space-products discipline,
 * with neither a WMO parameter nor a producer to say what it is. */
const LOCAL_SCALARS: readonly {
  family: ChartFamily;
  discipline: number;
  category: number;
  number: number;
}[] = [{ family: "aurora", ...localScalarOf("aurora") }];

/** The identity of a local-use scalar this shell knows without a producer
 * block, or null. A local-use parameter under a number this build has no
 * chart for is unknown. */
function identityForLocalScalar(parameter: BundleParameter): VariableIdentity | null {
  if (!isLocalUse(parameter)) return null;
  for (const entry of LOCAL_SCALARS) {
    if (isTriple(parameter, entry.discipline, entry.category, entry.number)) return scalar(entry.family, null);
  }
  return null;
}

/**
 * The identity of a three-variable bundle whose variables are the guns of
 * a composite this shell knows, in red, green, blue order, or null. Each
 * gun must carry the same producer id and sit on the same surface; the
 * producer's version is not part of the identity.
 */
export function identityForProducedTriple(
  variables: readonly { parameter?: BundleParameter; producer?: BundleProducer }[],
): VariableIdentity | null {
  if (variables.length !== 3) return null;
  const guns: { parameter: BundleParameter; producer: BundleProducer }[] = [];
  for (const variable of variables) {
    if (!variable.parameter || !variable.producer) return null;
    guns.push({ parameter: variable.parameter, producer: variable.producer });
  }
  const [r, g, b] = guns as [(typeof guns)[number], (typeof guns)[number], (typeof guns)[number]];
  const producer = r.producer.id;
  for (const gun of [g, b]) {
    if (gun.producer.id !== producer) return null;
    if (gun.parameter.typeOfFirstFixedSurface !== r.parameter.typeOfFirstFixedSurface) return null;
    if (surfaceValue(gun.parameter) !== surfaceValue(r.parameter)) return null;
  }
  for (const triple of COMPOSITE_TRIPLES) {
    if (triple.producer !== producer) continue;
    const matches = [r, g, b].every(
      (gun, at) =>
        isLocalUse(gun.parameter) && isTriple(gun.parameter, triple.discipline, triple.category, triple.guns[at]!),
    );
    if (matches) return scalar(triple.family, null);
  }
  return null;
}

/**
 * The ids a schemaVersion 1 or 2 file can carry, and what they are. Those
 * files predate the parameter block and are never rebuilt, so this is a
 * closed list of exactly what was published before v3 — not a registry to
 * grow. Anything else in such a file is an unknown variable.
 */
const LEGACY_IDENTITIES: Record<string, VariableIdentity> = {
  tmp2m: scalar("tmp", null),
  prate: scalar("prate", null),
  dswrf: scalar("dswrf", null),
  cref: scalar("cref", null),
};

/** The u/v pair a legacy file spells out: only the 10 m wind ever shipped
 * as a two-variable bundle before schemaVersion 3. */
const LEGACY_WIND_COMPONENTS: readonly [string, string] = ["ugrd10m", "vgrd10m"];

/** One bundle's identity plus its variables in the order the renderer wants
 * them (u then v for a vector field, red, green, blue for a composite, the
 * single variable otherwise). */
export interface BundleIdentity {
  identity: VariableIdentity;
  variables: BundleVariable[];
}

/**
 * What a bundle is, from the variables its own metadata declares: the
 * parameter blocks where the file has them (schemaVersion 3), the legacy id
 * list where it does not. Null when nothing in the file is recognizable —
 * the caller then renders the first variable as an unlabelled scalar.
 */
export function identifyBundle(variables: readonly BundleVariable[]): BundleIdentity | null {
  if (variables.length === 0) return null;
  if (variables.length === 3) {
    // A composite is its three guns in the file's own order: the encoders
    // number them red, green, blue, and a file that did not could not be
    // drawn as a picture.
    const composite = identityForProducedTriple(variables);
    if (composite) return { identity: composite, variables: [...variables] };
  }
  if (variables.length >= 2) {
    const [a, b] = variables as [BundleVariable, BundleVariable];
    if (a.parameter && b.parameter) {
      const forward = identityForParameterPair(a.parameter, b.parameter);
      if (forward) return { identity: forward, variables: [a, b] };
      const reversed = identityForParameterPair(b.parameter, a.parameter);
      if (reversed) return { identity: reversed, variables: [b, a] };
    } else if (
      a.parameter === undefined &&
      b.parameter === undefined &&
      ((a.id === LEGACY_WIND_COMPONENTS[0] && b.id === LEGACY_WIND_COMPONENTS[1]) ||
        (a.id === LEGACY_WIND_COMPONENTS[1] && b.id === LEGACY_WIND_COMPONENTS[0]))
    ) {
      const u = a.id === LEGACY_WIND_COMPONENTS[0] ? a : b;
      const v = u === a ? b : a;
      return { identity: vector("wind", null), variables: [u, v] };
    }
  }
  // Not a pair: the bundle reads as whatever its first variable is. A
  // local-use parameter is the producer's to name (a produced scalar); any
  // other is the WMO table's.
  const first = variables[0]!;
  const identity = first.parameter
    ? isLocalUse(first.parameter)
      ? (identityForProducedScalar(first) ?? identityForLocalScalar(first.parameter))
      : identityForParameter(first.parameter, first.band, first.aerosol, first.ensemble)
    : (LEGACY_IDENTITIES[first.id] ?? null);
  return identity === null ? null : { identity, variables: [first] };
}

/**
 * What a bundle id *looks* like it is, by the naming convention alone. This
 * is all the shell has before a bundle is open — the rail tiles, the level
 * row, `?type=` resolution and the legend prepared from the manifest — and
 * it is a guess: once the session is ready, the parameter block wins and
 * main.ts warns if the two disagree. A registered id answers from the
 * variable table; `<family><level>` on a level nothing is registered on
 * still reads as that family, by the convention. Null for a name the
 * convention does not describe.
 */
export function identityForBundleId(id: string): VariableIdentity | null {
  const spec = variableSpec(id);
  if (spec) {
    const identity: VariableIdentity = { family: spec.chart, level: spec.level, vector: spec.vector };
    if (spec.statistic !== null) identity.statistic = spec.statistic;
    return identity;
  }
  const match = /^([a-z]+)(\d+)$/.exec(id);
  if (!match) return null;
  const family = match[1] as ChartFamily;
  const isVector = isobaricChartFamily(family);
  if (isVector === undefined) return null;
  return isVector ? vector(family, Number(match[2])) : scalar(family, Number(match[2]));
}

/**
 * The registered bundle id for an identity — the name the shell's chart
 * registries (`levels.ts`, `pressure.ts`, the palette tables) are written
 * under — or null when the identity names a surface no registry covers.
 * That null is what makes a field "unknown" for rendering purposes, whether
 * because its parameters are unrecognized or because it sits on a level
 * nothing is registered on.
 */
export function registeredBundleId(identity: VariableIdentity | null): KnownBundleId | null {
  return specForIdentity(identity)?.id ?? null;
}
