/**
 * Distances on the WGS84 ellipsoid, and the straight line between two points
 * at their heights.
 *
 * The ellipsoid distance is what a chart, a navigation system or an
 * aviation route reads: the shortest path along the sea-level surface. It
 * differs from the sphere's great circle by up to about half a percent with
 * the direction and the latitude. The straight line is the chord between
 * two points in space (Earth-centred coordinates), the line of sight they
 * would have through air or rock.
 */

/** WGS84 semi-major axis, metres, and flattening. */
export const WGS84_A = 6378137;
export const WGS84_F = 1 / 298.257223563;
const WGS84_B = WGS84_A * (1 - WGS84_F);
const E2 = WGS84_F * (2 - WGS84_F);

/** The authalic radius: the sphere with the ellipsoid's surface area, which
 * an area on the sphere is reckoned on. */
export const AUTHALIC_RADIUS = 6371007.181;

const RADIANS = Math.PI / 180;

/** The ellipsoid distance between two points, metres (Vincenty's inverse
 * formula, good to well under a millimetre). Nearly antipodal points, where
 * the iteration does not converge, fall back to the great circle on the
 * sphere of the ellipsoid's mean radius: off by a fraction of a percent at
 * twenty thousand kilometres. */
export function geodesicDistance(lon1: number, lat1: number, lon2: number, lat2: number): number {
  const f = WGS84_F;
  const L = (lon2 - lon1) * RADIANS;
  const U1 = Math.atan((1 - f) * Math.tan(lat1 * RADIANS));
  const U2 = Math.atan((1 - f) * Math.tan(lat2 * RADIANS));
  const sinU1 = Math.sin(U1);
  const cosU1 = Math.cos(U1);
  const sinU2 = Math.sin(U2);
  const cosU2 = Math.cos(U2);
  let lambda = L;
  for (let iteration = 0; iteration < 200; iteration += 1) {
    const sinLambda = Math.sin(lambda);
    const cosLambda = Math.cos(lambda);
    const sinSigma = Math.hypot(cosU2 * sinLambda, cosU1 * sinU2 - sinU1 * cosU2 * cosLambda);
    if (sinSigma === 0) return 0;
    const cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLambda;
    const sigma = Math.atan2(sinSigma, cosSigma);
    const sinAlpha = (cosU1 * cosU2 * sinLambda) / sinSigma;
    const cos2Alpha = 1 - sinAlpha * sinAlpha;
    // On the equator cos²α is zero and the term drops out.
    const cos2SigmaM = cos2Alpha === 0 ? 0 : cosSigma - (2 * sinU1 * sinU2) / cos2Alpha;
    const C = (f / 16) * cos2Alpha * (4 + f * (4 - 3 * cos2Alpha));
    const previous = lambda;
    lambda = L + (1 - C) * f * sinAlpha * (sigma + C * sinSigma * (cos2SigmaM + C * cosSigma * (-1 + 2 * cos2SigmaM * cos2SigmaM)));
    if (Math.abs(lambda - previous) < 1e-12) {
      const u2 = (cos2Alpha * (WGS84_A * WGS84_A - WGS84_B * WGS84_B)) / (WGS84_B * WGS84_B);
      const A = 1 + (u2 / 16384) * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)));
      const B = (u2 / 1024) * (256 + u2 * (-128 + u2 * (74 - 47 * u2)));
      const deltaSigma =
        B *
        sinSigma *
        (cos2SigmaM +
          (B / 4) *
            (cosSigma * (-1 + 2 * cos2SigmaM * cos2SigmaM) -
              (B / 6) * cos2SigmaM * (-3 + 4 * sinSigma * sinSigma) * (-3 + 4 * cos2SigmaM * cos2SigmaM)));
      return WGS84_B * A * (sigma - deltaSigma);
    }
  }
  return sphereDistance(lon1, lat1, lon2, lat2, (2 * WGS84_A + WGS84_B) / 3);
}

function sphereDistance(lon1: number, lat1: number, lon2: number, lat2: number, radius: number): number {
  const phi1 = lat1 * RADIANS;
  const phi2 = lat2 * RADIANS;
  const h = Math.sin((phi2 - phi1) / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(((lon2 - lon1) * RADIANS) / 2) ** 2;
  return 2 * radius * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** A point at a height above the ellipsoid in Earth-centred, Earth-fixed
 * coordinates, metres. */
export function ecef(lon: number, lat: number, height: number): [number, number, number] {
  const phi = lat * RADIANS;
  const lambda = lon * RADIANS;
  const n = WGS84_A / Math.sqrt(1 - E2 * Math.sin(phi) ** 2);
  return [
    (n + height) * Math.cos(phi) * Math.cos(lambda),
    (n + height) * Math.cos(phi) * Math.sin(lambda),
    (n * (1 - E2) + height) * Math.sin(phi),
  ];
}

/** The straight line between two points at their heights, metres: the
 * chord, which for far points runs under the Earth's curve. */
export function straightLineDistance(
  a: { lon: number; lat: number; height: number },
  b: { lon: number; lat: number; height: number },
): number {
  const [ax, ay, az] = ecef(a.lon, a.lat, a.height);
  const [bx, by, bz] = ecef(b.lon, b.lat, b.height);
  return Math.hypot(bx - ax, by - ay, bz - az);
}
