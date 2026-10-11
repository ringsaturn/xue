// A volume drawn as a 3D texture and raymarched: a volume bundle's
// constant-altitude levels (the MRMS reflectivity `refl3d`, the WRF cloud
// water `cloud3d`) stacked into one texture per frame, the ray from the
// camera through each pixel of the box the volume fills, front to back
// with a transfer function chosen by what the levels are: reflectivity
// through its palette, cloud water as a lit Beer–Lambert medium.
//
// On the plane the box is a Mercator box whose height is the altitude
// scaled by one exaggerated metres-to-Mercator factor, and with the map's
// terrain on, the march stops where the terrain already drawn is in front
// of it (MapLibre's packed terrain depth), so a mountain hides the cloud
// behind it; on the globe it is a shell over the box in MapLibre's
// unit-sphere space, its own program, the ray stopped by the planet and
// not by the relief.

import type { CustomLayerInterface, CustomRenderMethodInput, Map as MapLibreMap } from "maplibre-gl";
import { MercatorCoordinate } from "maplibre-gl";
import type { BundleParameter, BundleVariable, LinearQuantization } from "./manifest";
import { isGlobe, TERRAIN_DEPTH_GLSL, terrainDepthTexture } from "./projection";
import { isDark } from "./theme";

export { VOLUME_BUNDLE_LEVELS } from "./manifest";

/** The vertical exaggeration a wide volume is drawn at unless the URL asks
 * for another (`?vexag=`): CONUS is 7000 km wide and its echoes reach
 * 19 km, so at true scale the volume is a film on the map. */
export const DEFAULT_VERTICAL_EXAGGERATION = 10;

/** The longitude span under which a volume is drawn at true scale: a box a
 * few tens of kilometres across (a WRF nest) is as wide as its cloud is
 * tall, and stretching it would stand the cloud off the mountain it sits
 * on. */
const TRUE_SCALE_SPAN_DEGREES = 2;

/** The vertical exaggeration a volume is drawn at by default, by how wide
 * its grid is: true scale under two degrees of longitude, the wide-area
 * exaggeration otherwise. */
export function defaultVerticalExaggeration(grid: { width: number; longitudeStep: number }): number {
  return Math.abs(grid.width * grid.longitudeStep) < TRUE_SCALE_SPAN_DEGREES ? 1 : DEFAULT_VERTICAL_EXAGGERATION;
}

/** How a volume's codes become light: radar reflectivity through its
 * palette, or cloud water as an extinguishing, sunlit medium. */
export type VolumeTransfer = "reflectivity" | "cloud";

/** The transfer function for a volume's levels, by their parameter: cloud
 * water mixing ratio (0/1/22) is a cloud, anything else (the MRMS
 * reflectivity, 209/9/0) draws through its palette. */
export function volumeTransfer(parameter: BundleParameter | null | undefined): VolumeTransfer {
  return parameter && parameter.discipline === 0 && parameter.parameterCategory === 1 && parameter.parameterNumber === 22
    ? "cloud"
    : "reflectivity";
}

/** Reflectivity below which the transfer function is clear and at which it
 * is fully dense, in dBZ: light stratiform echo stays a thin veil, so it
 * cannot bury the convective cores that read as solids inside it. */
const CLEAR_DBZ = 25;
const DENSE_DBZ = 55;

/** How much one voxel of the densest echo occludes: well under one, so a
 * stratiform shield hundreds of voxels deep reads as a veil and the cores
 * inside it still show. */
const VOXEL_OPACITY = 0.6;

/** Cloud water's extinction, per g/kg per voxel of path (one texel across):
 * opacity is 1 − exp(−σ · q · path). A real cloud is far denser than a
 * voxel can show: 0.3 g/kg (0.3 g/m³, 10 µm droplets) extinguishes at
 * about 0.05 /m, an optical depth of 25 across a 500 m voxel, so a deck
 * one level thick is a wall, not a haze. σ is a fifth of that: a 0.2 g/kg
 * layer is opaque within one voxel (so the thin deck on the lowest levels
 * reads as a deck), while a 0.05 g/kg wisp is still half transparent
 * through one and keeps the edges soft. */
const CLOUD_SIGMA = 16;

/** Cloud water under which a sample is clear, in g/kg: below it the linear
 * filter's ramp between a cloudy and a clear voxel would paint a halo of
 * haze around every cloud. */
const CLOUD_CLEAR_GPKG = 0.01;

/** The sun the cloud is lit by, fixed: from the south-west at 45°
 * elevation, the cartographer's light turned to the afternoon, so the
 * side of a deck facing a tilted camera from the south reads lit. As a
 * direction in the box's Mercator space: x east, y south, z up. */
const SUN_DIRECTION: readonly [number, number, number] = [-0.5, 0.5, Math.SQRT1_2];

/** How far towards the sun the one shadow sample is taken, in voxels, and
 * how much path it stands for: a sample two voxels up-sun answers "is
 * there cloud between this point and the light", and the light through
 * it is exp(−σ · q · path). The path is short because σ is large: with
 * σ · path near 12, cloud of 0.05 g/kg up-sun leaves half the light,
 * 0.1 g/kg a third and 0.3 g/kg almost none, so a deck's sunward face is
 * lit, its far side dark and the slope between them a gradient that gives
 * a mountain-side cloud its relief; at a whole voxel of path everything
 * behind the first voxel of cloud would be shade-coloured and the cloud a
 * flat block. */
const SHADOW_OFFSET_VOXELS = 2;
const SHADOW_PATH_VOXELS = 0.75;

/** The silver lining: how much a thin, lit edge brightens when the camera
 * looks towards the sun through it, and how tight that forward lobe is
 * (the power of the cosine between the view ray and the sun). */
const SILVER_STRENGTH = 0.6;
const SILVER_POWER = 6;

/** The cloud's lit and shaded colours per theme. On the paper basemap the
 * cloud sits over warm fills (the temperature palettes run yellow to red),
 * so it leans the other way: a cool blue-white lit side, brighter than the
 * yellows, over a slate-blue shade dark enough to read as a body rather
 * than a haze. On the dark basemap it is a warm white over a grey. */
type Rgb = readonly [number, number, number];
const CLOUD_COLOURS: Record<"light" | "dark", { lit: Rgb; shade: Rgb }> = {
  light: { lit: [0.9, 0.95, 1.0], shade: [0.36, 0.44, 0.58] },
  dark: { lit: [1.0, 0.97, 0.92], shade: [0.46, 0.47, 0.5] },
};

/** Cloud water above which a section paints the palette, in g/kg. */
const SECTION_CLOUD_GPKG = 0.01;

/** Bins in the altitude-to-level lookup, over `[0, top]`. */
const LEVEL_LOOKUP_SIZE = 1024;

/** The longest march a ray takes, in samples. */
const MAX_STEPS = 256;

/** The levels of a volume bundle: its variables in bundle order, which the
 * encoders write bottom to top, with each one's altitude in metres. */
export interface VolumeLevels {
  variables: BundleVariable[];
  altitudes: number[];
}

/** The levels of a bundle every variable of which is one altitude of one
 * quantity — the same parameter on a "specific altitude above mean sea
 * level" surface (GRIB2 type 102), ascending, on a linear codebook — or
 * null for anything else. */
export function volumeLevels(variables: readonly BundleVariable[]): VolumeLevels | null {
  const first = variables[0]?.parameter;
  if (variables.length < 2 || !first) return null;
  const altitudes: number[] = [];
  for (const variable of variables) {
    const parameter = variable.parameter;
    if (
      !parameter ||
      variable.quantization.type !== "linear" ||
      parameter.discipline !== first.discipline ||
      parameter.parameterCategory !== first.parameterCategory ||
      parameter.parameterNumber !== first.parameterNumber ||
      parameter.typeOfFirstFixedSurface !== 102 ||
      parameter.scaledValueOfFirstFixedSurface === null ||
      parameter.scaleFactorOfFirstFixedSurface === null
    ) {
      return null;
    }
    const altitude = parameter.scaledValueOfFirstFixedSurface / 10 ** parameter.scaleFactorOfFirstFixedSurface;
    if (altitudes.length > 0 && altitude <= altitudes[altitudes.length - 1]!) return null;
    altitudes.push(altitude);
  }
  return { variables: [...variables], altitudes };
}

/** The top of the box: half a level step above the highest level. */
export function volumeTop(altitudes: readonly number[]): number {
  const count = altitudes.length;
  const last = altitudes[count - 1]!;
  return count > 1 ? last + (last - altitudes[count - 2]!) / 2 : last;
}

/** The altitude-to-texture lookup: for each of `size` bins over
 * `[0, top]`, the 3D texture's third coordinate at that altitude —
 * `(index + 0.5) / count` at a level, linear between two, clamped to the
 * bottom and top levels outside them — so the shader takes non-uniform
 * level spacing with one texture read. */
export function levelLookup(altitudes: readonly number[], top: number, size = LEVEL_LOOKUP_SIZE): Float32Array {
  const count = altitudes.length;
  const table = new Float32Array(size);
  let below = 0;
  for (let bin = 0; bin < size; bin += 1) {
    const altitude = ((bin + 0.5) / size) * top;
    while (below < count - 2 && altitude >= altitudes[below + 1]!) below += 1;
    let index: number;
    if (altitude <= altitudes[0]!) index = 0;
    else if (altitude >= altitudes[count - 1]!) index = count - 1;
    else {
      const lower = altitudes[below]!;
      const upper = altitudes[below + 1]!;
      index = below + (altitude - lower) / (upper - lower);
    }
    table[bin] = (index + 0.5) / count;
  }
  return table;
}

/** The camera's position in the space a projection matrix maps from: the
 * point every one of the matrix's x, y and w rows sends to zero, found from
 * those three rows alone. The z row — depth — never enters, which matters:
 * MapLibre's matrix is not usefully invertible through it. Column-major, as
 * WebGL and MapLibre store it. Null when the rows are degenerate. */
export function cameraFromMatrix(matrix: ArrayLike<number>): [number, number, number] | null {
  const row = (r: number): [number, number, number, number] => [
    matrix[r]!,
    matrix[4 + r]!,
    matrix[8 + r]!,
    matrix[12 + r]!,
  ];
  const [a, b, c] = [row(0), row(1), row(3)];
  // Solve [a b c](0..2) · p = -(a b c)(3) by Cramer's rule.
  const det3 = (
    m00: number, m01: number, m02: number,
    m10: number, m11: number, m12: number,
    m20: number, m21: number, m22: number,
  ): number => m00 * (m11 * m22 - m12 * m21) - m01 * (m10 * m22 - m12 * m20) + m02 * (m10 * m21 - m11 * m20);
  const d = det3(a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2]);
  if (!Number.isFinite(d) || Math.abs(d) < 1e-30) return null;
  const [ra, rb, rc] = [-a[3], -b[3], -c[3]];
  return [
    det3(ra, a[1], a[2], rb, b[1], b[2], rc, c[1], c[2]) / d,
    det3(a[0], ra, a[2], b[0], rb, b[2], c[0], rc, c[2]) / d,
    det3(a[0], a[1], ra, b[0], b[1], rb, c[0], c[1], rc) / d,
  ];
}

/** Where a volume sits: its grid's extent as cell edges, in degrees, and in
 * Mercator with the box's height. */
export interface VolumeBox {
  west: number;
  east: number;
  north: number;
  south: number;
  min: [number, number, number];
  max: [number, number, number];
  /** Mercator units per metre of altitude, exaggeration included. */
  zScale: number;
  top: number;
}

/** The box of a regular latitude/longitude grid whose cell centres start at
 * `(firstLongitude, firstLatitude)` and step by the given amounts (latitude
 * negative, north to south), drawn `exaggeration` times its height. */
export function volumeBox(
  grid: {
    width: number;
    height: number;
    firstLongitude: number;
    firstLatitude: number;
    longitudeStep: number;
    latitudeStep: number;
  },
  top: number,
  exaggeration: number,
): VolumeBox {
  const west = grid.firstLongitude - grid.longitudeStep / 2;
  const east = west + grid.width * grid.longitudeStep;
  const north = grid.firstLatitude - grid.latitudeStep / 2;
  const south = north + grid.height * grid.latitudeStep;
  const northWest = MercatorCoordinate.fromLngLat([west, north]);
  const southEast = MercatorCoordinate.fromLngLat([east, south]);
  // One factor for the whole box, the one at its middle latitude: the
  // exaggeration dwarfs the few tens of percent Mercator's own vertical
  // stretch would vary by across it.
  const middle = MercatorCoordinate.fromLngLat([(west + east) / 2, (north + south) / 2]);
  const zScale = middle.meterInMercatorCoordinateUnits() * exaggeration;
  return {
    west,
    east,
    north,
    south,
    min: [northWest.x, northWest.y, 0],
    max: [southEast.x, southEast.y, top * zScale],
    zScale,
    top,
  };
}

/** Halve a plane by block maximum when a texture dimension is over the
 * device's 3D texture limit: the strongest return of each 2 x 2 block, the
 * way the encoder thins the mosaic. */
export function maxPool2(plane: Uint8Array, width: number, height: number): Uint8Array {
  const halfWidth = Math.ceil(width / 2);
  const halfHeight = Math.ceil(height / 2);
  const out = new Uint8Array(halfWidth * halfHeight);
  for (let row = 0; row < halfHeight; row += 1) {
    for (let column = 0; column < halfWidth; column += 1) {
      let best = 0;
      for (let dy = 0; dy < 2; dy += 1) {
        const y = row * 2 + dy;
        if (y >= height) continue;
        for (let dx = 0; dx < 2; dx += 1) {
          const x = column * 2 + dx;
          if (x >= width) continue;
          best = Math.max(best, plane[y * width + x]!);
        }
      }
      out[row * halfWidth + column] = best;
    }
  }
  return out;
}

/** A regular latitude/longitude grid: its size and its first cell centre. */
export interface VolumeGrid {
  width: number;
  height: number;
  firstLongitude: number;
  firstLatitude: number;
  longitudeStep: number;
  latitudeStep: number;
}

/** A rectangle of grid cells: the part of the grid a close-up holds. */
export interface GridRegion {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** The cells a set of tile rectangles covers (inclusive tile indices, the
 * decoder's shape), as one rectangle clipped to the grid, or null for none
 * — the whole plane. */
export function tileRegion(
  rects: readonly { firstColumn: number; firstRow: number; lastColumn: number; lastRow: number }[] | null,
  tile: { tileWidth: number; tileHeight: number },
  width: number,
  height: number,
): GridRegion | null {
  if (!rects || rects.length === 0) return null;
  const firstColumn = Math.min(...rects.map((rect) => rect.firstColumn));
  const firstRow = Math.min(...rects.map((rect) => rect.firstRow));
  const lastColumn = Math.max(...rects.map((rect) => rect.lastColumn));
  const lastRow = Math.max(...rects.map((rect) => rect.lastRow));
  const x = firstColumn * tile.tileWidth;
  const y = firstRow * tile.tileHeight;
  const right = Math.min(width, (lastColumn + 1) * tile.tileWidth);
  const bottom = Math.min(height, (lastRow + 1) * tile.tileHeight);
  if (right <= x || bottom <= y) return null;
  return { x, y, width: right - x, height: bottom - y };
}

/** The grid a region of `grid` is: the same steps from its own first cell. */
export function regionGrid(grid: VolumeGrid, region: GridRegion): VolumeGrid {
  return {
    ...grid,
    width: region.width,
    height: region.height,
    firstLongitude: grid.firstLongitude + region.x * grid.longitudeStep,
    firstLatitude: grid.firstLatitude + region.y * grid.latitudeStep,
  };
}

/** MapLibre's globe radius, in metres: the unit sphere its globe matrix
 * maps from is this many metres across a radius. */
export const GLOBE_RADIUS = 6371008.8;

/** A point in the unit-sphere space MapLibre's globe matrix maps from (the
 * projection prelude's `projectToSphere`, y toward the north pole), lifted
 * `metres` (already exaggerated) above the sphere. */
export function spherePoint(longitude: number, latitude: number, metres = 0): [number, number, number] {
  const lon = (longitude * Math.PI) / 180;
  const lat = (latitude * Math.PI) / 180;
  const radius = 1 + metres / GLOBE_RADIUS;
  return [Math.sin(lon) * Math.cos(lat) * radius, Math.sin(lat) * radius, Math.cos(lon) * Math.cos(lat) * radius];
}

/** A closed mesh around a latitude/longitude box between the sphere and
 * `top` metres (exaggerated) above it, in unit-sphere space, each vertex
 * with its face's outward normal: the top and the floor subdivided so
 * they follow the curve, and the four walls. Triangles, indexed. */
export function globeShellMesh(
  box: { west: number; east: number; south: number; north: number },
  top: number,
  columns = 48,
  rows = 24,
): { positions: Float32Array; normals: Float32Array; indices: Uint16Array } {
  const positions: number[] = [];
  const normals: number[] = [];
  const indices: number[] = [];
  const lonAt = (i: number) => box.west + ((box.east - box.west) * i) / columns;
  const latAt = (j: number) => box.north + ((box.south - box.north) * j) / rows;
  const push = (point: [number, number, number], normal: [number, number, number]) => {
    positions.push(...point);
    normals.push(...normal);
    return positions.length / 3 - 1;
  };
  const radial = (lon: number, lat: number, sign: number): [number, number, number] =>
    spherePoint(lon, lat).map((value) => value * sign) as [number, number, number];
  // A grid of (u + 1) x (v + 1) vertices from `vertex(i, j)`, two triangles
  // a cell.
  const sheet = (u: number, v: number, vertex: (i: number, j: number) => number) => {
    const at: number[][] = [];
    for (let j = 0; j <= v; j += 1) {
      at.push([]);
      for (let i = 0; i <= u; i += 1) at[j]!.push(vertex(i, j));
    }
    for (let j = 0; j < v; j += 1) {
      for (let i = 0; i < u; i += 1) {
        const a = at[j]![i]!;
        const b = at[j]![i + 1]!;
        const c = at[j + 1]![i]!;
        const d = at[j + 1]![i + 1]!;
        indices.push(a, c, d, a, d, b);
      }
    }
  };
  // Top and floor: radial normals, out and in.
  sheet(columns, rows, (i, j) => push(spherePoint(lonAt(i), latAt(j), top), radial(lonAt(i), latAt(j), 1)));
  sheet(columns, rows, (i, j) => push(spherePoint(lonAt(i), latAt(j), 0), radial(lonAt(i), latAt(j), -1)));
  // East and west walls: the meridian planes, whose normal is the
  // direction of increasing longitude.
  for (const [lon, sign] of [
    [box.east, 1],
    [box.west, -1],
  ] as const) {
    const radians = (lon * Math.PI) / 180;
    const normal: [number, number, number] = [Math.cos(radians) * sign, 0, -Math.sin(radians) * sign];
    sheet(rows, 1, (j, k) => push(spherePoint(lon, latAt(j), k * top), normal));
  }
  // North and south walls: cones of constant latitude, whose normal is the
  // direction of increasing latitude at each longitude.
  for (const [lat, sign] of [
    [box.north, 1],
    [box.south, -1],
  ] as const) {
    const phi = (lat * Math.PI) / 180;
    sheet(columns, 1, (i, k) => {
      const lambda = (lonAt(i) * Math.PI) / 180;
      const normal: [number, number, number] = [
        -Math.sin(lambda) * Math.sin(phi) * sign,
        Math.cos(phi) * sign,
        -Math.cos(lambda) * Math.sin(phi) * sign,
      ];
      return push(spherePoint(lonAt(i), lat, k * top), normal);
    });
  }
  return { positions: Float32Array.from(positions), normals: Float32Array.from(normals), indices: Uint16Array.from(indices) };
}

/** A section's wall on the globe: a strip along the great circle from `a`
 * to `b`, from the sphere to `top` metres (exaggerated) above it, as
 * triangle-strip vertex pairs (ground, top). */
export function globeSectionStrip(a: [number, number], b: [number, number], top: number, segments = 48): Float32Array {
  const p = spherePoint(a[0], a[1]);
  const q = spherePoint(b[0], b[1]);
  const angle = Math.acos(Math.min(1, Math.max(-1, p[0] * q[0] + p[1] * q[1] + p[2] * q[2])));
  const out: number[] = [];
  for (let i = 0; i <= segments; i += 1) {
    const t = i / segments;
    // Spherical interpolation; a straight blend where the ends coincide.
    const [wa, wb] =
      angle < 1e-9 ? [1 - t, t] : [Math.sin((1 - t) * angle) / Math.sin(angle), Math.sin(t * angle) / Math.sin(angle)];
    const point = [0, 1, 2].map((axis) => wa * p[axis]! + wb * q[axis]!);
    const length = Math.hypot(...point);
    const unit = point.map((value) => value / length);
    const lifted = 1 + top / GLOBE_RADIUS;
    out.push(...unit, ...unit.map((value) => value * lifted));
  }
  return Float32Array.from(out);
}

export const VOLUME_VERTEX_SHADER = `#version 300 es
in vec3 a_position;
uniform mat4 u_matrix;
out vec3 v_world;
void main() {
  v_world = a_position;
  gl_Position = u_matrix * vec4(a_position, 1.0);
}
`;

export const VOLUME_FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp sampler3D;
in vec3 v_world;
uniform mat4 u_matrix;
uniform vec3 u_camera;
uniform vec3 u_box_min;
uniform vec3 u_box_max;
// west, north, east − west, north − south, in degrees
uniform vec4 u_grid;
uniform float u_z_scale;
uniform float u_top;
uniform sampler3D u_volume_a;
uniform sampler3D u_volume_b;
uniform float u_mix;
uniform sampler2D u_palette;
uniform sampler2D u_levels;
// offset, scale (value per code), maximum code
uniform vec3 u_codebook;
uniform vec3 u_dbz;
uniform float u_voxel;
// The whole march dimmed while a section stands in it.
uniform float u_veil;
// 0 reflectivity through the palette, 1 cloud water (Beer–Lambert, lit).
uniform float u_cloud;
// σ per g/kg per voxel, the clear threshold in g/kg, the shadow's offset
// and path in voxels.
uniform vec4 u_cloud_shape;
uniform vec3 u_sun;
uniform vec3 u_cloud_lit;
uniform vec3 u_cloud_shade;
// silver strength, lobe power
uniform vec2 u_silver;
// 1 while the terrain's depth is bound: the march stops behind it.
uniform float u_occlude;
${TERRAIN_DEPTH_GLSL}
out vec4 out_color;

const float PI = 3.141592653589793;
const int MAX_STEPS = ${MAX_STEPS};

// The texture coordinate of a point of the box, or a negative x outside it.
vec3 volumeCoordinate(vec3 p) {
  float lon = p.x * 360.0 - 180.0;
  float lat = degrees(atan(sinh(PI * (1.0 - 2.0 * p.y))));
  vec2 uv = vec2((lon - u_grid.x) / u_grid.z, (u_grid.y - lat) / u_grid.w);
  if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0 || p.z < 0.0 || p.z > u_box_max.z) return vec3(-1.0);
  float w = texture(u_levels, vec2(p.z / u_z_scale / u_top, 0.5)).r;
  return vec3(uv, w);
}

float volumeCode(vec3 coordinate) {
  return mix(texture(u_volume_a, coordinate).r, texture(u_volume_b, coordinate).r, u_mix) * 255.0;
}

void main() {
  vec3 ray = v_world - u_camera;
  float hit = length(ray);
  vec3 dir = ray / hit;
  vec3 inverse = 1.0 / dir;
  vec3 t0 = (u_box_min - u_camera) * inverse;
  vec3 t1 = (u_box_max - u_camera) * inverse;
  vec3 near = min(t0, t1);
  vec3 far = max(t0, t1);
  float enter = max(max(near.x, near.y), near.z);
  float leave = min(min(far.x, far.y), far.z);
  // Only the face the ray leaves through draws: the one it enters through
  // would composite the same column twice. The tolerance is a share of a
  // voxel, not of the distance: close in (a 30 km box at zoom 12) the
  // camera is a thousandth of a Mercator unit away, and a relative one
  // falls under float precision and loses the exit face too.
  if (hit < leave - 0.25 * u_voxel) discard;
  enter = max(enter, 0.0);
  float span = leave - enter;
  if (span <= 0.0) discard;
  int steps = int(clamp(ceil(span / u_voxel), 1.0, float(MAX_STEPS)));
  float dt = span / float(steps);
  // Opacity per sample follows the step's length in voxels, so the picture
  // does not change with how many samples a ray takes.
  float stride = dt / u_voxel;
  // Each pixel starts its march at its own phase within the first step.
  // At one fixed phase every ray samples the same shells, and a sampled
  // volume shows them as wood-grain bands; a per-pixel offset (interleaved
  // gradient noise) turns the bands into a grain too fine to see.
  float phase = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
  // The forward-scattering lobe: brightest looking into the sun.
  float lobe = pow(max(dot(dir, u_sun), 0.0), u_silver.y);
  vec4 sum = vec4(0.0);
  for (int i = 0; i < MAX_STEPS; i += 1) {
    if (i >= steps) break;
    vec3 p = u_camera + dir * (enter + (float(i) + phase) * dt);
    // The terrain already drawn is in front of this sample, and of every
    // one after it along the ray.
    if (u_occlude > 0.5 && surfaceOccluded(u_matrix * vec4(p, 1.0))) break;
    vec3 coordinate = volumeCoordinate(p);
    if (coordinate.x < 0.0) continue;
    float code = volumeCode(coordinate);
    if (code > u_codebook.z + 0.5) continue;
    if (u_cloud > 0.5) {
      float q = u_codebook.x + code * u_codebook.y;
      if (q < u_cloud_shape.y) continue;
      float alpha = 1.0 - exp(-u_cloud_shape.x * q * stride);
      // One sample towards the sun: the cloud between this point and the
      // light, weighed as a few voxels of path.
      vec3 towards = volumeCoordinate(p + u_sun * (u_cloud_shape.z * u_voxel));
      float shaded = 0.0;
      if (towards.x >= 0.0) {
        float code_s = volumeCode(towards);
        if (code_s <= u_codebook.z + 0.5) shaded = max(0.0, u_codebook.x + code_s * u_codebook.y);
      }
      float light = exp(-u_cloud_shape.x * shaded * u_cloud_shape.w);
      // A thin edge with clear sky up-sun glows when seen against the sun.
      float edge = light * exp(-u_cloud_shape.x * q);
      vec3 color = mix(u_cloud_shade, u_cloud_lit, light) + vec3(u_silver.x * lobe * edge);
      sum += (1.0 - sum.a) * vec4(min(color, vec3(1.0)) * alpha, alpha);
      if (sum.a > 0.97) break;
      continue;
    }
    float dbz = u_codebook.x + code * u_codebook.y;
    float density = smoothstep(u_dbz.x, u_dbz.y, dbz) * u_dbz.z;
    if (density <= 0.0) continue;
    float alpha = 1.0 - pow(1.0 - min(density, 0.999), stride);
    // Unshaded: the palette alone carries the reflectivity, and depth reads
    // from the tilted camera and from nearer echo occluding farther.
    vec3 color = texture(u_palette, vec2((code + 0.5) / 256.0, 0.5)).rgb;
    sum += (1.0 - sum.a) * vec4(color * alpha, alpha);
    if (sum.a > 0.97) break;
  }
  if (sum.a <= 0.0) discard;
  out_color = sum * u_veil;
}
`;

/** A vertical section: a wall from sea level to the top along a line,
 * painted with the volume's codes where it stands. Echo is opaque in the
 * palette's colours, the rest a pale panel, so the wall reads as a chart
 * standing in the volume. */
export const SECTION_FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp sampler3D;
in vec3 v_world;
uniform vec4 u_grid;
uniform float u_z_scale;
uniform float u_top;
uniform sampler3D u_volume_a;
uniform sampler3D u_volume_b;
uniform float u_mix;
uniform sampler2D u_palette;
uniform sampler2D u_levels;
uniform vec3 u_codebook;
// The value from which the palette paints: dBZ, or g/kg of cloud water.
uniform vec2 u_dbz;
out vec4 out_color;

const float PI = 3.141592653589793;

void main() {
  float lon = v_world.x * 360.0 - 180.0;
  float lat = degrees(atan(sinh(PI * (1.0 - 2.0 * v_world.y))));
  vec2 uv = vec2((lon - u_grid.x) / u_grid.z, (u_grid.y - lat) / u_grid.w);
  if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) discard;
  float w = texture(u_levels, vec2(v_world.z / u_z_scale / u_top, 0.5)).r;
  vec3 coordinate = vec3(uv, w);
  float code = mix(texture(u_volume_a, coordinate).r, texture(u_volume_b, coordinate).r, u_mix) * 255.0;
  float dbz = u_codebook.x + code * u_codebook.y;
  bool echo = code <= u_codebook.z + 0.5 && dbz >= u_dbz.x;
  vec3 color = echo ? texture(u_palette, vec2((code + 0.5) / 256.0, 0.5)).rgb : vec3(0.93);
  float alpha = echo ? 0.95 : 0.5;
  out_color = vec4(color * alpha, alpha);
}
`;

/** The globe's volume: the same march in unit-sphere space, through the
 * shell between the sphere and the top over the box. Only the faces a ray
 * leaves through draw (by their outward normal, not by winding), and a ray
 * stops where it meets the planet. */
export const VOLUME_GLOBE_VERTEX_SHADER = `#version 300 es
in vec3 a_position;
in vec3 a_normal;
uniform mat4 u_matrix;
out vec3 v_world;
out vec3 v_normal;
void main() {
  v_world = a_position;
  v_normal = a_normal;
  gl_Position = u_matrix * vec4(a_position, 1.0);
}
`;

export const VOLUME_GLOBE_FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp sampler3D;
in vec3 v_world;
in vec3 v_normal;
uniform vec3 u_camera;
uniform float u_r_top;
// metres of (unexaggerated) altitude per unit of radius above the sphere
uniform float u_altitude_scale;
uniform vec4 u_grid;
uniform float u_top;
uniform sampler3D u_volume_a;
uniform sampler3D u_volume_b;
uniform float u_mix;
uniform sampler2D u_palette;
uniform sampler2D u_levels;
uniform vec3 u_codebook;
uniform vec3 u_dbz;
uniform float u_voxel;
uniform float u_veil;
out vec4 out_color;

const int MAX_STEPS = ${MAX_STEPS};

void main() {
  vec3 ray = v_world - u_camera;
  float hit = length(ray);
  vec3 dir = ray / hit;
  if (dot(v_normal, dir) <= 0.0) discard;
  float b = dot(u_camera, dir);
  float c = dot(u_camera, u_camera);
  // The planet: a face behind it is drawn by the floor the ray meets first.
  // The floor is the ground itself, but its triangles are chords that sag
  // up to half a kilometre under the sphere, so a ray meets the sphere a
  // little before its floor fragment: the floor is taken on the near half
  // of the ray's way through the planet, every other face only short of
  // the ground by more than a few kilometres.
  float ground = b * b - (c - 1.0);
  if (c > 1.0 && ground > 0.0) {
    float root = sqrt(ground);
    float near = -b - root;
    float far = -b + root;
    bool floor = dot(v_normal, v_world) < -0.99 * length(v_world);
    if (floor ? hit > 0.5 * (near + far) : (near > 0.0 && near < hit - 1e-3)) discard;
  }
  float shell = b * b - (c - u_r_top * u_r_top);
  float enter = c > u_r_top * u_r_top && shell > 0.0 ? -b - sqrt(shell) : 0.0;
  enter = max(enter, 0.0);
  float span = hit - enter;
  if (span <= 0.0) discard;
  int steps = int(clamp(ceil(span / u_voxel), 1.0, float(MAX_STEPS)));
  float dt = span / float(steps);
  float stride = dt / u_voxel;
  // Each pixel starts its march at its own phase within the first step.
  // At one fixed phase every ray samples the same shells, and a sampled
  // volume shows them as wood-grain bands; a per-pixel offset (interleaved
  // gradient noise) turns the bands into a grain too fine to see.
  float phase = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
  vec4 sum = vec4(0.0);
  for (int i = 0; i < MAX_STEPS; i += 1) {
    if (i >= steps) break;
    vec3 p = u_camera + dir * (enter + (float(i) + phase) * dt);
    float r = length(p);
    float lat = degrees(asin(clamp(p.y / r, -1.0, 1.0)));
    float lon = degrees(atan(p.x, p.z));
    vec2 uv = vec2((lon - u_grid.x) / u_grid.z, (u_grid.y - lat) / u_grid.w);
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) continue;
    float altitude = (r - 1.0) * u_altitude_scale;
    if (altitude < 0.0) continue;
    float w = texture(u_levels, vec2(altitude / u_top, 0.5)).r;
    vec3 coordinate = vec3(uv, w);
    float code = mix(texture(u_volume_a, coordinate).r, texture(u_volume_b, coordinate).r, u_mix) * 255.0;
    if (code > u_codebook.z + 0.5) continue;
    float dbz = u_codebook.x + code * u_codebook.y;
    float density = smoothstep(u_dbz.x, u_dbz.y, dbz) * u_dbz.z;
    if (density <= 0.0) continue;
    float alpha = 1.0 - pow(1.0 - min(density, 0.999), stride);
    vec3 color = texture(u_palette, vec2((code + 0.5) / 256.0, 0.5)).rgb;
    sum += (1.0 - sum.a) * vec4(color * alpha, alpha);
    if (sum.a > 0.97) break;
  }
  if (sum.a <= 0.0) discard;
  out_color = sum * u_veil;
}
`;

/** The globe's section wall: positions straight in unit-sphere space. */
export const SECTION_GLOBE_VERTEX_SHADER = `#version 300 es
in vec3 a_position;
uniform mat4 u_matrix;
out vec3 v_world;
void main() {
  v_world = a_position;
  gl_Position = u_matrix * vec4(a_position, 1.0);
}
`;

export const SECTION_GLOBE_FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp sampler3D;
in vec3 v_world;
uniform vec4 u_grid;
uniform float u_altitude_scale;
uniform float u_top;
uniform sampler3D u_volume_a;
uniform sampler3D u_volume_b;
uniform float u_mix;
uniform sampler2D u_palette;
uniform sampler2D u_levels;
uniform vec3 u_codebook;
uniform vec2 u_dbz;
out vec4 out_color;

void main() {
  float r = length(v_world);
  float lat = degrees(asin(clamp(v_world.y / r, -1.0, 1.0)));
  float lon = degrees(atan(v_world.x, v_world.z));
  vec2 uv = vec2((lon - u_grid.x) / u_grid.z, (u_grid.y - lat) / u_grid.w);
  if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) discard;
  float w = texture(u_levels, vec2((r - 1.0) * u_altitude_scale / u_top, 0.5)).r;
  vec3 coordinate = vec3(uv, w);
  float code = mix(texture(u_volume_a, coordinate).r, texture(u_volume_b, coordinate).r, u_mix) * 255.0;
  float dbz = u_codebook.x + code * u_codebook.y;
  bool echo = code <= u_codebook.z + 0.5 && dbz >= u_dbz.x;
  vec3 color = echo ? texture(u_palette, vec2((code + 0.5) / 256.0, 0.5)).rgb : vec3(0.93);
  float alpha = echo ? 0.95 : 0.5;
  out_color = vec4(color * alpha, alpha);
}
`;

/** The veil over the march while a section stands in it. */
const SECTION_VEIL = 0.35;

/** Reflectivity below which a section shows the panel, not the palette. */
const SECTION_ECHO_DBZ = 10;

interface VolumeSlot {
  texture: WebGLTexture;
  planes: readonly Uint8Array[] | null;
}

/** The volume as a MapLibre custom layer. Fed the level planes of two
 * frames and the weight between them, as the field layer is. */
export class VolumeLayer implements CustomLayerInterface {
  readonly id: string;
  readonly type = "custom" as const;
  readonly renderingMode = "3d" as const;

  private map: MapLibreMap | null = null;
  private gl: WebGL2RenderingContext | null = null;
  private program: WebGLProgram | null = null;
  private uniforms = new Map<string, WebGLUniformLocation | null>();
  private boxBuffer: WebGLBuffer | null = null;
  private indexBuffer: WebGLBuffer | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private slots: VolumeSlot[] = [];
  private paletteTexture: WebGLTexture | null = null;
  private levelTexture: WebGLTexture | null = null;
  private grid: VolumeGrid | null = null;
  /** Each plane's dimensions, and the texture's after any halving. */
  private planeSize: [number, number] = [0, 0];
  private textureSize: [number, number] = [0, 0];
  private levelCount = 0;
  private altitudes: number[] = [];
  private codebook: [number, number, number] = [0, 0, 255];
  private palette: Uint8Array | null = null;
  private box: VolumeBox | null = null;
  /** `?vexag=`, or null for the grid's own default. */
  private exaggerationOverride: number | null;
  /** The map's terrain exaggeration the box was last built for (1 without
   * terrain), so a volume over the relief is stretched as the relief is. */
  private terrainExaggeration = 1;
  private transfer: VolumeTransfer = "reflectivity";
  private mixWeight = 0;
  private visible = false;
  private boxDirty = true;
  private levelsDirty = true;
  private storageDirty = true;
  /** The cells a close-up holds, or null for the whole grid. */
  private region: GridRegion | null = null;
  /** A section's two ends, longitude and latitude, or null. */
  private section: [[number, number], [number, number]] | null = null;
  private sectionProgram: WebGLProgram | null = null;
  private sectionUniforms = new Map<string, WebGLUniformLocation | null>();
  private sectionBuffer: WebGLBuffer | null = null;
  private sectionVao: WebGLVertexArrayObject | null = null;
  /** The globe's programs and its shell mesh, built from the box. */
  private globeProgram: WebGLProgram | null = null;
  private globeUniforms = new Map<string, WebGLUniformLocation | null>();
  private globeVao: WebGLVertexArrayObject | null = null;
  private globeBuffers: WebGLBuffer[] = [];
  private globeIndexCount = 0;
  private globeDirty = true;
  private sectionGlobeProgram: WebGLProgram | null = null;
  private sectionGlobeUniforms = new Map<string, WebGLUniformLocation | null>();
  private sectionGlobeVao: WebGLVertexArrayObject | null = null;
  private sectionGlobeBuffer: WebGLBuffer | null = null;

  constructor(
    private readonly onUnsupported: (message: string) => void,
    exaggeration: number | null = null,
    id = "radar-volume",
  ) {
    this.id = id;
    this.exaggerationOverride = exaggeration;
  }

  /** The exaggeration the box is drawn at: the URL's or the grid's own
   * default, times the terrain's while the map has terrain on. */
  private get exaggeration(): number {
    const own = this.exaggerationOverride ?? (this.grid ? defaultVerticalExaggeration(this.grid) : DEFAULT_VERTICAL_EXAGGERATION);
    return own * this.terrainExaggeration;
  }

  private readonly onTerrain = (): void => {
    this.syncTerrain();
    this.map?.triggerRepaint();
  };

  /** Follow the map's terrain exaggeration; the box is rebuilt when it
   * changes. */
  private syncTerrain(): void {
    const terrain = this.map?.getTerrain() ?? null;
    const exaggeration = terrain ? (terrain.exaggeration ?? 1) : 1;
    if (exaggeration === this.terrainExaggeration) return;
    this.terrainExaggeration = exaggeration;
    this.boxDirty = true;
  }

  /** The grid, levels and codebook of the session to draw. */
  configure(grid: VolumeGrid, levels: VolumeLevels): void {
    const quantization = levels.variables[0]!.quantization as LinearQuantization;
    const sameGrid =
      this.grid !== null &&
      this.grid.width === grid.width &&
      this.grid.height === grid.height &&
      this.levelCount === levels.altitudes.length;
    this.grid = { ...grid };
    this.levelCount = levels.altitudes.length;
    this.altitudes = [...levels.altitudes];
    // The field layer reads value = offset + (code / 255) * 255 * scale off
    // a normalized texel; the march works in whole codes, offset + code * scale.
    this.codebook = [quantization.offset, quantization.scale, quantization.maximumCode];
    this.transfer = volumeTransfer(levels.variables[0]!.parameter);
    this.boxDirty = true;
    this.levelsDirty = true;
    if (!sameGrid) {
      this.storageDirty = true;
      for (const slot of this.slots) slot.planes = null;
    }
    this.map?.triggerRepaint();
  }

  setPalette(palette: Uint8Array): void {
    this.palette = palette;
    if (this.gl && this.paletteTexture) this.uploadPalette(this.gl);
    this.map?.triggerRepaint();
  }

  /** `?vexag=`, or null for the grid's own default. */
  setExaggeration(exaggeration: number | null): void {
    if (exaggeration === this.exaggerationOverride) return;
    this.exaggerationOverride = exaggeration;
    this.boxDirty = true;
    this.map?.triggerRepaint();
  }

  setVisible(visible: boolean): void {
    if (visible === this.visible) return;
    this.visible = visible;
    this.map?.triggerRepaint();
  }

  /** Hold only `region`'s cells (the tiles a close-up decodes), or the
   * whole grid. The textures, the box and the march all shrink to it. */
  setRegion(region: GridRegion | null): void {
    const same =
      region === this.region ||
      (region !== null &&
        this.region !== null &&
        region.x === this.region.x &&
        region.y === this.region.y &&
        region.width === this.region.width &&
        region.height === this.region.height);
    if (same) return;
    this.region = region ? { ...region } : null;
    this.storageDirty = true;
    this.boxDirty = true;
    for (const slot of this.slots) slot.planes = null;
    this.map?.triggerRepaint();
  }

  /** A vertical section between two points, longitude and latitude, or
   * none. */
  setSection(section: [[number, number], [number, number]] | null): void {
    this.section = section;
    this.map?.triggerRepaint();
  }

  setFrame(planes: readonly Uint8Array[]): void {
    this.setBlend(planes, null, 0);
  }

  /** Two frames' level planes and how far between them the picture is. */
  setBlend(a: readonly Uint8Array[], b: readonly Uint8Array[] | null, mix: number): void {
    const gl = this.gl;
    if (!gl || this.slots.length < 2) return;
    // A step forward makes the frame in the second slot the first: swap the
    // textures rather than upload a volume that is already on the GPU.
    if (this.slots[1]!.planes === a || (this.slots[1]!.planes && samePlanes(this.slots[1]!.planes, a))) {
      this.slots.reverse();
    }
    this.ensureStorage(gl);
    this.upload(gl, this.slots[0]!, a);
    if (b) this.upload(gl, this.slots[1]!, b);
    this.mixWeight = b ? Math.min(1, Math.max(0, mix)) : 0;
    this.map?.triggerRepaint();
  }

  onAdd(map: MapLibreMap, context: WebGLRenderingContext | WebGL2RenderingContext): void {
    if (!(typeof WebGL2RenderingContext !== "undefined" && context instanceof WebGL2RenderingContext)) {
      this.onUnsupported("The radar volume needs WebGL2.");
      return;
    }
    const gl = context;
    this.map = map;
    this.gl = gl;
    map.on("terrain", this.onTerrain);
    this.syncTerrain();
    this.program = linkProgram(gl, VOLUME_VERTEX_SHADER, VOLUME_FRAGMENT_SHADER);
    if (!this.program) {
      this.onUnsupported("The radar volume shader failed to compile.");
      return;
    }
    for (const name of [
      "u_matrix", "u_camera", "u_box_min", "u_box_max", "u_grid", "u_z_scale", "u_top",
      "u_volume_a", "u_volume_b", "u_mix", "u_palette", "u_levels", "u_codebook", "u_dbz", "u_voxel", "u_veil",
      "u_cloud", "u_cloud_shape", "u_sun", "u_cloud_lit", "u_cloud_shade", "u_silver", "u_occlude", "u_depth",
    ]) {
      this.uniforms.set(name, gl.getUniformLocation(this.program, name));
    }
    this.vao = gl.createVertexArray();
    this.boxBuffer = gl.createBuffer();
    this.indexBuffer = gl.createBuffer();
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.boxBuffer);
    const location = gl.getAttribLocation(this.program, "a_position");
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexBuffer);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, BOX_INDICES, gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    this.sectionProgram = linkProgram(gl, VOLUME_VERTEX_SHADER, SECTION_FRAGMENT_SHADER);
    if (this.sectionProgram) {
      for (const name of [
        "u_matrix", "u_grid", "u_z_scale", "u_top", "u_volume_a", "u_volume_b", "u_mix",
        "u_palette", "u_levels", "u_codebook", "u_dbz",
      ]) {
        this.sectionUniforms.set(name, gl.getUniformLocation(this.sectionProgram, name));
      }
      this.sectionVao = gl.createVertexArray();
      this.sectionBuffer = gl.createBuffer();
      gl.bindVertexArray(this.sectionVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.sectionBuffer);
      const sectionLocation = gl.getAttribLocation(this.sectionProgram, "a_position");
      gl.enableVertexAttribArray(sectionLocation);
      gl.vertexAttribPointer(sectionLocation, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
    }
    this.globeProgram = linkProgram(gl, VOLUME_GLOBE_VERTEX_SHADER, VOLUME_GLOBE_FRAGMENT_SHADER);
    if (this.globeProgram) {
      for (const name of [
        "u_matrix", "u_camera", "u_r_top", "u_altitude_scale", "u_grid", "u_top", "u_volume_a", "u_volume_b",
        "u_mix", "u_palette", "u_levels", "u_codebook", "u_dbz", "u_voxel", "u_veil",
      ]) {
        this.globeUniforms.set(name, gl.getUniformLocation(this.globeProgram, name));
      }
      this.globeVao = gl.createVertexArray();
      this.globeBuffers = [gl.createBuffer()!, gl.createBuffer()!, gl.createBuffer()!];
      gl.bindVertexArray(this.globeVao);
      for (const [index, name] of ["a_position", "a_normal"].entries()) {
        gl.bindBuffer(gl.ARRAY_BUFFER, this.globeBuffers[index]!);
        const at = gl.getAttribLocation(this.globeProgram, name);
        gl.enableVertexAttribArray(at);
        gl.vertexAttribPointer(at, 3, gl.FLOAT, false, 0, 0);
      }
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.globeBuffers[2]!);
      gl.bindVertexArray(null);
    }
    this.sectionGlobeProgram = linkProgram(gl, SECTION_GLOBE_VERTEX_SHADER, SECTION_GLOBE_FRAGMENT_SHADER);
    if (this.sectionGlobeProgram) {
      for (const name of [
        "u_matrix", "u_grid", "u_altitude_scale", "u_top", "u_volume_a", "u_volume_b", "u_mix",
        "u_palette", "u_levels", "u_codebook", "u_dbz",
      ]) {
        this.sectionGlobeUniforms.set(name, gl.getUniformLocation(this.sectionGlobeProgram, name));
      }
      this.sectionGlobeVao = gl.createVertexArray();
      this.sectionGlobeBuffer = gl.createBuffer();
      gl.bindVertexArray(this.sectionGlobeVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.sectionGlobeBuffer);
      const at = gl.getAttribLocation(this.sectionGlobeProgram, "a_position");
      gl.enableVertexAttribArray(at);
      gl.vertexAttribPointer(at, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
    }
    this.globeDirty = true;
    this.slots = [0, 1].map(() => ({ texture: gl.createTexture()!, planes: null }));
    this.paletteTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    setLinearClamp(gl, gl.TEXTURE_2D);
    if (this.palette) this.uploadPalette(gl);
    this.levelTexture = gl.createTexture();
    this.storageDirty = true;
    this.levelsDirty = true;
    this.boxDirty = true;
  }

  onRemove(): void {
    const gl = this.gl;
    if (gl) {
      for (const slot of this.slots) gl.deleteTexture(slot.texture);
      gl.deleteTexture(this.paletteTexture);
      gl.deleteTexture(this.levelTexture);
      gl.deleteBuffer(this.boxBuffer);
      gl.deleteBuffer(this.indexBuffer);
      gl.deleteVertexArray(this.vao);
      gl.deleteProgram(this.program);
      gl.deleteProgram(this.sectionProgram);
      gl.deleteProgram(this.globeProgram);
      gl.deleteProgram(this.sectionGlobeProgram);
      for (const buffer of this.globeBuffers) gl.deleteBuffer(buffer);
      gl.deleteBuffer(this.sectionGlobeBuffer);
      gl.deleteVertexArray(this.globeVao);
      gl.deleteVertexArray(this.sectionGlobeVao);
      gl.deleteBuffer(this.sectionBuffer);
      gl.deleteVertexArray(this.sectionVao);
    }
    this.map?.off("terrain", this.onTerrain);
    this.slots = [];
    this.gl = null;
    this.map = null;
  }

  render(context: WebGLRenderingContext | WebGL2RenderingContext, input: CustomRenderMethodInput): void {
    const gl = context as WebGL2RenderingContext;
    if (!this.visible || !this.program || !this.grid || !this.slots[0]?.planes || !this.palette) return;
    if (isGlobe(input)) {
      this.renderGlobe(gl, input);
      return;
    }
    const matrix = input.defaultProjectionData.mainMatrix as ArrayLike<number>;
    const camera = cameraFromMatrix(matrix);
    if (!camera) return;
    this.syncTerrain();
    if (this.boxDirty) this.rebuildBox(gl);
    if (this.levelsDirty) this.uploadLevels(gl);
    const box = this.box!;
    const cloud = this.transfer === "cloud";
    const depth = this.map?.getTerrain() ? terrainDepthTexture(this.map) : null;

    gl.useProgram(this.program);
    gl.uniformMatrix4fv(this.uniforms.get("u_matrix")!, false, Float32Array.from(matrix));
    gl.uniform3f(this.uniforms.get("u_camera")!, camera[0], camera[1], camera[2]);
    gl.uniform3fv(this.uniforms.get("u_box_min")!, box.min);
    gl.uniform3fv(this.uniforms.get("u_box_max")!, box.max);
    gl.uniform4f(this.uniforms.get("u_grid")!, box.west, box.north, box.east - box.west, box.north - box.south);
    gl.uniform1f(this.uniforms.get("u_z_scale")!, box.zScale);
    gl.uniform1f(this.uniforms.get("u_top")!, box.top);
    gl.uniform1f(this.uniforms.get("u_mix")!, this.slots[1]!.planes ? this.mixWeight : 0);
    gl.uniform3f(this.uniforms.get("u_codebook")!, ...this.codebook);
    gl.uniform3f(this.uniforms.get("u_dbz")!, CLEAR_DBZ, DENSE_DBZ, VOXEL_OPACITY);
    // One texel of the texture as it is held, in Mercator units across.
    gl.uniform1f(this.uniforms.get("u_voxel")!, (box.max[0] - box.min[0]) / this.textureSize[0]);
    gl.uniform1f(this.uniforms.get("u_veil")!, this.section ? SECTION_VEIL : 1);
    gl.uniform1f(this.uniforms.get("u_cloud")!, cloud ? 1 : 0);
    gl.uniform4f(this.uniforms.get("u_cloud_shape")!, CLOUD_SIGMA, CLOUD_CLEAR_GPKG, SHADOW_OFFSET_VOXELS, SHADOW_PATH_VOXELS);
    gl.uniform3fv(this.uniforms.get("u_sun")!, SUN_DIRECTION);
    // Read at draw time: the theme switches in place.
    const colours = isDark ? CLOUD_COLOURS.dark : CLOUD_COLOURS.light;
    gl.uniform3fv(this.uniforms.get("u_cloud_lit")!, colours.lit);
    gl.uniform3fv(this.uniforms.get("u_cloud_shade")!, colours.shade);
    gl.uniform2f(this.uniforms.get("u_silver")!, SILVER_STRENGTH, SILVER_POWER);
    gl.uniform1f(this.uniforms.get("u_occlude")!, depth ? 1 : 0);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_3D, this.slots[0]!.texture);
    gl.uniform1i(this.uniforms.get("u_volume_a")!, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_3D, (this.slots[1]!.planes ? this.slots[1]! : this.slots[0]!).texture);
    gl.uniform1i(this.uniforms.get("u_volume_b")!, 1);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.uniform1i(this.uniforms.get("u_palette")!, 2);
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.levelTexture);
    gl.uniform1i(this.uniforms.get("u_levels")!, 3);
    // The terrain's depth on its own unit; without terrain the unit still
    // holds a 2D texture (the level lookup), since a sampler2D left on a
    // unit holding the 3D volume would fail the draw.
    gl.activeTexture(gl.TEXTURE4);
    gl.bindTexture(gl.TEXTURE_2D, depth ?? this.levelTexture);
    gl.uniform1i(this.uniforms.get("u_depth")!, 4);

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.depthMask(false);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this.vao);
    gl.drawElements(gl.TRIANGLES, BOX_INDICES.length, gl.UNSIGNED_SHORT, 0);
    gl.bindVertexArray(null);
    if (this.section) this.renderSection(gl, matrix, box);
    gl.activeTexture(gl.TEXTURE0);
  }

  /** The section wall, over the march with the textures still bound. */
  private renderSection(gl: WebGL2RenderingContext, matrix: ArrayLike<number>, box: VolumeBox): void {
    if (!this.section || !this.sectionProgram || !this.sectionVao) return;
    const [a, b] = this.section.map((point) => MercatorCoordinate.fromLngLat(point));
    const top = box.max[2];
    const wall = new Float32Array([a!.x, a!.y, 0, b!.x, b!.y, 0, a!.x, a!.y, top, b!.x, b!.y, top]);
    gl.useProgram(this.sectionProgram);
    const uniform = (name: string) => this.sectionUniforms.get(name)!;
    gl.uniformMatrix4fv(uniform("u_matrix"), false, Float32Array.from(matrix));
    gl.uniform4f(uniform("u_grid"), box.west, box.north, box.east - box.west, box.north - box.south);
    gl.uniform1f(uniform("u_z_scale"), box.zScale);
    gl.uniform1f(uniform("u_top"), box.top);
    gl.uniform1f(uniform("u_mix"), this.slots[1]!.planes ? this.mixWeight : 0);
    gl.uniform3f(uniform("u_codebook"), ...this.codebook);
    gl.uniform2f(uniform("u_dbz"), this.sectionThreshold(), DENSE_DBZ);
    gl.uniform1i(uniform("u_volume_a"), 0);
    gl.uniform1i(uniform("u_volume_b"), 1);
    gl.uniform1i(uniform("u_palette"), 2);
    gl.uniform1i(uniform("u_levels"), 3);
    gl.bindVertexArray(this.sectionVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.sectionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, wall, gl.DYNAMIC_DRAW);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.bindVertexArray(null);
  }

  /** The globe: the march through the shell over the box in unit-sphere
   * space, then the section, if any, as a wall along its great circle.
   * Nothing while the projection is between globe and plane. */
  private renderGlobe(gl: WebGL2RenderingContext, input: CustomRenderMethodInput): void {
    const data = input.defaultProjectionData;
    if (!this.globeProgram || !this.globeVao || data.projectionTransition < 0.999) return;
    const matrix = data.mainMatrix as ArrayLike<number>;
    const camera = cameraFromMatrix(matrix);
    if (!camera) return;
    if (this.boxDirty) this.rebuildBox(gl);
    if (this.levelsDirty) this.uploadLevels(gl);
    const box = this.box!;
    // Metres of the box's height above the sphere, exaggerated.
    const lift = box.top * this.exaggeration;
    if (this.globeDirty) {
      const mesh = globeShellMesh(box, lift);
      gl.bindVertexArray(this.globeVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.globeBuffers[0]!);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.globeBuffers[1]!);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.normals, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.globeBuffers[2]!);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.STATIC_DRAW);
      gl.bindVertexArray(null);
      this.globeIndexCount = mesh.indices.length;
      this.globeDirty = false;
    }
    const uniform = (name: string) => this.globeUniforms.get(name)!;
    const grid = [box.west, box.north, box.east - box.west, box.north - box.south] as const;
    gl.useProgram(this.globeProgram);
    gl.uniformMatrix4fv(uniform("u_matrix"), false, Float32Array.from(matrix));
    gl.uniform3f(uniform("u_camera"), camera[0], camera[1], camera[2]);
    gl.uniform1f(uniform("u_r_top"), 1 + lift / GLOBE_RADIUS);
    gl.uniform1f(uniform("u_altitude_scale"), GLOBE_RADIUS / this.exaggeration);
    gl.uniform4f(uniform("u_grid"), ...grid);
    gl.uniform1f(uniform("u_top"), box.top);
    gl.uniform1f(uniform("u_mix"), this.slots[1]!.planes ? this.mixWeight : 0);
    gl.uniform3f(uniform("u_codebook"), ...this.codebook);
    gl.uniform3f(uniform("u_dbz"), CLEAR_DBZ, DENSE_DBZ, VOXEL_OPACITY);
    // One texel across, in radians of arc: the sphere's own unit.
    gl.uniform1f(uniform("u_voxel"), (((box.east - box.west) / this.textureSize[0]) * Math.PI) / 180);
    gl.uniform1f(uniform("u_veil"), this.section ? SECTION_VEIL : 1);
    this.bindVolumeTextures(gl, uniform);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.depthMask(false);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this.globeVao);
    gl.drawElements(gl.TRIANGLES, this.globeIndexCount, gl.UNSIGNED_SHORT, 0);
    gl.bindVertexArray(null);

    if (this.section && this.sectionGlobeProgram && this.sectionGlobeVao) {
      const wall = globeSectionStrip(this.section[0], this.section[1], lift);
      const sectionUniform = (name: string) => this.sectionGlobeUniforms.get(name)!;
      gl.useProgram(this.sectionGlobeProgram);
      gl.uniformMatrix4fv(sectionUniform("u_matrix"), false, Float32Array.from(matrix));
      gl.uniform4f(sectionUniform("u_grid"), ...grid);
      gl.uniform1f(sectionUniform("u_altitude_scale"), GLOBE_RADIUS / this.exaggeration);
      gl.uniform1f(sectionUniform("u_top"), box.top);
      gl.uniform1f(sectionUniform("u_mix"), this.slots[1]!.planes ? this.mixWeight : 0);
      gl.uniform3f(sectionUniform("u_codebook"), ...this.codebook);
      gl.uniform2f(sectionUniform("u_dbz"), this.sectionThreshold(), DENSE_DBZ);
      this.bindVolumeTextures(gl, sectionUniform);
      gl.bindVertexArray(this.sectionGlobeVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.sectionGlobeBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, wall, gl.DYNAMIC_DRAW);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, wall.length / 3);
      gl.bindVertexArray(null);
    }
    gl.activeTexture(gl.TEXTURE0);
  }

  /** The value from which a section paints the palette rather than the
   * panel: an echo in dBZ, or cloud in g/kg. */
  private sectionThreshold(): number {
    return this.transfer === "cloud" ? SECTION_CLOUD_GPKG : SECTION_ECHO_DBZ;
  }

  /** The two frames' volumes, the palette and the level lookup on units
   * 0–3, for whichever program `uniform` belongs to. */
  private bindVolumeTextures(gl: WebGL2RenderingContext, uniform: (name: string) => WebGLUniformLocation | null): void {
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_3D, this.slots[0]!.texture);
    gl.uniform1i(uniform("u_volume_a"), 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_3D, (this.slots[1]!.planes ? this.slots[1]! : this.slots[0]!).texture);
    gl.uniform1i(uniform("u_volume_b"), 1);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.uniform1i(uniform("u_palette"), 2);
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.levelTexture);
    gl.uniform1i(uniform("u_levels"), 3);
  }

  private rebuildBox(gl: WebGL2RenderingContext): void {
    if (!this.grid || this.altitudes.length === 0) return;
    const grid = this.region ? regionGrid(this.grid, this.region) : this.grid;
    this.box = volumeBox(grid, volumeTop(this.altitudes), this.exaggeration);
    this.globeDirty = true;
    const [x0, y0, z0] = this.box.min;
    const [x1, y1, z1] = this.box.max;
    const corners = new Float32Array([
      x0, y0, z0, x1, y0, z0, x1, y1, z0, x0, y1, z0,
      x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1,
    ]);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.boxBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, corners, gl.STATIC_DRAW);
    this.boxDirty = false;
  }

  private uploadLevels(gl: WebGL2RenderingContext): void {
    if (this.altitudes.length === 0 || !this.levelTexture) return;
    const table = levelLookup(this.altitudes, volumeTop(this.altitudes));
    gl.bindTexture(gl.TEXTURE_2D, this.levelTexture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    withPlainUnpack(gl, () => gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, table.length, 1, 0, gl.RED, gl.FLOAT, table));
    setLinearClamp(gl, gl.TEXTURE_2D);
    this.levelsDirty = false;
  }

  private uploadPalette(gl: WebGL2RenderingContext): void {
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    withPlainUnpack(gl, () => gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 1, gl.RGBA, gl.UNSIGNED_BYTE, this.palette));
  }

  /** (Re)allocate both 3D textures for the grid, halving a dimension the
   * device cannot hold in one 3D texture. */
  private ensureStorage(gl: WebGL2RenderingContext): void {
    if (!this.storageDirty || !this.grid) return;
    const limit = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE) as number;
    this.planeSize = [this.grid.width, this.grid.height];
    // A close-up holds its own cells at full detail; one past the device's
    // limit falls back to the whole grid, halved as that is.
    if (this.region && (this.region.width > limit || this.region.height > limit)) {
      this.region = null;
      this.boxDirty = true;
    }
    let [width, height] = this.region ? [this.region.width, this.region.height] : this.planeSize;
    while ((width > limit || height > limit) && width > 1) {
      width = Math.ceil(width / 2);
      height = Math.ceil(height / 2);
    }
    this.textureSize = [width, height];
    for (const slot of this.slots) {
      gl.deleteTexture(slot.texture);
      slot.texture = gl.createTexture()!;
      gl.bindTexture(gl.TEXTURE_3D, slot.texture);
      gl.texStorage3D(gl.TEXTURE_3D, 1, gl.R8, width, height, this.levelCount);
      setLinearClamp(gl, gl.TEXTURE_3D);
      slot.planes = null;
    }
    this.storageDirty = false;
  }

  private upload(gl: WebGL2RenderingContext, slot: VolumeSlot, planes: readonly Uint8Array[]): void {
    if (slot.planes && samePlanes(slot.planes, planes)) return;
    withPlainUnpack(gl, () => this.uploadPlanes(gl, slot, planes));
  }

  private uploadPlanes(gl: WebGL2RenderingContext, slot: VolumeSlot, planes: readonly Uint8Array[]): void {
    gl.bindTexture(gl.TEXTURE_3D, slot.texture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    const [width, height] = this.textureSize;
    const region = this.region;
    if (region) {
      // Only the region's cells: the rest of a close-up's planes are stale.
      // A 3D upload takes its rows inside an image UNPACK_IMAGE_HEIGHT tall
      // (by default the upload's own height), so skipping rows needs the
      // plane's whole height there, or WebGL refuses the upload.
      gl.pixelStorei(gl.UNPACK_ROW_LENGTH, this.planeSize[0]);
      gl.pixelStorei(gl.UNPACK_IMAGE_HEIGHT, this.planeSize[1]);
      gl.pixelStorei(gl.UNPACK_SKIP_PIXELS, region.x);
      gl.pixelStorei(gl.UNPACK_SKIP_ROWS, region.y);
      for (let level = 0; level < Math.min(planes.length, this.levelCount); level += 1) {
        gl.texSubImage3D(gl.TEXTURE_3D, 0, 0, 0, level, width, height, 1, gl.RED, gl.UNSIGNED_BYTE, planes[level]!);
      }
      gl.pixelStorei(gl.UNPACK_ROW_LENGTH, 0);
      gl.pixelStorei(gl.UNPACK_IMAGE_HEIGHT, 0);
      gl.pixelStorei(gl.UNPACK_SKIP_PIXELS, 0);
      gl.pixelStorei(gl.UNPACK_SKIP_ROWS, 0);
      slot.planes = planes;
      return;
    }
    for (let level = 0; level < Math.min(planes.length, this.levelCount); level += 1) {
      let plane = planes[level]!;
      let [w, h] = this.planeSize;
      while (w > width) {
        plane = maxPool2(plane, w, h);
        w = Math.ceil(w / 2);
        h = Math.ceil(h / 2);
      }
      gl.texSubImage3D(gl.TEXTURE_3D, 0, 0, 0, level, width, height, 1, gl.RED, gl.UNSIGNED_BYTE, plane);
    }
    slot.planes = planes;
  }
}

/** Run an upload from typed arrays with the unpack flags MapLibre leaves
 * behind for its images turned off: a 3D upload from an array view with
 * premultiplied alpha or a flipped Y is an error in WebGL2 (the volume came
 * out empty once the basemap had uploaded its sprites and peak badges), and
 * a 2D one would be premultiplied or flipped. MapLibre caches that state,
 * so it is put back as it was. */
function withPlainUnpack(gl: WebGL2RenderingContext, upload: () => void): void {
  const flip = gl.getParameter(gl.UNPACK_FLIP_Y_WEBGL) as boolean;
  const premultiply = gl.getParameter(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL) as boolean;
  if (flip) gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
  if (premultiply) gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
  upload();
  if (flip) gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
  if (premultiply) gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
}

function samePlanes(a: readonly Uint8Array[], b: readonly Uint8Array[]): boolean {
  return a.length === b.length && a.every((plane, index) => plane === b[index]);
}

// The box's twelve triangles, corners 0–3 the bottom and 4–7 the top.
const BOX_INDICES = new Uint16Array([
  0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6,
  0, 4, 5, 0, 5, 1, 1, 5, 6, 1, 6, 2,
  2, 6, 7, 2, 7, 3, 3, 7, 4, 3, 4, 0,
]);

function setLinearClamp(gl: WebGL2RenderingContext, target: number): void {
  gl.texParameteri(target, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(target, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(target, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(target, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  if (target === gl.TEXTURE_3D) gl.texParameteri(target, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
}

function linkProgram(gl: WebGL2RenderingContext, vertex: string, fragment: string): WebGLProgram | null {
  const compile = (type: number, source: string): WebGLShader | null => {
    const shader = gl.createShader(type);
    if (!shader) return null;
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.error(gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  };
  const vs = compile(gl.VERTEX_SHADER, vertex);
  const fs = compile(gl.FRAGMENT_SHADER, fragment);
  if (!vs || !fs) return null;
  const program = gl.createProgram();
  if (!program) return null;
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.error(gl.getProgramInfoLog(program));
    gl.deleteProgram(program);
    return null;
  }
  return program;
}
