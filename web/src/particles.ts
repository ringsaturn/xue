import type { CustomLayerInterface, Map as MaplibreMap } from "maplibre-gl";

import { t } from "./i18n";
import { extractMatrix } from "./layer";
import type { BundleMetadata, LinearQuantization } from "./manifest";
import { WIND_COMPONENT_IDS, type BundleVariable } from "./manifest";
import { mercatorY } from "./mercator";
import { buildWindSpeedPalette, WIND_SPEED_MAX } from "./palettes";

/**
 * MapLibre custom layer that advects GPU particles through the 10 m wind
 * field. The u/v quantized code planes are packed into
 * the R/G channels of one texture (the industry-wide convention), particle
 * positions live in two ping-pong RGBA8 state textures (16 bits per axis,
 * webgl-wind encoding), and trails come from ping-pong screen textures faded
 * a little every frame. Particles are colored through a 256x1 speed palette.
 *
 * The parameter set (count / speed factor / fade / drop a.k.a. reset rate /
 * speed color ramp) and its defaults follow the conventions shared by
 * webgl-wind, Windy and earth.nullschool.
 */

export interface WindParticleOptions {
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

export const WIND_PARTICLE_DEFAULTS: WindParticleOptions = {
  count: 65536,
  speedFactor: 55000,
  fadeOpacity: 0.955,
  dropRate: 0.003,
  dropRateBump: 0.01,
  opacity: 0.85,
};

const EARTH_CIRCUMFERENCE_M = 40075016.7;

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
  return vec2(color.r / 255.0 + color.b, color.g / 255.0 + color.a);
}

float latitudeOf(vec2 pos) {
  return 90.0 - (360.0 / PI) * atan(exp((pos.y * 2.0 - 1.0) * PI));
}

// Grid coordinates of a world position. The longitude offset wraps into
// [0, 360) so a global grid indexes from -180 as before and a regional
// window that crosses the antimeridian stays contiguous.
vec2 gridUv(vec2 pos) {
  float longitude = fract(pos.x) * 360.0 - 180.0;
  float latitude = latitudeOf(pos);
  return vec2(
    (mod(longitude - u_first.x, 360.0) / u_step.x + 0.5) / u_size.x,
    ((latitude - u_first.y) / u_step.y + 0.5) / u_size.y
  );
}

// A regional grid (a showcase case) has no wind outside its window; a
// particle that drifts out has to be respawned rather than pushed by the
// clamped edge texel forever.
bool inGrid(vec2 uv) {
  if (uv.y < 0.0 || uv.y > 1.0) return false;
  return u_wrap > 0.5 || (uv.x >= 0.0 && uv.x <= 1.0);
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
    inGrid(gridUv(pos)) ? 0.0 : 1.0
  );
  vec2 random_pos = vec2(
    fract(u_spawn.x + rand(seed + 1.3) * u_spawn.z),
    u_spawn.y + rand(seed + 2.1) * u_spawn.w
  );
  pos = mix(pos, random_pos, drop);

  out_color = vec4(fract(pos * 255.0), floor(pos * 255.0) / 255.0);
}`;

const DRAW_VERTEX_SHADER = `#version 300 es
precision highp float;
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
uniform float u_world_offset;
uniform mat4 u_matrix;
uniform float u_max_speed;
out float v_speed_t;
${WIND_SAMPLING}

void main() {
  vec2 lookup = vec2(
    fract(a_index / u_particles_res) + 0.5 / u_particles_res,
    floor(a_index / u_particles_res) / u_particles_res + 0.5 / u_particles_res
  );
  vec2 pos = decodePosition(texture(u_particles, lookup));
  v_speed_t = clamp(length(windAt(pos)) / u_max_speed, 0.0, 1.0);
  gl_PointSize = u_point_size;
  gl_Position = u_matrix * vec4(pos.x + u_world_offset, pos.y, 0.0, 1.0);
}`;

const DRAW_FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in float v_speed_t;
uniform sampler2D u_palette;
out vec4 out_color;
void main() {
  vec4 color = texture(u_palette, vec2((v_speed_t * 255.0 + 0.5) / 256.0, 0.5));
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
  /** Respawn rectangle in world Mercator units, (x, y, width, height). */
  private spawn: [number, number, number, number] = [0, 0, 1, 1];
  private windOffset: [number, number] = [0, 0];
  private windScale: [number, number] = [0, 0];

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

  /** Adopt the wind bundle's grid and both components' linear quantization.
   * Clears any uploaded planes — the caller re-feeds them for the new grid. */
  configureGrid(metadata: BundleMetadata): void {
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
    const [u, v] = WIND_COMPONENT_IDS.map((id) => metadata.variables.find((item) => item.id === id));
    if (!u || !v || !isLinear(u.quantization) || !isLinear(v.quantization)) {
      throw new Error("wind bundle is missing linear-quantized u/v components");
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

    this.updateProgram = this.createProgram(gl, QUAD_VERTEX_SHADER, UPDATE_FRAGMENT_SHADER, [
      "u_particles", "u_wind", "u_wind_offset", "u_wind_scale", "u_first", "u_step", "u_size", "u_wrap", "u_spawn",
      "u_rand_seed", "u_speed_factor", "u_elapsed", "u_drop_rate", "u_drop_rate_bump", "u_max_speed",
    ]);
    this.drawProgram = this.createProgram(gl, DRAW_VERTEX_SHADER, DRAW_FRAGMENT_SHADER, [
      "u_particles", "u_wind", "u_wind_offset", "u_wind_scale", "u_first", "u_step", "u_size", "u_wrap",
      "u_particles_res", "u_point_size", "u_world_offset", "u_matrix", "u_max_speed", "u_palette",
    ]);
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
    const indexPosition = gl.getAttribLocation(this.drawProgram.program, "a_index");
    gl.enableVertexAttribArray(indexPosition);
    gl.vertexAttribPointer(indexPosition, 1, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    this.framebuffer = gl.createFramebuffer();

    // Random initial particle positions: any byte pattern decodes to a
    // position inside the world square. getRandomValues caps each call at
    // 65536 bytes, so fill in chunks.
    const state = new Uint8Array(this.particleCount * 4);
    for (let offset = 0; offset < state.length; offset += 65536) {
      crypto.getRandomValues(state.subarray(offset, Math.min(offset + 65536, state.length)));
    }
    this.stateTextures = [
      this.createTexture(gl, gl.NEAREST, gl.RGBA, this.particleRes, this.particleRes, state),
      this.createTexture(gl, gl.NEAREST, gl.RGBA, this.particleRes, this.particleRes, state),
    ];

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
    this.map = null;
    this.gl = null;
    this.updateProgram = null;
    this.drawProgram = null;
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

  private createProgram(gl: WebGL2RenderingContext, vertex: string, fragment: string, uniformNames: string[]): ProgramInfo {
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
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`wind program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
    }
    const uniforms: Record<string, WebGLUniformLocation | null> = {};
    for (const name of uniformNames) uniforms[name] = gl.getUniformLocation(program, name);
    return { program, uniforms };
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
    if (info.uniforms.u_spawn) {
      gl.uniform4f(info.uniforms.u_spawn, this.spawn[0], this.spawn[1], this.spawn[2], this.spawn[3]);
    }
    gl.uniform1f(info.uniforms.u_max_speed!, WIND_SPEED_MAX);
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

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: unknown): void {
    if (!(gl instanceof WebGL2RenderingContext)) return;
    if (
      !this.visible ||
      !this.windUploaded ||
      !this.windTexture ||
      !this.stateTextures ||
      !this.updateProgram ||
      !this.drawProgram ||
      !this.fadeProgram ||
      !this.screenProgram
    ) {
      return;
    }
    const matrix = extractMatrix(args);
    if (!matrix) return;

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
    gl.uniform1f(draw.uniforms.u_particles_res!, this.particleRes);
    gl.uniform1f(draw.uniforms.u_point_size!, Math.min(3, Math.max(1, 1.3 * (window.devicePixelRatio || 1))));
    gl.uniformMatrix4fv(draw.uniforms.u_matrix!, false, matrix);
    gl.bindVertexArray(this.indexVertexArray);
    for (const worldOffset of [-1, 0, 1]) {
      gl.uniform1f(draw.uniforms.u_world_offset!, worldOffset);
      gl.drawArrays(gl.POINTS, 0, this.particleCount);
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
      gl.uniform1f(update.uniforms.u_speed_factor!, this.options.speedFactor);
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
