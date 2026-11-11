/**
 * WGS84 latitude/longitude ↔ UTM / UPS ↔ MGRS, the grid the GSI's UTM grid
 * and military maps number their squares in. Pure arithmetic, no DOM.
 *
 * UTM is the transverse Mercator in Krüger's series to the fourth order in
 * the third flattening, which holds to well under a millimetre across a
 * zone. UPS is the polar stereographic with k0 = 0.994. The 100 km square
 * letters follow the AA scheme WGS84 uses, and the UPS letters NGA's tables
 * (GeoTrans' MGRS module): the A/B and Y/Z column sets skip D, E, M, N, V,
 * W besides I and O.
 */

const A = 6378137;
const F = 1 / 298.257223563;
const E2 = F * (2 - F);
const E = Math.sqrt(E2);
const N_ = F / (2 - F);
const UTM_K0 = 0.9996;
const UPS_K0 = 0.994;
const UTM_FALSE_EASTING = 500_000;
const UTM_SOUTH_FALSE_NORTHING = 10_000_000;
const UPS_FALSE = 2_000_000;

const n2 = N_ * N_;
const n3 = n2 * N_;
const n4 = n3 * N_;
/** The rectifying radius. */
const RECT = (A / (1 + N_)) * (1 + n2 / 4 + n4 / 64);
const ALPHA = [
  N_ / 2 - (2 / 3) * n2 + (5 / 16) * n3 + (41 / 180) * n4,
  (13 / 48) * n2 - (3 / 5) * n3 + (557 / 1440) * n4,
  (61 / 240) * n3 - (103 / 140) * n4,
  (49561 / 161280) * n4,
];
const BETA = [
  N_ / 2 - (2 / 3) * n2 + (37 / 96) * n3 - (1 / 360) * n4,
  (1 / 48) * n2 + (1 / 15) * n3 - (437 / 1440) * n4,
  (17 / 480) * n3 - (37 / 840) * n4,
  (4397 / 161280) * n4,
];
const DELTA = [
  2 * N_ - (2 / 3) * n2 - 2 * n3 + (116 / 45) * n4,
  (7 / 3) * n2 - (8 / 5) * n3 - (227 / 45) * n4,
  (56 / 15) * n3 - (136 / 35) * n4,
  (4279 / 630) * n4,
];
const TWO_ROOT_N = (2 * Math.sqrt(N_)) / (1 + N_);
/** ρ = UPS_RHO · t, the polar stereographic's radius from the isometric t. */
const UPS_RHO = (2 * A * UPS_K0) / Math.sqrt((1 + E) ** (1 + E) * (1 - E) ** (1 - E));

const DEG = Math.PI / 180;

export const MGRS_BANDS = "CDEFGHJKLMNPQRSTUVWX";
/** UTM covers 80°S up to (not including) 84°N; UPS the two caps beyond. */
export const UTM_SOUTH_LIMIT = -80;
export const UTM_NORTH_LIMIT = 84;

const UTM_COLUMNS = ["ABCDEFGH", "JKLMNPQR", "STUVWXYZ"];
const UTM_ROWS = "ABCDEFGHJKLMNPQRSTUV";
const UPS_WEST_COLUMNS = "JKLPQRSTUXYZ";
const UPS_EAST_COLUMNS = "ABCFGHJKLPQR";
const UPS_ROWS = "ABCDEFGHJKLMNPQRSTUVWXYZ";

export interface LatLon {
  lat: number;
  lon: number;
}

export interface Utm {
  zone: number;
  north: boolean;
  easting: number;
  northing: number;
}

export interface Ups {
  north: boolean;
  easting: number;
  northing: number;
}

/** A longitude in [-180, 180). */
export function wrapLongitude(lon: number): number {
  const wrapped = ((((lon + 180) % 360) + 360) % 360) - 180;
  return wrapped === 180 ? -180 : wrapped;
}

/** The UTM zone a point is numbered in, with the Norway (32V) and Svalbard
 * (31X, 33X, 35X, 37X) widenings. */
export function utmZone(lat: number, lon: number): number {
  const x = wrapLongitude(lon);
  let zone = Math.min(60, Math.floor((x + 180) / 6) + 1);
  if (lat >= 56 && lat < 64 && x >= 3 && x < 12) zone = 32;
  if (lat >= 72 && x >= 0 && x < 42) zone = x < 9 ? 31 : x < 21 ? 33 : x < 33 ? 35 : 37;
  return zone;
}

/** The zone's central meridian in degrees. */
export function centralMeridian(zone: number): number {
  return (zone - 1) * 6 - 180 + 3;
}

/** The latitude band letter, C–X (X is 12° tall); `null` in the polar caps. */
export function latitudeBand(lat: number): string | null {
  if (lat < UTM_SOUTH_LIMIT || lat >= UTM_NORTH_LIMIT) return null;
  return MGRS_BANDS.charAt(Math.min(19, Math.floor((lat + 80) / 8)));
}

/** Project into UTM: in the point's own zone, or in `zone` when given (a
 * line drawn across a zone's whole cell is computed in that one zone). The
 * hemisphere picks the false northing unless `north` forces it. */
export function toUtm(lat: number, lon: number, zone = utmZone(lat, lon), north = lat >= 0): Utm {
  const phi = lat * DEG;
  let dLon = wrapLongitude(lon - centralMeridian(zone)) * DEG;
  if (dLon < -Math.PI) dLon += 2 * Math.PI;
  const sinPhi = Math.sin(phi);
  const t = Math.sinh(Math.atanh(sinPhi) - TWO_ROOT_N * Math.atanh(TWO_ROOT_N * sinPhi));
  const xiP = Math.atan2(t, Math.cos(dLon));
  const etaP = Math.atanh(Math.sin(dLon) / Math.sqrt(1 + t * t));
  let xi = xiP;
  let eta = etaP;
  for (const [i, a] of ALPHA.entries()) {
    const j = i + 1;
    xi += a * Math.sin(2 * j * xiP) * Math.cosh(2 * j * etaP);
    eta += a * Math.cos(2 * j * xiP) * Math.sinh(2 * j * etaP);
  }
  return {
    zone,
    north,
    easting: UTM_FALSE_EASTING + UTM_K0 * RECT * eta,
    northing: (north ? 0 : UTM_SOUTH_FALSE_NORTHING) + UTM_K0 * RECT * xi,
  };
}

export function fromUtm(zone: number, north: boolean, easting: number, northing: number): LatLon {
  const xi = (northing - (north ? 0 : UTM_SOUTH_FALSE_NORTHING)) / (UTM_K0 * RECT);
  const eta = (easting - UTM_FALSE_EASTING) / (UTM_K0 * RECT);
  let xiP = xi;
  let etaP = eta;
  for (const [i, b] of BETA.entries()) {
    const j = i + 1;
    xiP -= b * Math.sin(2 * j * xi) * Math.cosh(2 * j * eta);
    etaP -= b * Math.cos(2 * j * xi) * Math.sinh(2 * j * eta);
  }
  const chi = Math.asin(Math.sin(xiP) / Math.cosh(etaP));
  let phi = chi;
  for (const [i, d] of DELTA.entries()) phi += d * Math.sin(2 * (i + 1) * chi);
  const lon = centralMeridian(zone) + Math.atan2(Math.sinh(etaP), Math.cos(xiP)) / DEG;
  return { lat: phi / DEG, lon: wrapLongitude(lon) };
}

/** Project into UPS; the cap is the point's own hemisphere unless forced. */
export function toUps(lat: number, lon: number, north = lat >= 0): Ups {
  const phi = (north ? lat : -lat) * DEG;
  const sinPhi = Math.sin(phi);
  const t = Math.tan(Math.PI / 4 - phi / 2) / ((1 - E * sinPhi) / (1 + E * sinPhi)) ** (E / 2);
  const rho = UPS_RHO * t;
  const lambda = lon * DEG;
  return {
    north,
    easting: UPS_FALSE + rho * Math.sin(lambda),
    northing: north ? UPS_FALSE - rho * Math.cos(lambda) : UPS_FALSE + rho * Math.cos(lambda),
  };
}

export function fromUps(north: boolean, easting: number, northing: number): LatLon {
  const x = easting - UPS_FALSE;
  const y = northing - UPS_FALSE;
  const rho = Math.hypot(x, y);
  const t = rho / UPS_RHO;
  // The conformal latitude's fixed point: converges in a handful of steps.
  let phi = Math.PI / 2 - 2 * Math.atan(t);
  for (let step = 0; step < 12; step += 1) {
    const sinPhi = Math.sin(phi);
    const next = Math.PI / 2 - 2 * Math.atan(t * ((1 - E * sinPhi) / (1 + E * sinPhi)) ** (E / 2));
    if (Math.abs(next - phi) < 1e-13) {
      phi = next;
      break;
    }
    phi = next;
  }
  const lon = rho === 0 ? 0 : Math.atan2(x, north ? -y : y) / DEG;
  return { lat: (north ? phi : -phi) / DEG, lon: wrapLongitude(lon) };
}

/** The two letters of a UTM 100 km square. */
export function utmSquareId(zone: number, easting: number, northing: number): string | null {
  const set = zone % 6 || 6;
  const columns = UTM_COLUMNS[(set - 1) % 3] ?? "";
  const columnAt = Math.floor(easting / 100_000) - 1;
  if (columnAt < 0 || columnAt >= columns.length) return null;
  const column = columns.charAt(columnAt);
  // The rows repeat every 2 000 km; even sets start five letters on (F).
  const row = (((Math.floor(northing / 100_000) + (set % 2 === 0 ? 5 : 0)) % 20) + 20) % 20;
  return column + UTM_ROWS.charAt(row);
}

/** The polar zone letter (A/B south, Y/Z north) east or west of the 0°/180°
 * meridian a UPS point lies. */
export function upsZoneLetter(north: boolean, easting: number): string {
  return easting >= UPS_FALSE ? (north ? "Z" : "B") : north ? "Y" : "A";
}

/** The two letters of a UPS 100 km square. */
export function upsSquareId(north: boolean, easting: number, northing: number): string | null {
  const east = easting >= UPS_FALSE;
  const column = east
    ? UPS_EAST_COLUMNS[Math.floor((easting - UPS_FALSE) / 100_000)]
    : UPS_WEST_COLUMNS[Math.floor((easting - 800_000) / 100_000)];
  const row = UPS_ROWS[Math.floor((northing - (north ? 1_300_000 : 800_000)) / 100_000)];
  return column === undefined || row === undefined ? null : column + row;
}

function digits(value: number, precision: number): string {
  const metres = Math.floor(((value % 100_000) + 100_000) % 100_000);
  return String(Math.floor(metres / 10 ** (5 - precision))).padStart(precision, "0");
}

/** The MGRS reference of a point, `54S UE 88435 49293`, or `Z AH 00000
 * 00000` in a polar cap. `precision` is the digits per axis (1–5: 10 km to
 * 1 m); the digits are truncated, as MGRS names the square a point is in.
 * `null` for a point that is not a coordinate. */
export function toMgrs(lat: number, lon: number, precision = 5): string | null {
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90) return null;
  const p = Math.max(1, Math.min(5, Math.round(precision)));
  const band = latitudeBand(lat);
  if (band === null) {
    const ups = toUps(lat, lon);
    const square = upsSquareId(ups.north, ups.easting, ups.northing);
    if (square === null) return null;
    return `${upsZoneLetter(ups.north, ups.easting)} ${square} ${digits(ups.easting, p)} ${digits(ups.northing, p)}`;
  }
  const utm = toUtm(lat, lon);
  const square = utmSquareId(utm.zone, utm.easting, utm.northing);
  if (square === null) return null;
  return `${utm.zone}${band} ${square} ${digits(utm.easting, p)} ${digits(utm.northing, p)}`;
}

/** The UTM coordinate of a point, `54N 388436E 3949294N`: the zone and
 * hemisphere (N/S, not the MGRS band letter), and whole metres rounded to
 * `resolution` metres. In the polar caps the UPS form, `UPS N 2000000E
 * 2000000N`. `null` for a point that is not a coordinate. */
export function toUtmReference(lat: number, lon: number, resolution = 1): string | null {
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90) return null;
  const step = Math.max(1, resolution);
  const round = (value: number) => String(Math.round(Math.round(value / step) * step));
  if (latitudeBand(lat) === null) {
    const ups = toUps(lat, lon);
    return `UPS ${ups.north ? "N" : "S"} ${round(ups.easting)}E ${round(ups.northing)}N`;
  }
  const utm = toUtm(lat, lon);
  return `${utm.zone}${utm.north ? "N" : "S"} ${round(utm.easting)}E ${round(utm.northing)}N`;
}

/** The south-west corner of the square an MGRS reference names (spaces
 * optional), or `null` for one that does not parse or names no square. */
export function parseMgrs(text: string): LatLon | null {
  const match = /^(\d{1,2})?([A-Z])([A-Z])([A-Z])(\d*)$/.exec(text.replace(/\s+/g, "").toUpperCase());
  if (!match) return null;
  const zoneText = match[1];
  const zoneLetter = match[2] ?? "";
  const column = match[3] ?? "";
  const row = match[4] ?? "";
  const numbers = match[5] ?? "";
  if (numbers.length % 2 !== 0 || numbers.length > 10) return null;
  const p = numbers.length / 2;
  const scale = 10 ** (5 - p);
  const eDigits = p === 0 ? 0 : Number(numbers.slice(0, p)) * scale;
  const nDigits = p === 0 ? 0 : Number(numbers.slice(p)) * scale;
  if (zoneText === undefined) {
    const north = zoneLetter === "Y" || zoneLetter === "Z";
    if (!north && zoneLetter !== "A" && zoneLetter !== "B") return null;
    const east = zoneLetter === "B" || zoneLetter === "Z";
    const columnAt = (east ? UPS_EAST_COLUMNS : UPS_WEST_COLUMNS).indexOf(column);
    const rowAt = UPS_ROWS.indexOf(row);
    if (columnAt < 0 || rowAt < 0) return null;
    const easting = (east ? UPS_FALSE : 800_000) + columnAt * 100_000 + eDigits;
    const northing = (north ? 1_300_000 : 800_000) + rowAt * 100_000 + nDigits;
    return fromUps(north, easting, northing);
  }
  const zone = Number(zoneText);
  const bandAt = MGRS_BANDS.indexOf(zoneLetter);
  if (zone < 1 || zone > 60 || bandAt < 0) return null;
  const set = zone % 6 || 6;
  const columnAt = (UTM_COLUMNS[(set - 1) % 3] ?? "").indexOf(column);
  const rowAt = UTM_ROWS.indexOf(row);
  if (columnAt < 0 || rowAt < 0) return null;
  const easting = (columnAt + 1) * 100_000 + eDigits;
  const rowNorthing = ((((rowAt - (set % 2 === 0 ? 5 : 0)) % 20) + 20) % 20) * 100_000 + nDigits;
  // The row letters repeat every 2 000 km; the band says which cycle. Its
  // middle is within ~500 km of any point in it, the next cycle 1 500 km
  // away, so the nearest cycle is the one.
  const north = bandAt >= 10;
  const middle = toUtm(-80 + bandAt * 8 + (bandAt === 19 ? 6 : 4), centralMeridian(zone), zone, north).northing;
  const cycle = Math.round((middle - rowNorthing) / 2_000_000);
  return fromUtm(zone, north, easting, rowNorthing + cycle * 2_000_000);
}

/** A grid zone designation's cell: its UTM zone (0 for a polar one), its
 * name and its extent in degrees. */
export interface GzdCell {
  zone: number;
  name: string;
  west: number;
  east: number;
  south: number;
  north: number;
}

let cells: GzdCell[] | null = null;

/** Every grid zone designation, the polar A/B/Y/Z included. */
export function gzdCells(): readonly GzdCell[] {
  if (cells) return cells;
  const out: GzdCell[] = [];
  for (let band = 0; band < 20; band += 1) {
    const south = -80 + band * 8;
    const north = band === 19 ? 84 : south + 8;
    const letter = MGRS_BANDS.charAt(band);
    for (let zone = 1; zone <= 60; zone += 1) {
      let west = (zone - 1) * 6 - 180;
      let east = west + 6;
      if (letter === "V" && zone === 31) east = 3;
      if (letter === "V" && zone === 32) west = 3;
      if (letter === "X") {
        if (zone === 32 || zone === 34 || zone === 36) continue;
        if (zone === 31) east = 9;
        if (zone === 33) [west, east] = [9, 21];
        if (zone === 35) [west, east] = [21, 33];
        if (zone === 37) [west, east] = [33, 42];
      }
      out.push({ zone, name: `${zone}${letter}`, west, east, south, north });
    }
  }
  out.push({ zone: 0, name: "A", west: -180, east: 0, south: -90, north: -80 });
  out.push({ zone: 0, name: "B", west: 0, east: 180, south: -90, north: -80 });
  out.push({ zone: 0, name: "Y", west: -180, east: 0, south: 84, north: 90 });
  out.push({ zone: 0, name: "Z", west: 0, east: 180, south: 84, north: 90 });
  cells = out;
  return out;
}
