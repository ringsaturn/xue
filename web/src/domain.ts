/**
 * Lambert conformal conic arithmetic, and the footprint of a source computed
 * on that projection.
 *
 * A bundle whose metadata declares `projection` lies on its native grid and
 * `gridmap.ts` addresses it through these formulas. A run the encoder
 * resampled onto a regular latitude/longitude grid instead (HRRR's older
 * runs and published cases) keeps a rectangle around the model's footprint:
 * a conic domain is a trapezoid on it, and the corners the model never
 * covered hold the nearest source cell continued outwards. That extension is
 * not a forecast, and `LambertDomain` is what keeps it off the map: the
 * model's own grid, in the shell's registry beside the model, and one test —
 * project the point, ask whether it lands inside the grid — that the raster
 * shader, the particle overlay, the probe and the contour labels all run.
 *
 * Snyder (1987) §15, spherical case, the same formulas the encoder uses.
 */

/** The constants of a conic projection, without any grid laid on it. */
export interface LambertParameters {
  /** Sphere radius in metres. */
  radius: number;
  standardParallel1: number;
  standardParallel2: number;
  latitudeOfOrigin: number;
  centralMeridian: number;
}

export interface LambertDomain extends LambertParameters {
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

export function lambertCone(domain: LambertParameters): LambertCone {
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

/** How close to a pole a latitude is taken before projecting: the far pole of
 * a cone is infinitely far away, and a view that holds it must still cover
 * what lies on the near side. A literal in GLSL too, so one decimal place. */
export const LAMBERT_LATITUDE_LIMIT = "89.9";

/** Projected metres of one point. */
export function lambertForward(domain: LambertParameters, cone: LambertCone, longitude: number, latitude: number): [number, number] {
  const rho = cone.scale / Math.tan(Math.PI / 4 + ((latitude * Math.PI) / 180) / 2) ** cone.n;
  // The meridian's angle from the central one, the difference wrapped so a
  // longitude spelled the other way round the globe lands the same.
  let delta = longitude - domain.centralMeridian;
  delta -= 360 * Math.round(delta / 360);
  const theta = cone.n * ((delta * Math.PI) / 180);
  return [rho * Math.sin(theta), cone.originRadius - rho * Math.cos(theta)];
}

/** The longitude and latitude of projected metres. The longitude is spelled
 * within half a turn of the central meridian. */
export function lambertInverse(domain: LambertParameters, cone: LambertCone, x: number, y: number): [number, number] {
  const sign = Math.sign(cone.n);
  const across = cone.originRadius - y;
  const rho = sign * Math.hypot(x, across);
  const theta = Math.atan2(sign * x, sign * across);
  const longitude = domain.centralMeridian + (theta / cone.n) * (180 / Math.PI);
  if (rho === 0) return [longitude, sign * 90];
  const latitude = 2 * Math.atan((cone.scale / rho) ** (1 / cone.n)) - Math.PI / 2;
  return [longitude, (latitude * 180) / Math.PI];
}

/** How much the projection stretches the ground at a latitude: projected
 * metres per metre on the sphere, 1 on a standard parallel. */
export function lambertMapFactor(domain: LambertParameters, cone: LambertCone, latitude: number): number {
  const phi = (latitude * Math.PI) / 180;
  const rho = cone.scale / Math.tan(Math.PI / 4 + phi / 2) ** cone.n;
  return (cone.n * rho) / (domain.radius * Math.cos(phi));
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

/** A grid as GLSL addresses it (`gridmap.ts::gridUniforms` fills the
 * uniforms): `gridCell` is the continuous cell of a longitude and latitude,
 * cell centres on the integers. `first` and `cellStep` are degrees on a
 * regular grid, projected metres on a Lambert one; `cone` is the projection's
 * (`n`, scale, origin radius, central meridian in radians), and `n` of 0, which
 * no cone has, says the grid is regular. The latitude is held short of the
 * poles because the far pole is a division by zero and a NaN passes every
 * bounds test. This runs the trigonometry on the GPU, whose `sin`, `tan` and
 * `pow` carry no precision guarantee: a 5e-4 error on a radius of 8,000 km is
 * 4 km, so what must land on the right cell of a 3 km grid goes through
 * `GRID_TABLE_GLSL` instead. */
export const GRID_GLSL = `
vec2 lambertXY(vec4 cone, float longitude, float latitude) {
  float n = cone.x;
  float rho = cone.y / pow(tan(0.7853981633974483 + radians(clamp(latitude, -${LAMBERT_LATITUDE_LIMIT}, ${LAMBERT_LATITUDE_LIMIT})) * 0.5), n);
  float delta = radians(longitude) - cone.w;
  delta -= 6.283185307179586 * floor(delta / 6.283185307179586 + 0.5);
  float theta = n * delta;
  return vec2(rho * sin(theta), cone.z - rho * cos(theta));
}
vec2 gridCell(vec4 cone, vec2 first, vec2 cellStep, float longitude, float latitude) {
  if (cone.x == 0.0) return vec2(mod(longitude - first.x, 360.0) / cellStep.x, (latitude - first.y) / cellStep.y);
  return (lambertXY(cone, longitude, latitude) - first) / cellStep;
}`;

/** What a Lambert grid's raster draws through: the projected metres, computed
 * in doubles on the CPU over a Mercator rectangle (`gridmap.ts::gridTable`),
 * read by hand-written bilinear from an RG32F texture, so no trigonometry runs
 * per pixel. `tableCell` falls back to `gridCell` for a grid on another
 * projection, or a regular one. */
export const GRID_TABLE_GLSL = `
uniform highp sampler2D u_grid_table;
uniform vec4 u_grid_table_box;
uniform vec4 u_grid_table_cone;
vec2 gridTableXY(vec2 mercator) {
  ivec2 size = textureSize(u_grid_table, 0);
  vec2 extent = u_grid_table_box.zw - u_grid_table_box.xy;
  vec2 offset = vec2(fract(mercator.x - u_grid_table_box.x), mercator.y - u_grid_table_box.y);
  if (offset.x > extent.x || offset.y < 0.0 || offset.y > extent.y) return vec2(1e30);
  vec2 at = offset / extent * vec2(size - 1);
  ivec2 low = min(ivec2(at), size - 2);
  vec2 f = at - vec2(low);
  vec2 upper = mix(texelFetch(u_grid_table, low, 0).rg, texelFetch(u_grid_table, low + ivec2(1, 0), 0).rg, f.x);
  vec2 lower = mix(texelFetch(u_grid_table, low + ivec2(0, 1), 0).rg, texelFetch(u_grid_table, low + ivec2(1, 1), 0).rg, f.x);
  return mix(upper, lower, f.y);
}
vec2 tableCell(vec4 cone, vec2 first, vec2 cellStep, vec2 mercator, float longitude, float latitude) {
  if (cone.x != 0.0 && cone == u_grid_table_cone) return (gridTableXY(mercator) - first) / cellStep;
  return gridCell(cone, first, cellStep, longitude, latitude);
}`;

/** The GLSL of the footprint test, for a fragment shader that already has
 * the pixel's longitude and latitude in degrees and includes `GRID_GLSL`.
 * `u_domain` is 1.0 when a domain is set; the two vec4s are `domainUniforms`. */
export const DOMAIN_GLSL = `
uniform float u_domain;
uniform vec4 u_domain_cone;
uniform vec4 u_domain_box;
bool outsideDomain(float longitude, float latitude) {
  if (u_domain < 0.5) return false;
  vec2 xy = lambertXY(u_domain_cone, longitude, latitude);
  return xy.x < u_domain_box.x || xy.x > u_domain_box.y || xy.y < u_domain_box.z || xy.y > u_domain_box.w;
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
 * half a cell beyond the outermost centers. Clips a run the encoder resampled;
 * a native grid is its own footprint.
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
type GeoBox = readonly [number, number, number, number];

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
  const height = Math.min(north, viewNorth) - Math.max(south, viewSouth);
  if (height <= 0) return 0;
  // The region and the view each spell their longitudes in some copy of the
  // world: a satellite disk runs from 80.7 to 200.7, and the map reports a
  // view over the same water as [-185, -165] or [175, 195] depending on
  // which way it was panned there. Every copy of the region the view shows
  // counts, which is also what a view wider than the world needs.
  let width = 0;
  for (const shift of [-360, 0, 360]) {
    const overlap = Math.min(east + shift, viewEast) - Math.max(west + shift, viewWest);
    if (overlap > 0) width += overlap;
  }
  if (width <= 0) return 0;
  return (width * height) / viewArea;
}
