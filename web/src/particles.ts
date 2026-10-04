import type { CustomLayerInterface, CustomRenderMethodInput, Map as MaplibreMap } from "maplibre-gl";

import { DOMAIN_GLSL, DOMAIN_UNIFORM_NAMES, setDomainUniforms, type LambertDomain } from "./domain";
import { t } from "./i18n";
import {
  bindSurfaceTile,
  isGlobe,
  projectionPrelude,
  PROJECTION_UNIFORM_NAMES,
  setProjectionUniforms,
  surfaceTiles,
  TERRAIN_DEPTH_GLSL,
  TERRAIN_UNIFORM_NAMES,
  TERRAIN_VERTEX_GLSL,
  withDefines,
} from "./projection";
import type { BundleMetadata, LinearQuantization } from "./manifest";
import { WIND_COMPONENT_IDS, type BundleVariable, type DataVariableId } from "./manifest";
import { mercatorY } from "./mercator";
import { buildWindSpeedPalette, WIND_SPEED_MAX } from "./palettes";

/**
 * MapLibre custom layer that advects GPU particles through the 10 m wind
 * field. The u/v quantized code planes are packed into
 * the R/G channels of one texture (the industry-wide convention), particle
 * positions live in two ping-pong RGBA8 state textures (16 bits per axis,
 * webgl-wind encoding), and trails come from ping-pong screen textures faded
 * a little every frame. Particles are colored through a 256x1 speed palette,
 * or — the everyday case since the scalar layer began drawing the speed
 * field underneath — in one ink set by `setInk`: two speed ramps stacked
 * read as mud, and up here the particles are carrying direction and pace,
 * not a value.
 *
 * The parameter set (count / speed factor / fade / drop a.k.a. reset rate /
 * speed color ramp) and its defaults follow the conventions shared by
 * webgl-wind, Windy and earth.nullschool.
 */

interface WindParticleOptions {
  /** Number of particles simulated (rounded up to a square texture). */
  count: number;
  /** Time-lapse multiplier applied to real wind speed: a particle in 10 m/s
   * wind crosses the globe in about 46 days of real time, so playback runs
   * tens of thousands of times faster to read as flow. */
  speedFactor: number;
  /** Per-frame trail retention; lower fades trails faster. */
  fadeOpacity: number;
  /** Base probability per frame that a particle respawns somewhere random. */
  dropRate: number;
  /** Extra respawn probability scaled by particle speed, so fast particles
   * do not all pile up along jet streaks. */
  dropRateBump: number;
  /** Overall layer opacity when composited onto the map. */
  opacity: number;
}

const WIND_PARTICLE_DEFAULTS: WindParticleOptions = {
  // Sparse: over the colored speed field the particles are a trace of
  // direction, and any denser their trails haze the field over.
  count: 16384,
  speedFactor: 55000,
  fadeOpacity: 0.955,
  dropRate: 0.003,
  dropRateBump: 0.01,
  opacity: 0.85,
};

const EARTH_CIRCUMFERENCE_M = 40075016.7;

/** The zoom the speed factor is tuned at. Past it a particle's ground speed
 * halves with every zoom level, so it keeps its pace on screen: an
 * unscaled 10 m/s particle at a valley's zoom crosses the screen in a
 * fraction of a second and reads as scattered dots. */
const PACE_REFERENCE_ZOOM = 4;
export function zoomPace(zoom: number): number {
  return 2 ** -Math.max(0, zoom - PACE_REFERENCE_ZOOM);
}

/** From this zoom on (plane only) particles are seeded on the ground the
 * screen shows and respawned when they leave it. */
const SCREEN_SEEDING_ZOOM = 5;
/** Points per side of the screen lattice seeding interpolates within. */
const SEED_LATTICE = 9;

/** Where the point pass reads its particle index from, in every variant. */
const INDEX_ATTRIBUTE = 0;
/** Units 0..2 carry the state, wind and palette; the terrain rides above. */
const DEM_TEXTURE_UNIT = 3;
const DEPTH_TEXTURE_UNIT = 4;

const QUAD_VERTEX_SHADER = `#version 300 es
in vec2 a_pos;
out vec2 v_tex_pos;
void main() {
  v_tex_pos = a_pos;
  gl_Position = vec4(2.0 * a_pos - 1.0, 0.0, 1.0);
}`;

/** Copies the previous trail with a floored exponential fade, so every texel
 * provably reaches zero instead of asymptoting at a dim ghost value. */
const FADE_FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in vec2 v_tex_pos;
uniform sampler2D u_screen;
uniform float u_fade;
out vec4 out_color;
void main() {
  vec4 color = texture(u_screen, v_tex_pos) * u_fade;
  out_color = floor(color * 255.0) / 255.0;
}`;

const SCREEN_FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in vec2 v_tex_pos;
uniform sampler2D u_screen;
uniform float u_opacity;
out vec4 out_color;
void main() {
  out_color = texture(u_screen, v_tex_pos) * u_opacity;
}`;

/** Shared helpers: decode a particle position from the RGBA8 state texture
 * and sample the RG wind texture (codes) in m/s at a world position. */
const WIND_SAMPLING = `
const float PI = 3.141592653589793;

vec2 decodePosition(vec4 color) {
#ifdef XUE_FLOAT_STATE
  return color.rg;
#else
  return vec2(color.r / 255.0 + color.b, color.g / 255.0 + color.a);
#endif
}

float latitudeOf(vec2 pos) {
  return 90.0 - (360.0 / PI) * atan(exp((pos.y * 2.0 - 1.0) * PI));
}

// Grid coordinates of a world position. The longitude offset wraps into
// [0, 360) so a global grid indexes from -180 and a regional
// window that crosses the antimeridian stays contiguous.
vec2 gridUv(vec2 pos) {
  float longitude = fract(pos.x) * 360.0 - 180.0;
  float latitude = latitudeOf(pos);
  return vec2(
    (mod(longitude - u_first.x, 360.0) / u_step.x + 0.5) / u_size.x,
    ((latitude - u_first.y) / u_step.y + 0.5) / u_size.y
  );
}

${DOMAIN_GLSL}

// A regional grid (a showcase case) has no wind outside its window, and a
// regional model none outside its own footprint (domain.ts); a particle
// that drifts out has to be respawned rather than pushed by the clamped
// edge texel forever.
bool inGrid(vec2 pos) {
  vec2 uv = gridUv(pos);
  if (uv.y < 0.0 || uv.y > 1.0) return false;
  if (u_wrap < 0.5 && (uv.x < 0.0 || uv.x > 1.0)) return false;
  return !outsideDomain(fract(pos.x) * 360.0 - 180.0, latitudeOf(pos));
}

vec2 windAt(vec2 pos) {
  vec2 code = texture(u_wind, gridUv(pos)).rg;
  return u_wind_offset + code * 255.0 * u_wind_scale;
}`;

const UPDATE_FRAGMENT_SHADER = `#version 300 es
precision highp float;
in vec2 v_tex_pos;
uniform sampler2D u_particles;
uniform sampler2D u_wind;
uniform vec2 u_wind_offset;
uniform vec2 u_wind_scale;
uniform vec2 u_first;
uniform vec2 u_step;
uniform vec2 u_size;
uniform float u_wrap;
// Respawn rectangle in world Mercator units: (x, y, width, height) of the
// grid's own footprint, so particles are seeded where there is data.
uniform vec4 u_spawn;
// Screen-space seeding, on (1) once the view is a small part of the world:
// u_seeds is the ground under a 9x9 lattice of screen points, as Mercator
// (x unwrapped) over the clip w, and 1 / w — a homogeneous point, so a
// spot interpolated across a lattice cell is spread evenly over the cell's
// pixels rather than over its ground, most of which, near the horizon, is
// a sliver of screen; a point with 1 / w <= 0 lies on the sky. A particle is
// respawned at a random spot in a random cell, and one that leaves the
// screen (u_forward, at the height u_plane) is respawned — so the whole
// budget stays in view at an even density per pixel instead of thinning out
// as the camera closes in.
uniform float u_screen_spawn;
uniform vec3 u_seeds[${SEED_LATTICE * SEED_LATTICE}];
uniform mat4 u_forward;
uniform float u_plane;
// 1 for the one frame after the camera settles: every particle is reseeded
// onto the lattice at once, rather than over the minute the drop rate takes.
uniform float u_reseed;
uniform float u_rand_seed;
uniform float u_speed_factor;
uniform float u_elapsed;
uniform float u_drop_rate;
uniform float u_drop_rate_bump;
uniform float u_max_speed;
out vec4 out_color;
${WIND_SAMPLING}

const vec3 rand_constants = vec3(12.9898, 78.233, 4375.85453);
float rand(const vec2 co) {
  float t = dot(rand_constants.xy, co);
  return fract(sin(t) * (rand_constants.z + t));
}

bool onScreen(vec2 pos) {
  for (int copy = -1; copy <= 1; copy += 1) {
    vec4 clip = u_forward * vec4(pos.x + float(copy), pos.y, u_plane, 1.0);
    if (clip.w > 0.0 && all(lessThanEqual(abs(clip.xy / clip.w), vec2(1.05)))) return true;
  }
  return false;
}

// A random spot on the ground the screen shows, or false when the cells
// tried lie on the sky or off the data (a few tries, then the caller falls
// back to the grid).
bool screenSpawn(vec2 seed, out vec2 result) {
  const int side = ${SEED_LATTICE};
  for (int attempt = 0; attempt < 4; attempt += 1) {
    vec2 pick = vec2(rand(seed + float(attempt) * 3.7 + 0.3), rand(seed + float(attempt) * 5.3 + 0.7)) * float(side - 1);
    ivec2 cell = min(ivec2(floor(pick)), ivec2(side - 2));
    vec2 f = pick - vec2(cell);
    int at = cell.y * side + cell.x;
    vec3 a = u_seeds[at];
    vec3 b = u_seeds[at + 1];
    vec3 c = u_seeds[at + side];
    vec3 d = u_seeds[at + side + 1];
    if (min(min(a.z, b.z), min(c.z, d.z)) <= 0.0) continue;
    vec3 homogeneous = mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
    vec2 ground = homogeneous.xy / homogeneous.z;
    ground.x = fract(ground.x);
    if (ground.y < 0.0 || ground.y > 1.0 || !inGrid(ground)) continue;
    result = ground;
    return true;
  }
  return false;
}

void main() {
  vec2 pos = decodePosition(texture(u_particles, v_tex_pos));
  vec2 wind = windAt(pos);
  float speed_t = clamp(length(wind) / u_max_speed, 0.0, 1.0);

  // Mercator is conformal, so one local ground meter spans the same world
  // units in x and y: 1 / (circumference * cos(latitude)). Positive v blows
  // northward, which is decreasing world y.
  float distortion = max(cos(radians(latitudeOf(pos))), 0.05);
  vec2 offset = vec2(wind.x, -wind.y) * (u_speed_factor * u_elapsed / (${EARTH_CIRCUMFERENCE_M.toFixed(1)} * distortion));
  pos = vec2(fract(pos.x + offset.x + 1.0), clamp(pos.y + offset.y, 0.0, 1.0));

  // Randomly respawn: a base rate plus a speed-scaled bump, and always once
  // the particle has left the grid.
  vec2 seed = (pos + v_tex_pos) * u_rand_seed;
  float drop = max(
    step(1.0 - u_drop_rate - speed_t * u_drop_rate_bump, rand(seed)),
    inGrid(pos) && (u_screen_spawn < 0.5 || (onScreen(pos) && u_reseed < 0.5)) ? 0.0 : 1.0
  );
  vec2 random_pos = vec2(
    fract(u_spawn.x + rand(seed + 1.3) * u_spawn.z),
    u_spawn.y + rand(seed + 2.1) * u_spawn.w
  );
  if (drop > 0.5 && u_screen_spawn > 0.5) {
    vec2 ground;
    if (screenSpawn(seed + 4.1, ground)) random_pos = ground;
  }
  pos = mix(pos, random_pos, drop);

#ifdef XUE_FLOAT_STATE
  out_color = vec4(pos, 0.0, 1.0);
#else
  out_color = vec4(fract(pos * 255.0), floor(pos * 255.0) / 255.0);
#endif
}`;

// Through MapLibre's projection prelude, so particles fly over the plane or
// the globe alike. With XUE_TERRAIN the pass is drawn once per terrain tile
// (projection.ts): a particle outside the tile is sent off clip space, one
// inside is lifted onto the tile's DEM, and one the terrain's depth says is
// behind a ridge reads as calm, which the fragment stage leaves undrawn.
// u_tile is a world copy's offset in the flat pass and the tile's Mercator
// origin and side on the terrain.
// MapLibre's prelude declares PI already.
const PI_DECLARATION = /const float PI\s*=[^;]*;/;
function drawVertexShader(prelude: string): string {
  return `#version 300 es
precision highp float;
${prelude}
in float a_index;
uniform sampler2D u_particles;
uniform sampler2D u_wind;
uniform vec2 u_wind_offset;
uniform vec2 u_wind_scale;
uniform vec2 u_first;
uniform vec2 u_step;
uniform vec2 u_size;
uniform float u_wrap;
uniform float u_particles_res;
uniform float u_point_size;
uniform vec4 u_tile;
uniform float u_max_speed;
out float v_speed_t;
${WIND_SAMPLING.replace(PI_DECLARATION, "")}
#ifdef XUE_TERRAIN
${TERRAIN_VERTEX_GLSL}
${TERRAIN_DEPTH_GLSL}
#endif

void main() {
  vec2 lookup = vec2(
    fract(a_index / u_particles_res) + 0.5 / u_particles_res,
    floor(a_index / u_particles_res) / u_particles_res + 0.5 / u_particles_res
  );
  vec2 pos = decodePosition(texture(u_particles, lookup));
  // A particle waiting to be respawned off the model's footprint reads as
  // calm, which the fragment stage leaves undrawn.
  v_speed_t = inGrid(pos) ? clamp(length(windAt(pos)) / u_max_speed, 0.0, 1.0) : 0.0;
  gl_PointSize = u_point_size;
#ifdef XUE_TERRAIN
  vec2 local = vec2(fract(pos.x - u_tile.x), pos.y - u_tile.y) / u_tile.z;
  if (local.x >= 1.0 || local.y < 0.0 || local.y >= 1.0) {
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    v_speed_t = 0.0;
    return;
  }
  vec2 tilePosition = local * 8192.0;
  gl_Position = projectTileFor3D(tilePosition, surfaceMeshElevation(tilePosition));
  if (surfaceOccluded(gl_Position)) v_speed_t = 0.0;
#else
  gl_Position = projectTile(vec2(pos.x + u_tile.x, pos.y));
#endif
}`;
}

const DRAW_FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in float v_speed_t;
uniform sampler2D u_palette;
// One tone for every particle, used when u_monochrome is on; the speed
// palette is still compiled in and is what an overlay drawn on its own uses.
uniform vec4 u_ink;
uniform float u_monochrome;
out vec4 out_color;
void main() {
  // A particle sitting in a null field — the wave vector's land, (0, 0)
  // exactly — would otherwise stand still as a dot; a wind is never that
  // calm, so the threshold (a hundredth of a percent of the ceiling) costs
  // it nothing.
  if (v_speed_t < 1e-4) discard;
  vec4 color = u_monochrome > 0.5
    ? u_ink
    : texture(u_palette, vec2((v_speed_t * 255.0 + 0.5) / 256.0, 0.5));
  out_color = vec4(color.rgb * color.a, color.a);
}`;

interface ProgramInfo {
  program: WebGLProgram;
  uniforms: Record<string, WebGLUniformLocation | null>;
}

function isLinear(quantization: BundleVariable["quantization"]): quantization is LinearQuantization {
  return quantization.type === "linear";
}

/** The grid's own footprint in world Mercator units, (x, y, width, height) —
 * where the update shader respawns particles. A global grid returns the whole
 * world square; a regional one returns just its window, with the x origin
 * wrapped into [0, 1) so a window crossing the antimeridian still works (the
 * shader takes `fract` of the seeded x). */
export function spawnRectangle(
  firstLongitude: number,
  firstLatitude: number,
  longitudeStep: number,
  latitudeStep: number,
  width: number,
  height: number,
  wraps: boolean,
): [number, number, number, number] {
  const north = mercatorY(firstLatitude);
  const south = mercatorY(firstLatitude + (height - 1) * latitudeStep);
  if (wraps) return [0, north, 1, south - north];
  const x = (((firstLongitude + 180) / 360) % 1 + 1) % 1;
  return [x, north, ((width - 1) * longitudeStep) / 360, south - north];
}

export class WindParticleLayer implements CustomLayerInterface {
  readonly id = "wind-particles";
  readonly type = "custom" as const;
  readonly renderingMode = "2d" as const;

  /** When false the layer neither simulates nor draws (another variable is on
   * screen); flipping it back on restarts from fresh trails. */
  private visible = false;
  /** When false (prefers-reduced-motion) the simulation is frozen: the
   * current particle positions still draw, but nothing advects. */
  animate = true;

  private readonly options: WindParticleOptions;
  private map: MaplibreMap | null = null;
  private gl: WebGL2RenderingContext | null = null;

  private updateProgram: ProgramInfo | null = null;
  private drawProgram: ProgramInfo | null = null;
  /** Whether particle positions are RGBA32F (EXT_color_buffer_float) rather
   * than two bytes per axis. */
  private floatState = false;
  /** Screen-space seeding's lattice, refreshed when the camera settles. */
  private seeds: { points: Float32Array; plane: number } | null = null;
  private reseed = false;
  /** The plane's projection matrix of the last frame drawn, which the
   * lattice's homogeneous points are taken through. */
  private lastMatrix: number[] | null = null;
  private seedRefreshQueued = false;
  /** Whether the lattice was tried since the camera last settled: a view
   * with no ground to seed on is not retried every frame. */
  private seedsAttempted = false;
  private readonly onMoveEnd = (): void => {
    this.refreshSeeds();
  };
  /** The point pass per projection variant and terrain mode. */
  private drawPrograms = new Map<string, ProgramInfo>();
  private fadeProgram: ProgramInfo | null = null;
  private screenProgram: ProgramInfo | null = null;

  private quadVertexArray: WebGLVertexArrayObject | null = null;
  private indexVertexArray: WebGLVertexArrayObject | null = null;
  private framebuffer: WebGLFramebuffer | null = null;

  private windTexture: WebGLTexture | null = null;
  private paletteTexture: WebGLTexture | null = null;
  private stateTextures: [WebGLTexture, WebGLTexture] | null = null;
  private screenTextures: [WebGLTexture, WebGLTexture] | null = null;
  private screenSize: [number, number] = [0, 0];

  private particleRes = 0;
  private particleCount = 0;

  // Grid + quantization of the wind bundle currently configured.
  private width = 0;
  private height = 0;
  private firstLongitude = -180;
  private firstLatitude = 90;
  private longitudeStep = 0.25;
  private latitudeStep = -0.25;
  private wraps = true;
  /** The model's own footprint when the grid extends past it (domain.ts):
   * no particle is drawn or kept outside. */
  private domain: LambertDomain | null = null;
  /** Respawn rectangle in world Mercator units, (x, y, width, height). */
  private spawn: [number, number, number, number] = [0, 0, 1, 1];
  private windOffset: [number, number] = [0, 0];
  private windScale: [number, number] = [0, 0];
  /** The magnitude the speed palette tops out at — and the scale a particle's
   * pace is read against. The 10 m wind's 40 m/s by default; an isobaric wind
   * or a vapour flux field sets its own. */
  private maxSpeed = WIND_SPEED_MAX;
  private pace = 1;
  /** Single tone every particle is drawn in, or null for the speed palette. */
  private ink: readonly [number, number, number, number] | null = null;

  // Pending planes survive context loss and re-apply in onAdd.
  private pendingU: Uint8Array | null = null;
  private pendingV: Uint8Array | null = null;
  private uploadedU: Uint8Array | null = null;
  private interleaved: Uint8Array | null = null;
  private windUploaded = false;

  private lastFrameTime = 0;
  private trailsStale = true;
  private readonly requestTrailClear = (): void => {
    this.trailsStale = true;
  };

  constructor(
    private readonly onUnsupported: (message: string) => void,
    options: Partial<WindParticleOptions> = {},
  ) {
    this.options = { ...WIND_PARTICLE_DEFAULTS, ...options };
    this.particleRes = Math.ceil(Math.sqrt(this.options.count));
    this.particleCount = this.particleRes * this.particleRes;
  }

  /** The magnitude the palette (and the pace) is normalised by. */
  setMaxSpeed(maxSpeed: number): void {
    this.maxSpeed = maxSpeed;
    this.map?.triggerRepaint();
  }

  /** How fast the particles run for a given field magnitude, as a multiple
   * of the wind's pace — 1 where the field is a speed in m/s, more where it
   * is not (the wave vector's magnitude is a height in metres). */
  setPace(pace: number): void {
    this.pace = pace;
    this.map?.triggerRepaint();
  }

  /** Adopt a vector bundle's grid and both components' linear quantization.
   * Clears any uploaded planes — the caller re-feeds them for the new grid.
   * `components` names the u/v pair inside the metadata; the 10 m wind's by
   * default. */
  configureGrid(metadata: BundleMetadata, components: readonly [DataVariableId, DataVariableId] = [WIND_COMPONENT_IDS[0]!, WIND_COMPONENT_IDS[1]!]): void {
    const grid = metadata.grid as Record<string, number | boolean>;
    this.width = (grid.width as number) ?? 0;
    this.height = (grid.height as number) ?? 0;
    this.firstLongitude = (grid.firstLongitude as number) ?? -180;
    this.firstLatitude = (grid.firstLatitude as number) ?? 90;
    this.longitudeStep = (grid.longitudeStep as number) ?? 0.25;
    this.latitudeStep = (grid.latitudeStep as number) ?? -0.25;
    this.wraps = (grid.wrapLongitude as boolean) ?? Math.abs(this.width * this.longitudeStep - 360) < 1e-6;
    this.spawn = spawnRectangle(
      this.firstLongitude,
      this.firstLatitude,
      this.longitudeStep,
      this.latitudeStep,
      this.width,
      this.height,
      this.wraps,
    );
    const [u, v] = components.map((id) => metadata.variables.find((item) => item.id === id));
    if (!u || !v || !isLinear(u.quantization) || !isLinear(v.quantization)) {
      throw new Error("vector bundle is missing linear-quantized u/v components");
    }
    this.windOffset = [u.quantization.offset, v.quantization.offset];
    this.windScale = [u.quantization.scale, v.quantization.scale];
    this.pendingU = null;
    this.pendingV = null;
    this.uploadedU = null;
    this.interleaved = null;
    this.windUploaded = false;
  }

  /** Feed the current frame's u and v quantized code planes. */
  setWindPlanes(u: Uint8Array, v: Uint8Array): void {
    this.pendingU = u;
    this.pendingV = v;
    this.uploadWind();
    this.map?.triggerRepaint();
  }

  /** Draw every particle in one tone (r, g, b, a in 0..1), or pass null to
   * color them by speed again. */
  setInk(ink: readonly [number, number, number, number] | null): void {
    this.ink = ink;
    this.map?.triggerRepaint();
  }

  /** Keep the particles to a regional model's own footprint, or to the
   * grid alone. */
  setDomain(domain: LambertDomain | null): void {
    if (this.domain === domain) return;
    this.domain = domain;
    this.trailsStale = true;
    this.map?.triggerRepaint();
  }

  setVisible(visible: boolean): void {
    if (this.visible === visible) return;
    this.visible = visible;
    this.trailsStale = true;
    this.lastFrameTime = 0;
    this.map?.triggerRepaint();
  }

  hasWind(): boolean {
    return this.pendingU !== null && this.pendingV !== null;
  }

  onAdd(map: MaplibreMap, gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    if (!(gl instanceof WebGL2RenderingContext)) {
      this.onUnsupported(t("webglUnavailable"));
      return;
    }
    this.map = map;
    this.gl = gl;
    map.on("move", this.requestTrailClear);
    map.on("moveend", this.onMoveEnd);
    map.on("terrain", this.onMoveEnd);
    this.seeds = null;

    // Float positions where the GPU can render to them: a 16-bit position is
    // 600 m on the ground, a step a particle at a valley's zoom never makes.
    this.floatState = gl.getExtension("EXT_color_buffer_float") !== null;
    this.updateProgram = this.createProgram(gl, QUAD_VERTEX_SHADER, withDefines(UPDATE_FRAGMENT_SHADER, this.stateDefines()), [
      "u_particles", "u_wind", "u_wind_offset", "u_wind_scale", "u_first", "u_step", "u_size", "u_wrap", "u_spawn",
      "u_rand_seed", "u_speed_factor", "u_elapsed", "u_drop_rate", "u_drop_rate_bump", "u_max_speed",
      "u_screen_spawn", "u_seeds", "u_forward", "u_plane", "u_reseed",
      ...DOMAIN_UNIFORM_NAMES,
    ]);
    this.drawPrograms.clear();
    this.fadeProgram = this.createProgram(gl, QUAD_VERTEX_SHADER, FADE_FRAGMENT_SHADER, ["u_screen", "u_fade"]);
    this.screenProgram = this.createProgram(gl, QUAD_VERTEX_SHADER, SCREEN_FRAGMENT_SHADER, ["u_screen", "u_opacity"]);

    // Unit quad for the update / fade / screen passes.
    this.quadVertexArray = gl.createVertexArray();
    gl.bindVertexArray(this.quadVertexArray);
    const quadBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1]), gl.STATIC_DRAW);
    const quadPosition = gl.getAttribLocation(this.updateProgram.program, "a_pos");
    gl.enableVertexAttribArray(quadPosition);
    gl.vertexAttribPointer(quadPosition, 2, gl.FLOAT, false, 0, 0);

    // One float index per particle for the point pass.
    this.indexVertexArray = gl.createVertexArray();
    gl.bindVertexArray(this.indexVertexArray);
    const indices = new Float32Array(this.particleCount);
    for (let index = 0; index < this.particleCount; index += 1) indices[index] = index;
    const indexBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, indexBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, indices, gl.STATIC_DRAW);
    // Every variant of the point pass binds a_index here (drawProgramFor).
    gl.enableVertexAttribArray(INDEX_ATTRIBUTE);
    gl.vertexAttribPointer(INDEX_ATTRIBUTE, 1, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    this.framebuffer = gl.createFramebuffer();

    // Random initial particle positions: any byte pattern decodes to a
    // position inside the world square. getRandomValues caps each call at
    // 65536 bytes, so fill in chunks.
    if (this.floatState) {
      const state = new Float32Array(this.particleCount * 4);
      for (let index = 0; index < this.particleCount; index += 1) {
        state[index * 4] = Math.random();
        state[index * 4 + 1] = Math.random();
      }
      this.stateTextures = [
        this.createFloatTexture(gl, this.particleRes, this.particleRes, state),
        this.createFloatTexture(gl, this.particleRes, this.particleRes, state),
      ];
    } else {
      const state = new Uint8Array(this.particleCount * 4);
      for (let offset = 0; offset < state.length; offset += 65536) {
        crypto.getRandomValues(state.subarray(offset, Math.min(offset + 65536, state.length)));
      }
      this.stateTextures = [
        this.createTexture(gl, gl.NEAREST, gl.RGBA, this.particleRes, this.particleRes, state),
        this.createTexture(gl, gl.NEAREST, gl.RGBA, this.particleRes, this.particleRes, state),
      ];
    }

    this.paletteTexture = this.createTexture(gl, gl.LINEAR, gl.RGBA, 256, 1, buildWindSpeedPalette());
    this.windTexture = null;
    this.windUploaded = false;
    this.screenTextures = null;
    this.screenSize = [0, 0];
    this.trailsStale = true;
    this.lastFrameTime = 0;
    this.uploadWind();
  }

  onRemove(): void {
    this.map?.off("move", this.requestTrailClear);
    this.map?.off("moveend", this.onMoveEnd);
    this.map?.off("terrain", this.onMoveEnd);
    this.map = null;
    this.gl = null;
    this.updateProgram = null;
    this.drawProgram = null;
    this.drawPrograms.clear();
    this.fadeProgram = null;
    this.screenProgram = null;
    this.quadVertexArray = null;
    this.indexVertexArray = null;
    this.framebuffer = null;
    this.windTexture = null;
    this.paletteTexture = null;
    this.stateTextures = null;
    this.screenTextures = null;
    this.windUploaded = false;
  }

  /** The point pass for this frame's projection, compiled on first use. */
  private drawProgramFor(gl: WebGL2RenderingContext, args: CustomRenderMethodInput, terrain: boolean): ProgramInfo {
    const key = `${args.shaderData.variantName}:${terrain ? "terrain" : "flat"}`;
    const cached = this.drawPrograms.get(key);
    if (cached) return cached;
    const defines = [...(terrain ? ["XUE_TERRAIN"] : []), ...this.stateDefines()];
    const info = this.createProgram(
      gl,
      withDefines(drawVertexShader(projectionPrelude(args)), defines),
      DRAW_FRAGMENT_SHADER,
      [
        "u_particles", "u_wind", "u_wind_offset", "u_wind_scale", "u_first", "u_step", "u_size", "u_wrap",
        "u_particles_res", "u_point_size", "u_max_speed", "u_palette",
        "u_ink", "u_monochrome", ...DOMAIN_UNIFORM_NAMES,
        ...PROJECTION_UNIFORM_NAMES, ...TERRAIN_UNIFORM_NAMES,
      ],
      { a_index: INDEX_ATTRIBUTE },
    );
    this.drawPrograms.set(key, info);
    return info;
  }

  private createProgram(
    gl: WebGL2RenderingContext,
    vertex: string,
    fragment: string,
    uniformNames: string[],
    attributes: Record<string, number> = {},
  ): ProgramInfo {
    const program = gl.createProgram();
    for (const [kind, source] of [
      [gl.VERTEX_SHADER, vertex],
      [gl.FRAGMENT_SHADER, fragment],
    ] as const) {
      const shader = gl.createShader(kind);
      if (!shader) throw new Error("failed to create shader");
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(`wind shader compile failed: ${gl.getShaderInfoLog(shader) ?? "unknown"}`);
      }
      gl.attachShader(program, shader);
    }
    for (const [name, location] of Object.entries(attributes)) gl.bindAttribLocation(program, location, name);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`wind program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
    }
    const uniforms: Record<string, WebGLUniformLocation | null> = {};
    for (const name of uniformNames) uniforms[name] = gl.getUniformLocation(program, name);
    return { program, uniforms };
  }

  private stateDefines(): string[] {
    return this.floatState ? ["XUE_FLOAT_STATE"] : [];
  }

  private createFloatTexture(gl: WebGL2RenderingContext, width: number, height: number, data: Float32Array): WebGLTexture {
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, width, height, 0, gl.RGBA, gl.FLOAT, data);
    return texture;
  }

  private createTexture(
    gl: WebGL2RenderingContext,
    filter: number,
    format: number,
    width: number,
    height: number,
    data: Uint8Array | null,
  ): WebGLTexture {
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    const internal = format === gl.RG ? gl.RG8 : gl.RGBA;
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, width, height, 0, format, gl.UNSIGNED_BYTE, data);
    return texture;
  }

  /** Interleave the pending u/v planes into the RG wind texture. */
  private uploadWind(): void {
    const gl = this.gl;
    if (!gl || !this.pendingU || !this.pendingV || !this.width || !this.height) return;
    if (this.uploadedU === this.pendingU && this.windUploaded) return;
    const length = this.width * this.height;
    if (this.pendingU.length !== length || this.pendingV.length !== length) return;
    if (!this.interleaved || this.interleaved.length !== length * 2) {
      this.interleaved = new Uint8Array(length * 2);
    }
    const packed = this.interleaved;
    const u = this.pendingU;
    const v = this.pendingV;
    for (let index = 0; index < length; index += 1) {
      packed[index * 2] = u[index]!;
      packed[index * 2 + 1] = v[index]!;
    }
    if (!this.windTexture) {
      this.windTexture = this.createTexture(gl, gl.LINEAR, gl.RG, this.width, this.height, packed);
    } else {
      gl.bindTexture(gl.TEXTURE_2D, this.windTexture);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG8, this.width, this.height, 0, gl.RG, gl.UNSIGNED_BYTE, packed);
    }
    gl.bindTexture(gl.TEXTURE_2D, this.windTexture);
    // 1440 columns cover the full 360 degrees, so REPEAT blends across the
    // antimeridian exactly like the scalar data texture.
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, this.wraps ? gl.REPEAT : gl.CLAMP_TO_EDGE);
    this.uploadedU = this.pendingU;
    this.windUploaded = true;
  }

  private ensureScreenTextures(gl: WebGL2RenderingContext): void {
    const width = gl.canvas.width;
    const height = gl.canvas.height;
    if (this.screenTextures && this.screenSize[0] === width && this.screenSize[1] === height) return;
    this.screenTextures = [
      this.createTexture(gl, gl.NEAREST, gl.RGBA, width, height, null),
      this.createTexture(gl, gl.NEAREST, gl.RGBA, width, height, null),
    ];
    this.screenSize = [width, height];
    this.trailsStale = false;
  }

  private bindTarget(gl: WebGL2RenderingContext, texture: WebGLTexture, width: number, height: number): void {
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.framebuffer);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
    gl.viewport(0, 0, width, height);
  }

  private bindWindUniforms(gl: WebGL2RenderingContext, info: ProgramInfo, particleUnit: number, windUnit: number): void {
    gl.uniform1i(info.uniforms.u_particles!, particleUnit);
    gl.uniform1i(info.uniforms.u_wind!, windUnit);
    gl.uniform2f(info.uniforms.u_wind_offset!, this.windOffset[0], this.windOffset[1]);
    gl.uniform2f(info.uniforms.u_wind_scale!, this.windScale[0], this.windScale[1]);
    gl.uniform2f(info.uniforms.u_first!, this.firstLongitude, this.firstLatitude);
    gl.uniform2f(info.uniforms.u_step!, this.longitudeStep, this.latitudeStep);
    gl.uniform2f(info.uniforms.u_size!, this.width, this.height);
    gl.uniform1f(info.uniforms.u_wrap!, this.wraps ? 1 : 0);
    setDomainUniforms(gl, info.uniforms, this.domain);
    if (info.uniforms.u_spawn) {
      gl.uniform4f(info.uniforms.u_spawn, this.spawn[0], this.spawn[1], this.spawn[2], this.spawn[3]);
    }
    gl.uniform1f(info.uniforms.u_max_speed!, this.maxSpeed);
  }

  private drawQuadTexture(gl: WebGL2RenderingContext, info: ProgramInfo, texture: WebGLTexture, value: number, valueName: string): void {
    gl.useProgram(info.program);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.uniform1i(info.uniforms.u_screen!, 0);
    gl.uniform1f(info.uniforms[valueName]!, value);
    gl.bindVertexArray(this.quadVertexArray);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
  }

  /** What screen-space seeding needs this frame, or null where it is off:
   * on the globe and while the view still holds a large part of the world. */
  private screenSeeding(
    input: CustomRenderMethodInput,
  ): { forward: Float32Array; seeds: Float32Array; plane: number } | null {
    if (!this.map || isGlobe(input) || this.map.getZoom() < SCREEN_SEEDING_ZOOM) return null;
    const forward = Array.from(input.defaultProjectionData.mainMatrix as ArrayLike<number>);
    this.lastMatrix = forward;
    if (!this.seeds) {
      // Never inside a render: unproject reads the terrain's framebuffer and
      // leaves the default one bound under whatever pass is drawing.
      if (!this.seedRefreshQueued && !this.seedsAttempted) {
        this.seedRefreshQueued = true;
        window.setTimeout(() => {
          this.seedRefreshQueued = false;
          this.refreshSeeds();
          this.map?.triggerRepaint();
        }, 0);
      }
      return null;
    }
    return {
      forward: new Float32Array(forward),
      seeds: this.seeds.points,
      plane: this.seeds.plane,
    };
  }

  /** The ground under a lattice of screen points, through the map's own
   * unproject (which reads the terrain where there is one): a few dozen
   * reads, so done when the camera settles, not every frame. Up each column
   * the ground must keep receding from the bottom of the screen; where it
   * stops, the column has reached the sky. */
  private refreshSeeds(): void {
    const map = this.map;
    const matrix = this.lastMatrix;
    if (!map || !matrix || map.getZoom() < SCREEN_SEEDING_ZOOM) {
      this.seeds = null;
      return;
    }
    this.seedsAttempted = true;
    const canvas = map.getCanvas();
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const plane = map.getTerrain() ? (map.queryTerrainElevation(map.getCenter()) ?? 0) : 0;
    const points = new Float32Array(SEED_LATTICE * SEED_LATTICE * 3);
    let valid = 0;
    for (let column = 0; column < SEED_LATTICE; column += 1) {
      const x = (column / (SEED_LATTICE - 1)) * width;
      let origin: [number, number] | null = null;
      let reach = -1;
      let sky = false;
      for (let row = SEED_LATTICE - 1; row >= 0; row -= 1) {
        const at = (row * SEED_LATTICE + column) * 3;
        const ground = sky ? null : map.unproject([x, (row / (SEED_LATTICE - 1)) * height]);
        const mx = ground ? (ground.lng + 180) / 360 : 0;
        const my = ground ? mercatorY(ground.lat) : 0;
        origin ??= [mx, my];
        const distance = Math.hypot(mx - origin[0], my - origin[1]);
        const w = matrix[3]! * mx + matrix[7]! * my + matrix[11]! * plane + matrix[15]!;
        if (!ground || !Number.isFinite(my) || distance < reach || w <= 0) {
          sky = true;
          points[at + 2] = -1;
          continue;
        }
        reach = distance;
        points[at] = mx / w;
        points[at + 1] = my / w;
        points[at + 2] = 1 / w;
        valid += 1;
      }
    }
    this.seeds = valid >= 4 ? { points, plane } : null;
    this.reseed = this.seeds !== null;
  }

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: unknown): void {
    if (!(gl instanceof WebGL2RenderingContext)) return;
    if (
      !this.visible ||
      !this.windUploaded ||
      !this.windTexture ||
      !this.stateTextures ||
      !this.updateProgram ||
      !this.map ||
      !this.fadeProgram ||
      !this.screenProgram
    ) {
      return;
    }
    const input = args as CustomRenderMethodInput;
    if (!input?.shaderData || !input.defaultProjectionData) return;
    const tiles = surfaceTiles(this.map, input);
    this.drawProgram = this.drawProgramFor(gl, input, tiles !== null);

    const now = performance.now();
    const elapsed = this.lastFrameTime === 0 ? 0 : Math.min((now - this.lastFrameTime) / 1000, 0.1);
    this.lastFrameTime = now;

    const previousFramebuffer = gl.getParameter(gl.FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
    const previousViewport = gl.getParameter(gl.VIEWPORT) as Int32Array;

    this.ensureScreenTextures(gl);
    const [previousScreen, targetScreen] = this.screenTextures!;
    const [currentState, nextState] = this.stateTextures;
    const [screenWidth, screenHeight] = this.screenSize;

    gl.disable(gl.BLEND);
    gl.disable(gl.STENCIL_TEST);
    gl.disable(gl.DEPTH_TEST);

    // 1. Trails: previous screen faded into the target, particles on top.
    this.bindTarget(gl, targetScreen, screenWidth, screenHeight);
    if (this.trailsStale) {
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      // Also clear the other screen texture so the next swap starts clean.
      this.bindTarget(gl, previousScreen, screenWidth, screenHeight);
      gl.clear(gl.COLOR_BUFFER_BIT);
      this.bindTarget(gl, targetScreen, screenWidth, screenHeight);
      this.trailsStale = false;
    } else {
      this.drawQuadTexture(gl, this.fadeProgram, previousScreen, this.options.fadeOpacity, "u_fade");
    }

    const draw = this.drawProgram;
    gl.useProgram(draw.program);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, currentState);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.windTexture);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    this.bindWindUniforms(gl, draw, 0, 1);
    gl.uniform1i(draw.uniforms.u_palette!, 2);
    const ink = this.ink;
    gl.uniform1f(draw.uniforms.u_monochrome!, ink ? 1 : 0);
    gl.uniform4f(draw.uniforms.u_ink!, ink?.[0] ?? 1, ink?.[1] ?? 1, ink?.[2] ?? 1, ink?.[3] ?? 1);
    gl.uniform1f(draw.uniforms.u_particles_res!, this.particleRes);
    gl.uniform1f(draw.uniforms.u_point_size!, Math.min(3, Math.max(1, 1.3 * (window.devicePixelRatio || 1))));
    gl.bindVertexArray(this.indexVertexArray);
    if (tiles) {
      for (const tile of tiles) {
        bindSurfaceTile(gl, draw.uniforms, tile, DEM_TEXTURE_UNIT, null, DEPTH_TEXTURE_UNIT);
        gl.drawArrays(gl.POINTS, 0, this.particleCount);
      }
    } else {
      setProjectionUniforms(gl, draw.uniforms, input.defaultProjectionData);
      for (const worldOffset of isGlobe(input) ? [0] : [-1, 0, 1]) {
        gl.uniform4f(draw.uniforms.u_tile!, worldOffset, 0, 1, 0);
        gl.drawArrays(gl.POINTS, 0, this.particleCount);
      }
    }
    gl.bindVertexArray(null);

    // 2. Advance the simulation into the ping-pong state texture.
    if (this.animate && elapsed > 0) {
      this.bindTarget(gl, nextState, this.particleRes, this.particleRes);
      const update = this.updateProgram;
      gl.useProgram(update.program);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, currentState);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, this.windTexture);
      this.bindWindUniforms(gl, update, 0, 1);
      gl.uniform1f(update.uniforms.u_rand_seed!, Math.random());
      gl.uniform1f(update.uniforms.u_speed_factor!, this.options.speedFactor * this.pace * zoomPace(this.map.getZoom()));
      const seeding = this.screenSeeding(input);
      gl.uniform1f(update.uniforms.u_screen_spawn!, seeding ? 1 : 0);
      if (seeding) {
        gl.uniformMatrix4fv(update.uniforms.u_forward!, false, seeding.forward);
        gl.uniform3fv(update.uniforms.u_seeds!, seeding.seeds);
        gl.uniform1f(update.uniforms.u_plane!, seeding.plane);
      }
      gl.uniform1f(update.uniforms.u_reseed!, seeding && this.reseed ? 1 : 0);
      if (seeding) this.reseed = false;
      gl.uniform1f(update.uniforms.u_elapsed!, elapsed);
      gl.uniform1f(update.uniforms.u_drop_rate!, this.options.dropRate);
      gl.uniform1f(update.uniforms.u_drop_rate_bump!, this.options.dropRateBump);
      gl.bindVertexArray(this.quadVertexArray);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      gl.bindVertexArray(null);
      this.stateTextures = [nextState, currentState];
    }

    // 3. Composite the fresh trail texture onto the map.
    gl.bindFramebuffer(gl.FRAMEBUFFER, previousFramebuffer);
    gl.viewport(previousViewport[0]!, previousViewport[1]!, previousViewport[2]!, previousViewport[3]!);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    this.drawQuadTexture(gl, this.screenProgram, targetScreen, this.options.opacity, "u_opacity");
    this.screenTextures = [targetScreen, previousScreen];

    if (this.animate) this.map?.triggerRepaint();
  }
}
