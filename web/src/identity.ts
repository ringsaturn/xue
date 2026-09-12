import { ISOBARIC_LEVELS, type BundleParameter, type BundleVariable, type KnownBundleId } from "./manifest";

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
 * thickness, wave height, period and direction). */
export type ChartFamily =
  | "hgt"
  | "tmp"
  | "rh"
  | "spfh"
  | "wind"
  | "qflux"
  | "vvel"
  | "thetae"
  | "prate"
  | "dswrf"
  | "cref"
  | "gust"
  | "tcdc"
  | "lcdc"
  | "mcdc"
  | "hcdc"
  | "cape"
  | "vis"
  | "dpt2m"
  | "aptmp2m"
  | "tmpsfc"
  | "icec"
  | "icetk"
  | "htsgw"
  | "perpw"
  | "dirpw";

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
 * cover, (0,7,6) @1 surface-based CAPE, (0,19,0) @1 visibility, (0,0,6)
 * @103 value 2 dew point, (0,0,21) @103 value 2 apparent temperature,
 * (0,2,8) @100 vertical velocity, (0,0,3) @100 equivalent potential
 * temperature, (0,0,0) @1 surface (skin) temperature, (10,2,0) @1 sea ice
 * cover, (10,2,1) @1 sea ice thickness, (10,0,3) / (10,0,11) / (10,0,10)
 * @1 significant wave height, primary wave period and direction — the
 * oceanographic discipline's surface, whose value (0 from pgrb2, 1 from
 * WAVEWATCH III, none once declared) is not part of the identity.
 */
export function identityForParameter(parameter: BundleParameter): VariableIdentity | null {
  const surface = parameter.typeOfFirstFixedSurface;
  const level = isobaricLevel(parameter);
  const value = surfaceValue(parameter);
  if (isTriple(parameter, 0, 3, 5) && level !== null) return scalar("hgt", level);
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
  if (isTriple(parameter, 0, 19, 0) && surface === 1) return scalar("vis", null);
  return null;
}

/** The u and v halves of each vector family, as parameter triples. */
const VECTOR_PAIRS: readonly { family: ChartFamily; u: readonly [number, number, number]; v: readonly [number, number, number] }[] = [
  { family: "wind", u: [0, 2, 2], v: [0, 2, 3] },
  { family: "qflux", u: [0, 1, 250], v: [0, 1, 251] },
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
    if (pair.family === "wind" && u.typeOfFirstFixedSurface === 103 && surfaceValue(u) === 10) {
      return vector("wind", null);
    }
    const level = isobaricLevel(u);
    return level === null ? null : vector(pair.family, level);
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
 * them (u then v for a vector field, the single variable otherwise). */
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
  // Not a pair: the bundle reads as whatever its first variable is.
  const first = variables[0]!;
  const identity = first.parameter
    ? identityForParameter(first.parameter)
    : (LEGACY_IDENTITIES[first.id] ?? null);
  return identity === null ? null : { identity, variables: [first] };
}

/** The surface member of each family, by the naming convention. */
const SURFACE_IDS: Record<string, VariableIdentity> = {
  tmp2m: scalar("tmp", null),
  wind10m: vector("wind", null),
  prmsl: scalar("hgt", null),
  prate: scalar("prate", null),
  dswrf: scalar("dswrf", null),
  cref: scalar("cref", null),
  gust: scalar("gust", null),
  tcdc: scalar("tcdc", null),
  lcdc: scalar("lcdc", null),
  mcdc: scalar("mcdc", null),
  hcdc: scalar("hcdc", null),
  cape: scalar("cape", null),
  vis: scalar("vis", null),
  dpt2m: scalar("dpt2m", null),
  aptmp2m: scalar("aptmp2m", null),
  tmpsfc: scalar("tmpsfc", null),
  icec: scalar("icec", null),
  icetk: scalar("icetk", null),
  htsgw: scalar("htsgw", null),
  perpw: scalar("perpw", null),
  dirpw: scalar("dirpw", null),
};

const ISOBARIC_PREFIXES: Record<string, ChartFamily> = {
  hgt: "hgt",
  tmp: "tmp",
  rh: "rh",
  spfh: "spfh",
  wind: "wind",
  qflux: "qflux",
  vvel: "vvel",
  thetae: "thetae",
};

/**
 * What a bundle id *looks* like it is, by the naming convention alone. This
 * is all the shell has before a bundle is open — the rail tiles, the level
 * row, `?type=` resolution and the legend prepared from the manifest — and
 * it is a guess: once the session is ready, the parameter block wins and
 * main.ts warns if the two disagree. Null for a name the convention does not
 * describe.
 */
export function identityForBundleId(id: string): VariableIdentity | null {
  const surface = SURFACE_IDS[id];
  if (surface) return surface;
  const match = /^([a-z]+)(\d+)$/.exec(id);
  if (!match) return null;
  const family = ISOBARIC_PREFIXES[match[1]!];
  if (family === undefined) return null;
  return family === "wind" || family === "qflux"
    ? vector(family, Number(match[2]))
    : scalar(family, Number(match[2]));
}

/** The surface member's id for each family, or null where the family has
 * none (relative and specific humidity, the vapour flux). */
const FAMILY_SURFACE_ID: Record<ChartFamily, KnownBundleId | null> = {
  hgt: "prmsl",
  tmp: "tmp2m",
  rh: null,
  spfh: null,
  wind: "wind10m",
  qflux: null,
  prate: "prate",
  dswrf: "dswrf",
  cref: "cref",
  vvel: null,
  thetae: null,
  gust: "gust",
  tcdc: "tcdc",
  lcdc: "lcdc",
  mcdc: "mcdc",
  hcdc: "hcdc",
  cape: "cape",
  vis: "vis",
  dpt2m: "dpt2m",
  aptmp2m: "aptmp2m",
  tmpsfc: "tmpsfc",
  icec: "icec",
  icetk: "icetk",
  htsgw: "htsgw",
  perpw: "perpw",
  dirpw: "dirpw",
};

/**
 * The registered bundle id for an identity — the name the shell's chart
 * registries (`levels.ts`, `pressure.ts`, the palette tables) are written
 * under — or null when the identity names a surface no registry covers.
 * That null is what makes a field "unknown" for rendering purposes, whether
 * because its parameters are unrecognized or because it sits on a level
 * nothing is registered on.
 */
export function registeredBundleId(identity: VariableIdentity | null): KnownBundleId | null {
  if (identity === null) return null;
  if (identity.level === null) return FAMILY_SURFACE_ID[identity.family];
  if (!(ISOBARIC_LEVELS as readonly number[]).includes(identity.level)) return null;
  if (identity.family in ISOBARIC_PREFIXES) return `${identity.family}${identity.level}` as KnownBundleId;
  return null;
}
