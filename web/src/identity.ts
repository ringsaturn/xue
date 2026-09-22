import type { BundleAerosol, BundleBand, BundleParameter, BundleProducer, BundleVariable, KnownBundleId } from "./manifest";
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

/** The chart families. The first eight are registered on isobaric surfaces
 * and some of them also have a near-surface member; the rest are single
 * layers (precipitation, radiation, reflectivity, the surface diagnostics:
 * gust, the four cloud covers, CAPE, visibility, dew point, apparent
 * temperature — and the ocean set: skin temperature, sea ice cover and
 * thickness, wave height, period and direction, and the wave vector, the
 * height laid along the direction of travel as a u/v pair — and the
 * satellite channels, one family per nominal wavelength: the brightness
 * temperature parameter is the same for every infrared band, and the
 * `band` block beside it says which — the satellite composites, a
 * three-gun colour picture a producer derived from several channels, told
 * apart by the `producer` block and the local-use parameter numbers — and
 * the produced scalars, one field a producer derived from several channels
 * (the DEBRA dust confidence), told apart the same way — and the aerosol
 * set: the optical depth at 550 nm, one family for the whole column and one
 * per species, and the surface particulate matter by size cut, told apart
 * by the `aerosol` block beside a parameter that is the same for every
 * species). */
export type ChartFamily =
  | "hgt"
  | "tmp"
  | "rh"
  | "spfh"
  | "wind"
  | "wind100m"
  | "qflux"
  | "vvel"
  | "thetae"
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
  | "tmpsfc"
  | "icec"
  | "icetk"
  | "htsgw"
  | "perpw"
  | "dirpw"
  | "wave"
  | "ir104"
  | "dustrgb"
  | "dustcf"
  | "aod"
  | "aoddust"
  | "aodsalt"
  | "aodsulf"
  | "aodorg"
  | "aodbc"
  | "pm25"
  | "pm10"
  | "pm10dust";

export interface VariableIdentity {
  family: ChartFamily;
  /** Isobaric surface in hPa, or null for a surface member (2 m temperature,
   * 10 m wind, mean sea level pressure) and for a single layer. */
  level: number | null;
  /** True when the bundle carries a u/v component pair rather than a scalar. */
  vector: boolean;
}

function scalar(family: ChartFamily, level: number | null): VariableIdentity {
  return { family, level, vector: false };
}

function vector(family: ChartFamily, level: number | null): VariableIdentity {
  return { family, level, vector: true };
}

/** The composites: a family whose bundle is three colour guns rather than
 * one scalar or a u/v pair, drawn straight as red, green and blue. An
 * identity's `vector` flag is false for them; this is what says the third
 * texture mode is theirs. */
const COMPOSITE_FAMILIES: readonly ChartFamily[] = ["dustrgb"];

/** True when an identity names a colour composite (three guns). */
export function isCompositeIdentity(identity: VariableIdentity | null): boolean {
  return identity !== null && !identity.vector && COMPOSITE_FAMILIES.includes(identity.family);
}

export function sameIdentity(a: VariableIdentity | null, b: VariableIdentity | null): boolean {
  if (a === null || b === null) return a === b;
  return a.family === b.family && a.level === b.level && a.vector === b.vector;
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

/**
 * The identity of one scalar variable, or null when its parameters are not
 * ones this shell has chart knowledge for.
 *
 * The table (discipline / category / number @ surface type):
 * (0,3,5) @100 geopotential height, (0,3,1) @101 sea level pressure,
 * (0,0,0) @100 and @103 value 2 temperature, (0,1,1) @100 relative humidity,
 * (0,1,0) @100 specific humidity, (0,1,7) @1 precipitation rate,
 * (0,4,192) @1 downward shortwave radiation, (0,16,5) @10 composite
 * reflectivity, (0,2,22) @1 wind gust, (0,6,1) @10 total cloud cover,
 * (0,6,3) @214 / (0,6,4) @224 / (0,6,5) @234 low / middle / high cloud
 * cover, (0,7,6) @1 surface-based CAPE and (0,7,7) @1 convective
 * inhibition, (0,1,3) @200 precipitable water (NCEP's local "entire
 * atmosphere (considered as a single layer)" surface, which carries no
 * value), (0,3,196) @1 planetary boundary layer height, (0,1,19) @1
 * precipitation type, (0,19,0) @1 visibility, (0,0,6)
 * @103 value 2 dew point, (0,0,21) @103 value 2 apparent temperature,
 * (0,2,8) @100 vertical velocity, (0,0,3) @100 equivalent potential
 * temperature, (0,0,0) @1 surface (skin) temperature, (10,2,0) @1 sea ice
 * cover, (10,2,1) @1 sea ice thickness, (10,0,3) / (10,0,11) / (10,0,10)
 * @1 significant wave height, primary wave period and direction — the
 * oceanographic discipline's surface, whose value (0 from pgrb2, 1 from
 * WAVEWATCH III, none once declared) is not part of the identity; (0,4,4)
 * @8 brightness temperature, a satellite channel told apart by the
 * `band` block's central wave number (10–11 µm is the infrared window,
 * `ir104`); (0,20,102) @10 aerosol optical thickness and (0,13,193) /
 * (0,13,192) @1 fine / coarse particulate matter (NCEP-local numbers), each
 * told apart by the `aerosol` block's species, size and wavelength
 * (`aerosolField`).
 */
export function identityForParameter(parameter: BundleParameter, band?: BundleBand, aerosol?: BundleAerosol): VariableIdentity | null {
  const surface = parameter.typeOfFirstFixedSurface;
  const level = isobaricLevel(parameter);
  const value = surfaceValue(parameter);
  if (isTriple(parameter, 0, 4, 4) && surface === 8) {
    const channel = satelliteChannel(band);
    return channel === null ? null : scalar(channel, null);
  }
  if (isTriple(parameter, 0, 20, 102) && surface === 10) {
    const field = aerosolField(AEROSOL_OPTICAL_DEPTHS, aerosol);
    return field === null ? null : scalar(field, null);
  }
  if ((isTriple(parameter, 0, 13, 193) || isTriple(parameter, 0, 13, 192)) && surface === 1) {
    const field = aerosolField(parameter.parameterNumber === 193 ? FINE_PARTICULATES : COARSE_PARTICULATES, aerosol);
    return field === null ? null : scalar(field, null);
  }
  if (isTriple(parameter, 0, 3, 5) && level !== null) return scalar("hgt", level);
  // Orography: the geopotential height on the ground surface (NCEP), or the
  // geopotential itself (ECMWF open data), no level of its own.
  if (isTriple(parameter, 0, 3, 5) && surface === 1) return scalar("orog", null);
  if (isTriple(parameter, 0, 3, 4) && surface === 1) return scalar("orog", null);
  if (isTriple(parameter, 0, 3, 1) && surface === 101) return scalar("hgt", null);
  if (isTriple(parameter, 0, 0, 0)) {
    if (level !== null) return scalar("tmp", level);
    if (surface === 103 && value === 2) return scalar("tmp", null);
    if (surface === 1) return scalar("tmpsfc", null);
  }
  if (isTriple(parameter, 10, 2, 0) && surface === 1) return scalar("icec", null);
  if (isTriple(parameter, 10, 2, 1) && surface === 1) return scalar("icetk", null);
  if (isTriple(parameter, 10, 0, 3) && surface === 1) return scalar("htsgw", null);
  if (isTriple(parameter, 10, 0, 11) && surface === 1) return scalar("perpw", null);
  if (isTriple(parameter, 10, 0, 10) && surface === 1) return scalar("dirpw", null);
  if (isTriple(parameter, 0, 0, 3) && level !== null) return scalar("thetae", level);
  if (isTriple(parameter, 0, 0, 6) && surface === 103 && value === 2) return scalar("dpt2m", null);
  if (isTriple(parameter, 0, 0, 21) && surface === 103 && value === 2) return scalar("aptmp2m", null);
  if (isTriple(parameter, 0, 2, 8) && level !== null) return scalar("vvel", level);
  if (isTriple(parameter, 0, 1, 1) && level !== null) return scalar("rh", level);
  if (isTriple(parameter, 0, 1, 0) && level !== null) return scalar("spfh", level);
  if (isTriple(parameter, 0, 1, 7) && surface === 1) return scalar("prate", null);
  if (isTriple(parameter, 0, 4, 192) && surface === 1) return scalar("dswrf", null);
  if (isTriple(parameter, 0, 16, 5) && surface === 10) return scalar("cref", null);
  if (isTriple(parameter, 0, 2, 22) && surface === 1) return scalar("gust", null);
  if (isTriple(parameter, 0, 6, 1) && surface === 10) return scalar("tcdc", null);
  if (isTriple(parameter, 0, 6, 3) && surface === 214) return scalar("lcdc", null);
  if (isTriple(parameter, 0, 6, 4) && surface === 224) return scalar("mcdc", null);
  if (isTriple(parameter, 0, 6, 5) && surface === 234) return scalar("hcdc", null);
  if (isTriple(parameter, 0, 7, 6) && surface === 1) return scalar("cape", null);
  if (isTriple(parameter, 0, 7, 7) && surface === 1) return scalar("cin", null);
  if (isTriple(parameter, 0, 19, 0) && surface === 1) return scalar("vis", null);
  if (isTriple(parameter, 0, 1, 3) && surface === 200) return scalar("pwat", null);
  if (isTriple(parameter, 0, 3, 196) && surface === 1) return scalar("hpbl", null);
  if (isTriple(parameter, 0, 1, 19) && surface === 1) return scalar("ptype", null);
  return null;
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

/** One aerosol field's `aerosol` block, as the chart tells it apart: the
 * species (GRIB2 code table 4.233: 62000 the total, 62001 dust, 62006
 * sulphate, 62008 sea salt, 62009 black carbon, 62010 organic matter), the
 * size interval and, for an optical depth, the wavelength interval. Each
 * interval is its code table 4.91 type and its two limits in metres as
 * `[scaleFactor, scaledValue]`, or null for the limits of an interval the
 * record does not carry (type 255). Everything in the block is compared:
 * an optical depth at another wavelength, or a size cut this build has no
 * chart for, is an unknown field and renders generically. */
interface AerosolField {
  family: ChartFamily;
  aerosolType: number;
  size: AerosolInterval;
  wavelength: AerosolInterval;
}

interface AerosolInterval {
  type: number;
  first: readonly [number, number] | null;
  second: readonly [number, number] | null;
}

/** Code table 4.91's "missing", with no limits. */
const NO_INTERVAL: AerosolInterval = { type: 255, first: null, second: null };
/** "Smaller than the first limit": particles under 20 µm, 2.5 µm, 10 µm. */
const UNDER_20_MICRONS: AerosolInterval = { type: 0, first: [6, 20], second: [0, 0] };
const UNDER_2P5_MICRONS: AerosolInterval = { type: 0, first: [7, 25], second: [0, 0] };
const UNDER_10_MICRONS: AerosolInterval = { type: 0, first: [6, 10], second: [0, 0] };
/** "Between the two limits": the 545–565 nm band the 550 nm depth is
 * reported over. */
const AT_550_NANOMETRES: AerosolInterval = { type: 7, first: [9, 545], second: [9, 565] };

/** The optical depths: (0,20,102) on the entire atmosphere, the whole
 * aerosol column under 20 µm at 550 nm, one family per species. */
const AEROSOL_OPTICAL_DEPTHS: readonly AerosolField[] = [
  { family: "aod", aerosolType: 62000, size: UNDER_20_MICRONS, wavelength: AT_550_NANOMETRES },
  { family: "aoddust", aerosolType: 62001, size: UNDER_20_MICRONS, wavelength: AT_550_NANOMETRES },
  { family: "aodsalt", aerosolType: 62008, size: UNDER_20_MICRONS, wavelength: AT_550_NANOMETRES },
  { family: "aodsulf", aerosolType: 62006, size: UNDER_20_MICRONS, wavelength: AT_550_NANOMETRES },
  { family: "aodorg", aerosolType: 62010, size: UNDER_20_MICRONS, wavelength: AT_550_NANOMETRES },
  { family: "aodbc", aerosolType: 62009, size: UNDER_20_MICRONS, wavelength: AT_550_NANOMETRES },
];
/** The fine particulates, (0,13,193) on the ground: the total under 2.5 µm. */
const FINE_PARTICULATES: readonly AerosolField[] = [
  { family: "pm25", aerosolType: 62000, size: UNDER_2P5_MICRONS, wavelength: NO_INTERVAL },
];
/** The coarse particulates, (0,13,192) on the ground: the total and the
 * dust under 10 µm. */
const COARSE_PARTICULATES: readonly AerosolField[] = [
  { family: "pm10", aerosolType: 62000, size: UNDER_10_MICRONS, wavelength: NO_INTERVAL },
  { family: "pm10dust", aerosolType: 62001, size: UNDER_10_MICRONS, wavelength: NO_INTERVAL },
];

function sameLimit(a: readonly [number, number] | null, scale: number | null, value: number | null): boolean {
  return a === null ? scale === null && value === null : a[0] === scale && a[1] === value;
}

function sameInterval(
  interval: AerosolInterval,
  type: number,
  first: readonly [number | null, number | null],
  second: readonly [number | null, number | null],
): boolean {
  return interval.type === type && sameLimit(interval.first, ...first) && sameLimit(interval.second, ...second);
}

/** The aerosol field a block names among `fields`, or null: a parameter
 * with no block, or a block naming a species, size or wavelength none of
 * them has, is an unknown field. */
function aerosolField(fields: readonly AerosolField[], aerosol: BundleAerosol | undefined): ChartFamily | null {
  if (aerosol === undefined) return null;
  for (const field of fields) {
    if (field.aerosolType !== aerosol.aerosolType) continue;
    if (
      !sameInterval(
        field.size,
        aerosol.typeOfSizeInterval,
        [aerosol.scaleFactorOfFirstSize, aerosol.scaledValueOfFirstSize],
        [aerosol.scaleFactorOfSecondSize, aerosol.scaledValueOfSecondSize],
      )
    ) {
      continue;
    }
    if (
      !sameInterval(
        field.wavelength,
        aerosol.typeOfWavelengthInterval,
        [aerosol.scaleFactorOfFirstWavelength, aerosol.scaledValueOfFirstWavelength],
        [aerosol.scaleFactorOfSecondWavelength, aerosol.scaledValueOfSecondWavelength],
      )
    ) {
      continue;
    }
    return field.family;
  }
  return null;
}

/** The u and v halves of each vector family, as parameter triples. The
 * wind and the vapour flux are on isobaric surfaces (the wind also at 10 m
 * and, as its own family, at 100 m); the wave vector — Xue-local numbers in
 * the oceanographic discipline's waves category, the height along the
 * direction of travel — sits on the water surface like the wave fields it
 * is derived from, its surface value no more part of the identity than
 * theirs. */
const VECTOR_PAIRS: readonly { family: ChartFamily; u: readonly [number, number, number]; v: readonly [number, number, number] }[] = [
  { family: "wind", u: [0, 2, 2], v: [0, 2, 3] },
  { family: "qflux", u: [0, 1, 250], v: [0, 1, 251] },
  { family: "wave", u: [10, 0, 250], v: [10, 0, 251] },
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
      if (value === 100) return vector("wind100m", null);
      return null;
    }
    if (pair.family === "wave") return u.typeOfFirstFixedSurface === 1 ? vector("wave", null) : null;
    const level = isobaricLevel(u);
    return level === null ? null : vector(pair.family, level);
  }
  return null;
}

/** The composites by producer: a produced field's `parameter` is in
 * GRIB2's local-use range (category 192 and above, docs/format.md §"Band
 * and Producer"), where a number means nothing without the `producer.id`
 * beside it, so a composite is named by the producer and the parameter
 * numbers of its guns together. The Dust RGB is shachen's local numbers
 * 1, 2 and 3 in the space-products discipline, at the top of the
 * atmosphere like the channels it is made of. */
const COMPOSITE_TRIPLES: readonly {
  family: ChartFamily;
  producer: string;
  discipline: number;
  category: number;
  guns: readonly [number, number, number];
}[] = [{ family: "dustrgb", producer: "shachen", discipline: 3, category: 192, guns: [1, 2, 3] }];

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
 * beside. */
const PRODUCED_SCALARS: readonly {
  family: ChartFamily;
  producer: string;
  discipline: number;
  category: number;
  number: number;
}[] = [{ family: "dustcf", producer: "shachen", discipline: 3, category: 192, number: 4 }];

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
      ? identityForProducedScalar(first)
      : identityForParameter(first.parameter, first.band, first.aerosol)
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
  if (spec) return { family: spec.chart, level: spec.level, vector: spec.vector };
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
