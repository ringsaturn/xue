import type { CustomRenderMethodInput, Map as MaplibreMap, ProjectionSpecification } from "maplibre-gl";

/**
 * Where the custom layers put a point of the world on screen: on a flat
 * Web Mercator plane, on the globe, or on either draped over the map's 3D
 * terrain.
 *
 * MapLibre hands a custom layer the projection as GLSL (`projectTile`,
 * `projectTileFor3D`) plus the uniforms that drive it, and both change with
 * the projection, so a layer's vertex stage is compiled per projection
 * variant and cached by `variantName`. Every layer here addresses the world
 * in Mercator units, [0, 1] across and down, and the default projection data
 * is built for exactly that: `projectTile(mercator)` works unchanged on the
 * plane and on the sphere.
 *
 * Terrain is the map's own: the raster-dem source the hillshade already
 * draws, turned on with `map.setTerrain`. A custom layer is never draped by
 * MapLibre (only the raster-like layers render to the terrain's textures),
 * so a layer that should lie on the ground draws itself once per terrain
 * tile, with that tile's DEM texture and the same bilinear elevation lookup
 * MapLibre's own terrain mesh uses — so the field sits on the very surface
 * the basemap is drawn on, and the depth the terrain left behind hides what
 * a ridge hides. `Map.terrain` and its tile manager are typed but internal,
 * so they are read through the narrow structural types below and every
 * missing piece falls back to the flat path rather than throwing.
 */

/** Tile-local units a projection matrix for one tile expects. */
export const TILE_EXTENT = 8192;

const EARTH_CIRCUMFERENCE_M = 40075016.7;

/** The projection uniforms MapLibre's prelude declares. */
export const PROJECTION_UNIFORM_NAMES = [
  "u_projection_matrix",
  "u_projection_fallback_matrix",
  "u_projection_tile_mercator_coords",
  "u_projection_clipping_plane",
  "u_projection_transition",
] as const;

/** The terrain uniforms the surface vertex and fragment stages declare. */
export const TERRAIN_UNIFORM_NAMES = [
  "u_tile",
  "u_terrain",
  "u_terrain_dim",
  "u_terrain_matrix",
  "u_terrain_unpack",
  "u_terrain_exaggeration",
  "u_dem_texel_meters",
  "u_depth",
  "u_mesh_size",
] as const;

/** Elevation in metres (times the exaggeration) at a tile-local position,
 * MapLibre's `get_elevation` under names of our own: the DEM is a RGBA8
 * texture with a one-texel border, and the unpack vector turns its bytes back
 * into metres whatever the encoding (Terrarium or Mapbox). */
export const TERRAIN_VERTEX_GLSL = `
uniform highp sampler2D u_terrain;
uniform float u_terrain_dim;
uniform mat4 u_terrain_matrix;
uniform vec4 u_terrain_unpack;
uniform float u_terrain_exaggeration;
float surfaceTexel(ivec2 at) {
  vec4 rgb = (texelFetch(u_terrain, at, 0) * 255.0) * u_terrain_unpack;
  return rgb.r + rgb.g + rgb.b - u_terrain_unpack.a;
}
// DEM texel coordinates of a tile-local position (0..8192).
vec2 surfaceDemCoord(vec2 tilePosition) {
  return (u_terrain_matrix * vec4(tilePosition, 0.0, 1.0)).xy * u_terrain_dim + 1.0;
}
float surfaceElevation(vec2 coord);
// The height the terrain's own mesh has at a tile-local position: the DEM at
// the mesh's vertices, interpolated across the triangle the point falls in
// (split along the same diagonal as the mesh). A point lifted this way lies
// on the very surface the terrain's depth was drawn from, so a depth test
// against it is about ridges, not about the mesh's facets.
uniform float u_mesh_size;
float surfaceMeshElevation(vec2 tilePosition) {
  vec2 cell = tilePosition / 8192.0 * u_mesh_size;
  vec2 corner = floor(cell);
  vec2 f = cell - corner;
  float spacing = 8192.0 / u_mesh_size;
  float ea = surfaceElevation(surfaceDemCoord(corner * spacing));
  float eb = surfaceElevation(surfaceDemCoord((corner + vec2(1.0, 0.0)) * spacing));
  float ec = surfaceElevation(surfaceDemCoord((corner + vec2(0.0, 1.0)) * spacing));
  float ed = surfaceElevation(surfaceDemCoord((corner + vec2(1.0, 1.0)) * spacing));
  return f.x >= f.y
    ? ea + (eb - ea) * f.x + (ed - eb) * f.y
    : ea + (ec - ea) * f.y + (ed - ec) * f.x;
}
float surfaceElevation(vec2 coord) {
  vec2 f = fract(coord);
  ivec2 c = ivec2(floor(coord));
  ivec2 hi = textureSize(u_terrain, 0) - 1;
  float tl = surfaceTexel(clamp(c, ivec2(0), hi));
  float tr = surfaceTexel(clamp(c + ivec2(1, 0), ivec2(0), hi));
  float bl = surfaceTexel(clamp(c + ivec2(0, 1), ivec2(0), hi));
  float br = surfaceTexel(clamp(c + ivec2(1, 1), ivec2(0), hi));
  return mix(mix(tl, tr, f.x), mix(bl, br, f.x), f.y) * u_terrain_exaggeration;
}
`;

/** The terrain's depth, packed into RGBA by MapLibre's depth pass: whether a
 * clip-space position is behind the terrain surface already drawn. The
 * same test MapLibre's symbols fade behind hills with. */
export const TERRAIN_DEPTH_GLSL = `
uniform highp sampler2D u_depth;
bool surfaceOccluded(vec4 clip) {
  vec3 frag = clip.xyz / clip.w;
  highp float stored = dot(texture(u_depth, frag.xy * 0.5 + 0.5), vec4(1.0) / vec4(256.0 * 256.0 * 256.0, 256.0 * 256.0, 256.0, 1.0));
  return stored + 0.0001 - frag.z < -0.002;
}
`;

/** Relief shading for a fragment on the terrain: the slope under it, read
 * from the same DEM, lit from the north-west the way a printed relief map
 * is — or, while terrain shadows are on, from the sun (`u_light`, east,
 * north, up; left unset it is the north-west light). 1.0 is a flat
 * surface.
 *
 * The DEM is unpacked texel by texel and interpolated in metres, never
 * through the sampler's own filtering: the encodings are linear in their
 * bytes, but a filtered UNORM8 comes back at fp16-like precision on common
 * GPUs, and an ulp of Terrarium's red byte is about 32 m. Wherever the
 * ground crosses a multiple of 256 m — where neighbouring texels differ in
 * red — the slope then picks up that noise, and the shade draws it as
 * rippled bands along the contour. The texel convention is the vertex
 * stage's: an integer coordinate is a texel's center. */
export const TERRAIN_SHADE_GLSL = `
uniform highp sampler2D u_terrain;
uniform vec4 u_terrain_unpack;
uniform float u_terrain_exaggeration;
uniform float u_dem_texel_meters;
uniform vec3 u_light;
float surfaceShadeTexel(ivec2 at) {
  ivec2 hi = textureSize(u_terrain, 0) - 1;
  vec4 rgb = (texelFetch(u_terrain, clamp(at, ivec2(0), hi), 0) * 255.0) * u_terrain_unpack;
  return rgb.r + rgb.g + rgb.b - u_terrain_unpack.a;
}
float surfaceSample(vec2 coord) {
  vec2 f = fract(coord);
  ivec2 c = ivec2(floor(coord));
  float tl = surfaceShadeTexel(c);
  float tr = surfaceShadeTexel(c + ivec2(1, 0));
  float bl = surfaceShadeTexel(c + ivec2(0, 1));
  float br = surfaceShadeTexel(c + ivec2(1, 1));
  return mix(mix(tl, tr, f.x), mix(bl, br, f.x), f.y);
}
float surfaceShade(vec2 coord) {
  // The stencil stays inside the texture: within a texel of its edge a
  // central difference would read one clamped sample, halve the slope and
  // draw the DEM tile's edge as a pale seam across the relief.
  vec2 at = clamp(coord, vec2(1.0), vec2(textureSize(u_terrain, 0)) - 2.0);
  float east = surfaceSample(at + vec2(1.0, 0.0)) - surfaceSample(at - vec2(1.0, 0.0));
  float south = surfaceSample(at + vec2(0.0, 1.0)) - surfaceSample(at - vec2(0.0, 1.0));
  float run = 2.0 * u_dem_texel_meters / max(u_terrain_exaggeration, 1.0);
  // (east, north, up): a texel row runs south.
  vec3 normal = normalize(vec3(-east / run, south / run, 1.0));
  vec3 light = normalize(dot(u_light, u_light) > 0.0 ? u_light : vec3(-1.0, 1.0, 1.4));
  float lambert = max(dot(normal, light), 0.0);
  return clamp(0.42 + 0.78 * lambert / dot(vec3(0.0, 0.0, 1.0), light), 0.35, 1.25);
}
`;

/** Insert `#define`s after the `#version` line a shader opens with. */
export function withDefines(source: string, defines: readonly string[]): string {
  if (!defines.length) return source;
  const newline = source.indexOf("\n");
  return `${source.slice(0, newline + 1)}${defines.map((name) => `#define ${name}\n`).join("")}${source.slice(newline + 1)}`;
}

/** The vertex prelude for this frame's projection, with its defines. */
export function projectionPrelude(args: CustomRenderMethodInput): string {
  return `${args.shaderData.vertexShaderPrelude}\n${args.shaderData.define}\n`;
}

export function isGlobe(args: CustomRenderMethodInput): boolean {
  return args.shaderData.define.includes("GLOBE");
}

type ProjectionDataLike = {
  mainMatrix: ArrayLike<number>;
  fallbackMatrix: ArrayLike<number>;
  tileMercatorCoords: ArrayLike<number>;
  clippingPlane: ArrayLike<number>;
  projectionTransition: number;
};

function f32(values: ArrayLike<number>): Float32Array {
  return values instanceof Float32Array ? values : new Float32Array(Array.from(values));
}

export function setProjectionUniforms(
  gl: WebGL2RenderingContext,
  uniforms: Record<string, WebGLUniformLocation | null>,
  data: ProjectionDataLike,
): void {
  if (uniforms.u_projection_matrix) gl.uniformMatrix4fv(uniforms.u_projection_matrix, false, f32(data.mainMatrix));
  if (uniforms.u_projection_fallback_matrix) {
    gl.uniformMatrix4fv(uniforms.u_projection_fallback_matrix, false, f32(data.fallbackMatrix));
  }
  const tile = data.tileMercatorCoords;
  if (uniforms.u_projection_tile_mercator_coords) {
    gl.uniform4f(uniforms.u_projection_tile_mercator_coords, tile[0] ?? 0, tile[1] ?? 0, tile[2] ?? 1, tile[3] ?? 1);
  }
  const plane = data.clippingPlane;
  if (uniforms.u_projection_clipping_plane) {
    gl.uniform4f(uniforms.u_projection_clipping_plane, plane[0] ?? 0, plane[1] ?? 0, plane[2] ?? 0, plane[3] ?? 0);
  }
  if (uniforms.u_projection_transition) gl.uniform1f(uniforms.u_projection_transition, data.projectionTransition);
}

/** One terrain tile a layer draws itself over. */
export interface SurfaceTile {
  /** The tile's Mercator origin (wrap included) and its side. */
  x: number;
  y: number;
  size: number;
  projection: ProjectionDataLike;
  dem: WebGLTexture;
  depth: WebGLTexture;
  demDim: number;
  demMatrix: ArrayLike<number>;
  demUnpack: ArrayLike<number>;
  exaggeration: number;
  /** Ground metres one DEM texel spans, at the tile's middle latitude. */
  demTexelMeters: number;
  /** Cells per side of the terrain's mesh over this tile. */
  meshSize: number;
}

interface TileIdLike {
  wrap: number;
  canonical: { x: number; y: number; z: number };
}
interface TerrainLike {
  tileManager?: { getRenderableTiles?: () => Array<{ tileID: TileIdLike }> };
  getTerrainData?: (tileID: TileIdLike) => {
    u_terrain_dim: number;
    u_terrain_matrix: ArrayLike<number>;
    u_terrain_unpack: ArrayLike<number>;
    u_terrain_exaggeration: number;
    texture: WebGLTexture;
    depthTexture: WebGLTexture;
    tile?: { tileID?: TileIdLike; dem?: { dim?: number } } | null;
  };
}

/** The terrain tiles on screen, each with what drawing over it takes, or
 * null when the map has no terrain (or MapLibre no longer exposes it the way
 * this reads it — the layer then stays flat). */
export function surfaceTiles(map: MaplibreMap, args: CustomRenderMethodInput): SurfaceTile[] | null {
  const terrain = (map as unknown as { terrain?: TerrainLike | null }).terrain;
  const renderable = terrain?.tileManager?.getRenderableTiles?.();
  if (!terrain?.getTerrainData || !renderable) return null;
  const globe = isGlobe(args);
  const meshSize = terrainMeshSize(map);
  const tiles: SurfaceTile[] = [];
  for (const { tileID } of renderable) {
    const { x, y, z } = tileID.canonical;
    const count = 2 ** z;
    const data = terrain.getTerrainData(tileID);
    const projection = args.getProjectionData({
      tileID: { wrap: tileID.wrap, canonical: { x, y, z } },
      applyGlobeMatrix: globe,
    }) as unknown as ProjectionDataLike;
    const sourceZ = data.tile?.tileID?.canonical.z ?? z;
    const middle = (y + 0.5) / count;
    const latitude = Math.atan(Math.sinh(Math.PI * (1 - 2 * middle)));
    tiles.push({
      x: x / count + tileID.wrap,
      y: y / count,
      size: 1 / count,
      projection,
      dem: data.texture,
      depth: data.depthTexture,
      demDim: data.u_terrain_dim,
      demMatrix: data.u_terrain_matrix,
      demUnpack: data.u_terrain_unpack,
      exaggeration: data.u_terrain_exaggeration,
      meshSize,
      demTexelMeters: (EARTH_CIRCUMFERENCE_M * Math.cos(latitude)) / 2 ** sourceZ / Math.max(1, data.u_terrain_dim),
    });
  }
  return tiles;
}

/** Bind one terrain tile's projection and DEM. The DEM goes on `demUnit`
 * (read through `linearSampler` when given, so the texture's own NEAREST —
 * which MapLibre relies on — is left alone), the terrain depth on
 * `depthUnit` when the program reads it. */
export function bindSurfaceTile(
  gl: WebGL2RenderingContext,
  uniforms: Record<string, WebGLUniformLocation | null>,
  tile: SurfaceTile,
  demUnit: number,
  linearSampler: WebGLSampler | null,
  depthUnit: number | null,
): void {
  setProjectionUniforms(gl, uniforms, tile.projection);
  if (uniforms.u_tile) gl.uniform4f(uniforms.u_tile, tile.x, tile.y, tile.size, 0);
  gl.activeTexture(gl.TEXTURE0 + demUnit);
  gl.bindTexture(gl.TEXTURE_2D, tile.dem);
  gl.bindSampler(demUnit, linearSampler);
  if (uniforms.u_terrain) gl.uniform1i(uniforms.u_terrain, demUnit);
  if (uniforms.u_terrain_dim) gl.uniform1f(uniforms.u_terrain_dim, tile.demDim);
  if (uniforms.u_terrain_matrix) gl.uniformMatrix4fv(uniforms.u_terrain_matrix, false, f32(tile.demMatrix));
  const unpack = tile.demUnpack;
  if (uniforms.u_terrain_unpack) {
    gl.uniform4f(uniforms.u_terrain_unpack, unpack[0] ?? 0, unpack[1] ?? 0, unpack[2] ?? 0, unpack[3] ?? 0);
  }
  if (uniforms.u_terrain_exaggeration) gl.uniform1f(uniforms.u_terrain_exaggeration, tile.exaggeration);
  if (uniforms.u_dem_texel_meters) gl.uniform1f(uniforms.u_dem_texel_meters, tile.demTexelMeters);
  if (uniforms.u_mesh_size) gl.uniform1f(uniforms.u_mesh_size, tile.meshSize);
  if (depthUnit !== null && uniforms.u_depth) {
    gl.activeTexture(gl.TEXTURE0 + depthUnit);
    gl.bindTexture(gl.TEXTURE_2D, tile.depth);
    gl.uniform1i(uniforms.u_depth, depthUnit);
  }
}

/** A linear sampler for reading a DEM smoothly without touching the
 * texture's own filter state. */
export function createLinearSampler(gl: WebGL2RenderingContext): WebGLSampler | null {
  const sampler = gl.createSampler();
  gl.samplerParameteri(sampler, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.samplerParameteri(sampler, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.samplerParameteri(sampler, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.samplerParameteri(sampler, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return sampler;
}

/** A grid of `columns` x `rows` cells over the unit square, as two
 * triangles per cell. `rowAt` places row `j` (0..rows) on the vertical
 * axis, which lets the world mesh space its rows by latitude. */
export function gridMesh(
  columns: number,
  rows: number,
  rowAt: (row: number) => number = (row) => row / rows,
): { positions: Float32Array; indices: Uint32Array } {
  const positions = new Float32Array((columns + 1) * (rows + 1) * 2);
  for (let row = 0; row <= rows; row += 1) {
    const y = rowAt(row);
    for (let column = 0; column <= columns; column += 1) {
      const at = (row * (columns + 1) + column) * 2;
      positions[at] = column / columns;
      positions[at + 1] = y;
    }
  }
  const indices = new Uint32Array(columns * rows * 6);
  let at = 0;
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const a = row * (columns + 1) + column;
      const b = a + 1;
      const c = a + columns + 1;
      const d = c + 1;
      // The diagonal MapLibre's terrain mesh splits a cell along, so a
      // tile mesh and the terrain's are the same triangles.
      indices.set([a, c, d, a, d, b], at);
      at += 6;
    }
  }
  return { positions, indices };
}

/** Positions at or past this mark a skirt vertex: the edge vertex it hangs
 * from, plus `SKIRT_OFFSET` on both axes. */
export const SKIRT_OFFSET = 10;

/** A tile mesh with skirts: `gridMesh` plus a wall hanging down from each
 * edge, so where a tile meets a neighbour of another level of detail the
 * gap between their edges shows the field's own colour rather than the
 * basemap under it — what MapLibre's terrain mesh does for the same seam. */
export function skirtedTileMesh(size: number): { positions: Float32Array; indices: Uint32Array } {
  const grid = gridMesh(size, size);
  const edge: number[] = [];
  for (let column = 0; column < size; column += 1) edge.push(column);
  for (let row = 0; row < size; row += 1) edge.push(row * (size + 1) + size);
  for (let column = size; column > 0; column -= 1) edge.push(size * (size + 1) + column);
  for (let row = size; row > 0; row -= 1) edge.push(row * (size + 1));
  const base = grid.positions.length / 2;
  const positions = new Float32Array(grid.positions.length + edge.length * 2);
  positions.set(grid.positions);
  edge.forEach((vertex, at) => {
    positions[(base + at) * 2] = grid.positions[vertex * 2]! + SKIRT_OFFSET;
    positions[(base + at) * 2 + 1] = grid.positions[vertex * 2 + 1]! + SKIRT_OFFSET;
  });
  const indices = new Uint32Array(grid.indices.length + edge.length * 6);
  indices.set(grid.indices);
  let at = grid.indices.length;
  for (let index = 0; index < edge.length; index += 1) {
    const next = (index + 1) % edge.length;
    const a = edge[index]!;
    const b = edge[next]!;
    const c = base + index;
    const d = base + next;
    indices.set([a, c, b, b, c, d], at);
    at += 6;
  }
  return { positions, indices };
}

/** Mercator y of a latitude, unclamped: the globe's polar caps lie past
 * [0, 1], where the plane never shows and the sphere still does. */
export function mercatorYUnclamped(latitude: number): number {
  const phi = (latitude * Math.PI) / 180;
  return 0.5 - Math.log(Math.tan(Math.PI / 4 + phi / 2)) / (2 * Math.PI);
}

/** Rows of the world mesh, spaced evenly in latitude from pole to pole (a
 * hair short of each, where Mercator y is infinite), so the sphere stays
 * round at the poles and the plane is not over-tessellated at its edges. */
export function worldRowAt(rows: number): (row: number) => number {
  const limit = 89.9;
  return (row) => mercatorYUnclamped(limit - (2 * limit * row) / rows);
}

/** Cells per side of the terrain's own tile mesh, which a layer draping
 * itself over a tile matches. */
export function terrainMeshSize(map: MaplibreMap): number {
  const size = (map as unknown as { terrain?: { meshSize?: number } | null }).terrain?.meshSize;
  return typeof size === "number" && size > 0 ? size : 128;
}

/** The zoom at which the globe has handed over to the plane when the relief is on. */
const GLOBE_HANDOVER_WITH_RELIEF = 10;

/**
 * The globe projection to set: MapLibre's own, which hands over to the
 * plane between z11 and z12, or with the 3D relief on one that hands over
 * between z9 and z10. The globe's controls ignore terrain (the ground
 * grabbed does not follow the cursor, the center never lands), and relief
 * matters only where the plane takes over anyway.
 */
export function globeProjection(terrain: boolean): ProjectionSpecification {
  if (!terrain) return { type: "globe" };
  return { type: ["interpolate", ["linear"], ["zoom"], GLOBE_HANDOVER_WITH_RELIEF - 1, "vertical-perspective", GLOBE_HANDOVER_WITH_RELIEF, "mercator"] };
}

/** Whether the map shows the globe at low zoom, under either handover. */
export function isGlobeProjection(map: MaplibreMap): boolean {
  return (map.getProjection()?.type ?? "mercator") !== "mercator";
}

/** A MapLibre `LngLat`, reached through the transform's own center so no runtime import is needed. */
interface LngLatLike {
  lng: number;
  lat: number;
}
type LngLatClass = new (lng: number, lat: number) => LngLatLike;

/** What the terrain camera fix below reads from a transform. */
interface TransformReadout {
  center: LngLatLike;
  elevation: number;
  zoom: number;
  pitch: number;
  bearing: number;
  fovInRadians: number;
  height: number;
  cameraToCenterDistance: number;
  worldSize: number;
  tileSize: number;
  minZoom: number;
  maxZoom: number;
  tileZoom: number;
  centerPoint: unknown;
}

/** The transform internals the terrain camera fix below reaches. */
interface TransformInternals extends TransformReadout {
  /** Globe transform only: true while the globe, not its Mercator half, renders. */
  isGlobeRendering?: boolean;
  /** Globe transform only. */
  _mercatorTransform?: unknown;
  apply(source: unknown, forceOverrideZ: boolean): void;
  recalculateZoomAndCenter(terrain?: TerrainReadout): void;
  screenPointToLocation(point: unknown, terrain?: TerrainReadout): LngLatLike;
  setCenter(center: LngLatLike): void;
  setZoom(zoom: number): void;
  setElevation(elevation: number): void;
}

interface TerrainReadout {
  getElevationForLngLatZoom(lnglat: LngLatLike, zoom: number): number;
}

interface CameraInternals {
  transform?: TransformInternals;
  /** The copy a gesture accumulates into; stale once the rendered transform moves on its own. */
  _requestedCameraState?: TransformInternals;
  elevationFreeze?: boolean;
  isEasing(): boolean;
}

const CAMERA_FIX = Symbol("xue.terrainCameraFix");

/** A center this close to the ground is on it. */
const SETTLED_ELEVATION_M = 1;
/** Idle steps after which the center is left where it is (ground steeper than the ray). */
const MAX_IDLE_STEPS = 8;
/** Kept clear of the zoom limits so the constrained `setZoom` never moves the camera. */
const ZOOM_MARGIN = 0.01;

const mercatorUnitsPerMeter = (lat: number): number => 1 / (EARTH_CIRCUMFERENCE_M * Math.cos((lat * Math.PI) / 180));
const mercatorX = (lng: number): number => (180 + lng) / 360;
const mercatorY = (lat: number): number => (180 - (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360))) / 360;
const lngFromMercatorX = (x: number): number => x * 360 - 180;
const latFromMercatorY = (y: number): number => (360 / Math.PI) * Math.atan(Math.exp(((180 - y * 360) * Math.PI) / 180)) - 90;

/**
 * Move the center along the view ray onto the plane at `elevation`, the
 * camera where it is: MapLibre's `recalculateZoomAndCenter`, which does
 * the same in pixel space, without its two ways of moving the camera after
 * all. The zoom is the distance to the center, so a plane nearer than the
 * zoom ceiling allows (ground rising toward a camera that looks almost
 * level) leaves the transform as it is, the center floating off the
 * ground, and a plane farther than the floor allows stops there; MapLibre
 * parks the center 10 km out past 84° of pitch and lets the zoom clamp
 * pull the camera the rest of the way. A plane above the camera has no
 * still solution: the view is into the mountain, so the camera is lifted
 * with the center, as MapLibre does. Returns whether the center moved.
 */
export function moveCenterOnRay(transform: TransformReadout, elevation: number, setElevation: (elevation: number) => void, setCenter: (center: LngLatLike) => void, setZoom: (zoom: number) => void): boolean {
  const { center, worldSize } = transform;
  const unitsPerPixel = 1 / worldSize;
  const unitsPerMeter = mercatorUnitsPerMeter(center.lat);
  const pixelsPerMeter = unitsPerMeter * worldSize;
  // The camera, in pixels: behind the center along the bearing, above it by the pitch.
  const pitch = (transform.pitch * Math.PI) / 180;
  const bearing = (transform.bearing * Math.PI) / 180;
  const dirX = Math.sin(pitch) * Math.sin(bearing);
  const dirY = -Math.sin(pitch) * Math.cos(bearing);
  const cosPitch = Math.cos(pitch);
  const distancePixels = transform.cameraToCenterDistance;
  const cameraX = mercatorX(center.lng) / unitsPerPixel - distancePixels * dirX;
  const cameraY = mercatorY(center.lat) / unitsPerPixel - distancePixels * dirY;
  const cameraAltitude = transform.elevation + (distancePixels * cosPitch) / pixelsPerMeter;
  // Distance to the new center, in metres along the ray, held inside the zoom limits.
  const zoomScale = transform.height / 2 / Math.tan(transform.fovInRadians / 2) / transform.tileSize;
  const nearest = zoomScale / (2 ** (transform.maxZoom - ZOOM_MARGIN) * unitsPerMeter);
  const farthest = zoomScale / (2 ** (transform.minZoom + ZOOM_MARGIN) * unitsPerMeter);
  const wanted = (cameraAltitude - elevation) / cosPitch;
  if (wanted <= 0) {
    setElevation(elevation);
    return false;
  }
  if (wanted < nearest) return false;
  const distance = Math.min(farthest, wanted);
  const x = cameraX + dirX * distance * pixelsPerMeter;
  const y = cameraY + dirY * distance * pixelsPerMeter;
  const LngLat = center.constructor as LngLatClass;
  const next = new LngLat(lngFromMercatorX(x * unitsPerPixel), latFromMercatorY(y * unitsPerPixel));
  const zoom = Math.log2(zoomScale / (distance * mercatorUnitsPerMeter(next.lat)));
  setElevation(cameraAltitude - distance * cosPitch);
  setCenter(next);
  setZoom(Math.min(transform.maxZoom, Math.max(transform.minZoom, zoom)));
  return true;
}

/**
 * Keep the camera still over the 3D relief. MapLibre clamps the center to
 * the ground: a gesture ends by moving the center onto the terrain under
 * the screen center with the camera where it is, and whenever the ground
 * under the center then turns out elsewhere, the camera moves with the
 * center (`setElevation`: same center, same zoom, new height), which the
 * viewer sees as the whole scene shifting. That happens right after every
 * gesture, since the gesture lands on the terrain *mesh* and the next frame
 * re-samples the DEM there (metres to tens of metres apart on rough
 * ground), after an inertia glide for the same reason, and whenever a
 * sharper DEM tile lands under the center. Landing itself moves the camera
 * too whenever the ground point is closer than the zoom ceiling allows,
 * which a camera looking almost level reaches on any rise of the ground
 * ahead, and past 84° of pitch MapLibre gives up on the ray altogether.
 *
 * Here both go through `moveCenterOnRay`: the center slides along the view
 * ray onto the new elevation when the zoom limits leave room, the camera
 * never moving. An idle re-sample is a fixed-point walk that converges on
 * ground gentler than the ray and is invisible at every step; a repeated
 * request is one the ray cannot meet, and a walk that keeps finding new
 * ground stops after a few steps with the center floating. Eases, terrain
 * toggles and the globe's own rendering keep MapLibre's behaviour: an ease
 * animates the camera anyway, a toggle wants the camera lifted, and the
 * globe ignores terrain. The globe transform past its Mercator zoom also
 * hands a terrain-less landing to its Mercator half and never copies the
 * result back, as its own `setLocationAtPoint` does; that copy is here
 * too. Applied to the prototype of whichever transform the map has, once
 * each.
 */
export function keepTerrainCameraStill(map: MaplibreMap): void {
  const internals = map as unknown as { _camera?: CameraInternals; terrain?: TerrainReadout | null };
  let toggling = false;
  const setTerrain = map.setTerrain.bind(map);
  map.setTerrain = (options) => {
    toggling = true;
    try {
      return setTerrain(options);
    } finally {
      toggling = false;
    }
  };
  // The idle walk's state: steps in a row, and the ground last asked for.
  let steps = 0;
  let lastRequest = NaN;
  const patch = (): void => {
    const transform = internals._camera?.transform;
    if (!transform) return;
    const prototype = Object.getPrototypeOf(transform) as TransformInternals & { [CAMERA_FIX]?: true };
    if (prototype[CAMERA_FIX] || typeof prototype.setElevation !== "function") return;
    const setElevation = prototype.setElevation;
    const slide = (target: TransformInternals, elevation: number): boolean =>
      moveCenterOnRay(
        target,
        elevation,
        (value) => setElevation.call(target, value),
        (center) => target.setCenter(center),
        (zoom) => target.setZoom(zoom),
      );
    prototype.setElevation = function (this: TransformInternals, elevation: number): void {
      const camera = internals._camera;
      const idle =
        camera?.transform === this &&
        !!internals.terrain &&
        !toggling &&
        !camera.elevationFreeze &&
        !camera.isEasing() &&
        !this.isGlobeRendering;
      if (!idle) {
        steps = 0;
        lastRequest = NaN;
        setElevation.call(this, elevation);
        return;
      }
      if (Math.abs(elevation - lastRequest) < SETTLED_ELEVATION_M) return;
      lastRequest = elevation;
      if (Math.abs(elevation - this.elevation) < SETTLED_ELEVATION_M) return;
      if (++steps > MAX_IDLE_STEPS) return;
      slide(this, elevation);
      // A gesture's copy outlives the gesture; left behind, its camera would
      // snap back on the next gesture's first frame.
      camera._requestedCameraState?.apply(this, false);
    };
    const recalculate = prototype.recalculateZoomAndCenter;
    const globe = "_mercatorTransform" in transform && typeof prototype.apply === "function";
    prototype.recalculateZoomAndCenter = function (this: TransformInternals, terrain?: TerrainReadout): void {
      if (!terrain || this.isGlobeRendering) {
        recalculate.call(this, terrain);
        if (globe && !this.isGlobeRendering) this.apply(this._mercatorTransform, false);
        return;
      }
      // The ground under the screen center, as MapLibre samples it.
      const ground = terrain.getElevationForLngLatZoom(this.screenPointToLocation(this.centerPoint, terrain), this.tileZoom);
      slide(this, ground);
      steps = 0;
      lastRequest = ground;
    };
    prototype[CAMERA_FIX] = true;
  };
  patch();
  map.on("projectiontransition", patch);
}
