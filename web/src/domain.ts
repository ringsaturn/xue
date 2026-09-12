/**
 * The footprint of a source computed on a map projection.
 *
 * A bundle carries a regular latitude/longitude grid and nothing else
 * (docs/format.md). A model that runs on a projection — HRRR, Lambert
 * conformal conic, 3 km over the contiguous United States — is resampled
 * onto one by the encoder (xuebuild/reproject.py), whose target is the
 * rectangle around the model's footprint: a conic domain is a trapezoid on
 * it, and the corners the model never covered hold the nearest source cell
 * continued outwards, so the plane is complete and no filter meets an edge.
 * That extension is not a forecast, and this is what keeps it off the map:
 * the model's own grid, in the shell's registry beside the model, and one
 * test — project the point, ask whether it lands inside the grid — that the
 * raster shader, the particle overlay, the probe and the contour labels all
 * run. The shell knows the domain the way it knows a model's grid size;
 * the bundle need not say.
 *
 * Snyder (1987) §15, spherical case, the same formulas the encoder uses.
 */

export interface LambertDomain {
  /** Sphere radius in metres. */
  radius: number;
  standardParallel1: number;
  standardParallel2: number;
  latitudeOfOrigin: number;
  centralMeridian: number;
  /** Projected coordinates of the *edges* of the grid, in metres: what a
   * point must fall within to be covered by some cell. */
  west: number;
  east: number;
  south: number;
  north: number;
}

/** The derived constants of the cone: `n` (the cone constant), `R·F` (the
 * scale) and the radius of the origin's parallel. */
export interface LambertCone {
  n: number;
  scale: number;
  originRadius: number;
}

export function lambertCone(domain: LambertDomain): LambertCone {
  const phi1 = (domain.standardParallel1 * Math.PI) / 180;
  const phi2 = (domain.standardParallel2 * Math.PI) / 180;
  const n =
    phi1 === phi2
      ? Math.sin(phi1)
      : Math.log(Math.cos(phi1) / Math.cos(phi2)) /
        Math.log(Math.tan(Math.PI / 4 + phi2 / 2) / Math.tan(Math.PI / 4 + phi1 / 2));
  const scale = (domain.radius * Math.cos(phi1) * Math.tan(Math.PI / 4 + phi1 / 2) ** n) / n;
  const originRadius = scale / Math.tan(Math.PI / 4 + ((domain.latitudeOfOrigin * Math.PI) / 180) / 2) ** n;
  return { n, scale, originRadius };
}

/** Projected metres of one point. */
export function lambertForward(domain: LambertDomain, cone: LambertCone, longitude: number, latitude: number): [number, number] {
  const rho = cone.scale / Math.tan(Math.PI / 4 + ((latitude * Math.PI) / 180) / 2) ** cone.n;
  // The meridian's angle from the central one, the difference wrapped so a
  // longitude spelled the other way round the globe lands the same.
  let delta = longitude - domain.centralMeridian;
  delta -= 360 * Math.round(delta / 360);
  const theta = cone.n * ((delta * Math.PI) / 180);
  return [rho * Math.sin(theta), cone.originRadius - rho * Math.cos(theta)];
}

/** Whether a point lies over the model's own grid. */
export function domainContains(domain: LambertDomain, cone: LambertCone, longitude: number, latitude: number): boolean {
  const [x, y] = lambertForward(domain, cone, longitude, latitude);
  return x >= domain.west && x <= domain.east && y >= domain.south && y <= domain.north;
}

/** A domain as the two vec4 uniforms the shaders take: the cone
 * (`n`, scale, origin radius, central meridian in radians) and the box
 * (west, east, south, north) in metres. */
export function domainUniforms(domain: LambertDomain): { cone: [number, number, number, number]; box: [number, number, number, number] } {
  const cone = lambertCone(domain);
  return {
    cone: [cone.n, cone.scale, cone.originRadius, (domain.centralMeridian * Math.PI) / 180],
    box: [domain.west, domain.east, domain.south, domain.north],
  };
}

/** The GLSL of the same test, for a fragment shader that already has the
 * pixel's longitude and latitude in degrees. `u_domain` is 1.0 when a
 * domain is set; the two vec4s are `domainUniforms`. Single precision is
 * ample: the box is a few thousand kilometres across and a float resolves a
 * metre of it. */
export const DOMAIN_GLSL = `
uniform float u_domain;
uniform vec4 u_domain_cone;
uniform vec4 u_domain_box;
bool outsideDomain(float longitude, float latitude) {
  if (u_domain < 0.5) return false;
  float n = u_domain_cone.x;
  float rho = u_domain_cone.y / pow(tan(0.7853981633974483 + radians(latitude) * 0.5), n);
  float delta = radians(longitude) - u_domain_cone.w;
  delta -= 6.283185307179586 * floor(delta / 6.283185307179586 + 0.5);
  float theta = n * delta;
  float x = rho * sin(theta);
  float y = u_domain_cone.z - rho * cos(theta);
  return x < u_domain_box.x || x > u_domain_box.y || y < u_domain_box.z || y > u_domain_box.w;
}`;

/** The names `DOMAIN_GLSL` declares, for a program's uniform lookup. */
export const DOMAIN_UNIFORM_NAMES = ["u_domain", "u_domain_cone", "u_domain_box"] as const;

/** Upload a domain (or none) to a program that includes `DOMAIN_GLSL`. */
export function setDomainUniforms(
  gl: WebGL2RenderingContext,
  uniforms: Record<string, WebGLUniformLocation | null>,
  domain: LambertDomain | null,
): void {
  gl.uniform1f(uniforms.u_domain!, domain ? 1 : 0);
  if (!domain) return;
  const { cone, box } = domainUniforms(domain);
  gl.uniform4f(uniforms.u_domain_cone!, cone[0], cone[1], cone[2], cone[3]);
  gl.uniform4f(uniforms.u_domain_box!, box[0], box[1], box[2], box[3]);
}

/**
 * HRRR's CONUS grid, GRIB2 grid definition template 3.30 as NCEP fills it:
 * 1799 x 1059 cells of 3 km on a sphere of 6371229 m, both standard
 * parallels and the origin at 38.5 N, the central meridian at 97.5 W, the
 * first cell's center at 21.138123 N, 122.719528 W — which the projection
 * puts at (-2697520.14, -1587306.15) m. The box is that grid's outer edge,
 * half a cell beyond the outermost centers. Mirrors what
 * `xuebuild/reproject.py` reads out of the file.
 */
export const HRRR_DOMAIN: LambertDomain = {
  radius: 6371229,
  standardParallel1: 38.5,
  standardParallel2: 38.5,
  latitudeOfOrigin: 38.5,
  centralMeridian: -97.5,
  west: -2699020.14252193,
  east: -2699020.14252193 + 1799 * 3000,
  south: 1588193.8474433357 - 1059 * 3000,
  north: 1588193.8474433357,
};

/** A rectangle of the globe as [west, south, east, north] in degrees — a
 * model's `region`, or the map's own bounds, whose longitudes run past ±180
 * when the view is wider than the world. */
export type GeoBox = readonly [number, number, number, number];

/** How much of the view a region fills: the area of their overlap as a
 * fraction of the view's, in degrees squared. Zero when they miss each
 * other; one when the view sits wholly over the region. The shell uses it
 * to decide whether a viewer opening a regional model is already looking at
 * it — a world view overlaps every region and shows none of them. */
export function regionShareOfView(region: GeoBox, view: GeoBox): number {
  const [west, south, east, north] = region;
  const [viewWest, viewSouth, viewEast, viewNorth] = view;
  const viewArea = (viewEast - viewWest) * (viewNorth - viewSouth);
  if (!(viewArea > 0)) return 0;
  const width = Math.min(east, viewEast) - Math.max(west, viewWest);
  const height = Math.min(north, viewNorth) - Math.max(south, viewSouth);
  if (width <= 0 || height <= 0) return 0;
  return (width * height) / viewArea;
}
