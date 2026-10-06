/**
 * The terrain-shadow march on the GPU, for `shadow.worker.ts` only.
 *
 * One fragment per pixel of the mosaic's inner rectangle runs `marchLit`'s
 * march: the same per-pixel sun, refraction, earth curvature, penumbra,
 * twilight, ×1.06 steps and early exits, with the constants taken from
 * `shadow.ts` so the two cannot drift apart. The heights go up as R32F,
 * which WebGL2 cannot filter, so the bilinear sample is done by hand with
 * `texelFetch`, clamped at the last row and column as `marchLit` clamps.
 *
 * The context is the worker's own OffscreenCanvas, never the map's. The
 * first pass writes the mask into an R8 target that is read back for the
 * field layers; the second, only when an ink is asked for, paints the
 * basemap's picture onto the canvas and hands it over as an ImageBitmap.
 */

import {
  DEG,
  EFFECTIVE_RADIUS,
  EQUATOR_METRES,
  MAX_SHADOW_METRES,
  MIN_ELEVATION,
  NIGHT,
  STEP_GROWTH,
  SUN_RADIUS,
  hourAngle,
  solarEphemeris,
  worldPixels,
  type MosaicGeometry,
} from "./shadow";

/** The march loop's fixed bound (GLSL ES 3.00 loops need a constant one):
 * ×1.06 steps cover about 29 000 px in 128 of them, far past any mosaic's
 * diagonal. `marchStepBound` checks each mosaic against it. */
export const MAX_MARCH_STEPS = 128;

/** The steps a march may take before it must have left a mosaic: any point
 * farther than the diagonal from a pixel inside is outside. Two spare for
 * the float accumulation of the travelled distance. */
export function marchStepBound(width: number, height: number): number {
  const diagonal = Math.hypot(width, height);
  return Math.ceil(Math.log(diagonal * (STEP_GROWTH - 1) + 1) / Math.log(STEP_GROWTH)) + 2;
}

export interface MarchUniforms {
  /** sin and cos of the sun's declination. */
  sinDeclination: number;
  cosDeclination: number;
  /** The hour angle at the inner rectangle's first column's centre, radians
   * in [-π, π), and its growth per column: the hour angle is linear in
   * longitude, which is linear in Mercator x. */
  hourAngle0: number;
  hourAngleStep: number;
  /** π(1 − 2y) at the first inner row's centre and its fall per row, so
   * the latitude is atan(sinh(arg)) without large world-pixel offsets in
   * single precision. */
  mercatorArg0: number;
  mercatorArgStep: number;
  /** Ground metres per DEM pixel on the equator; × cos φ elsewhere. */
  metresPerPixel: number;
  maxDistance: number;
  /** 1 / 2R′, the curvature drop's factor. */
  inverse2R: number;
}

/** What the shader needs from a request besides the heights, worked out in
 * double precision so single precision only ever sees small numbers. */
export function marchUniforms(geometry: MosaicGeometry, timeMs: number): MarchUniforms {
  const { demZoom, originX, originY, inner } = geometry;
  const world = worldPixels(demZoom);
  const ephemeris = solarEphemeris(timeMs);
  const lon0 = ((originX + inner.x + 0.5) / world) * 360 - 180;
  const turn = 2 * Math.PI;
  const h0 = hourAngle(ephemeris, lon0);
  return {
    sinDeclination: Math.sin(ephemeris.declination),
    cosDeclination: Math.cos(ephemeris.declination),
    hourAngle0: h0 - turn * Math.floor((h0 + Math.PI) / turn),
    hourAngleStep: (360 / world) * DEG,
    mercatorArg0: Math.PI * (1 - (2 * (originY + inner.y + 0.5)) / world),
    mercatorArgStep: (2 * Math.PI) / world,
    metresPerPixel: EQUATOR_METRES / world,
    maxDistance: MAX_SHADOW_METRES,
    inverse2R: 1 / (2 * EFFECTIVE_RADIUS),
  };
}

/** A number as a GLSL float literal. */
function glsl(value: number): string {
  return Number.isInteger(value) ? value.toFixed(1) : String(value);
}

const VERTEX = `#version 300 es
void main() {
  vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}`;

const MARCH = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;

#define DEG ${glsl(DEG)}
#define NIGHT ${glsl(NIGHT)}
#define SUN_RADIUS ${glsl(SUN_RADIUS)}
#define MIN_ELEVATION ${glsl(MIN_ELEVATION)}
#define STEP_GROWTH ${glsl(STEP_GROWTH)}
#define MAX_STEPS ${MAX_MARCH_STEPS}
#define FAR 1e30

uniform sampler2D u_elevation;
uniform ivec2 u_size;
uniform ivec2 u_inner;
uniform float u_zmax;
uniform vec2 u_declination;
uniform float u_h0;
uniform float u_dh;
uniform float u_arg0;
uniform float u_darg;
uniform float u_metres;
uniform float u_max_distance;
uniform float u_inverse_2r;

out vec4 fragColor;

// shadow.ts::refraction, degrees to add to a geometric elevation.
float refraction(float e) {
  if (e > 85.0) return 0.0;
  float te = tan(e * DEG);
  float seconds;
  if (e > 5.0) {
    float inv = 1.0 / te;
    float inv2 = inv * inv;
    seconds = inv * (58.1 + inv2 * (-0.07 + inv2 * 0.000086));
  } else if (e > -0.575) {
    seconds = 1735.0 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)));
  } else {
    seconds = -20.772 / te;
  }
  return seconds / 3600.0;
}

float height(ivec2 p) {
  return texelFetch(u_elevation, p, 0).r;
}

void main() {
  // Row 0 is the bottom of the target, which readPixels returns first: the
  // north row, as marchLit lays it out.
  int col = int(gl_FragCoord.x);
  int row = int(gl_FragCoord.y);
  float lat = atan(sinh(u_arg0 - float(row) * u_darg));
  float sinLat = sin(lat);
  float cosLat = cos(lat);
  float h = u_h0 + float(col) * u_dh;
  float sinH = sin(h);
  float cosH = cos(h);
  float sinD = u_declination.x;
  float cosD = u_declination.y;
  float sinE = sinLat * sinD + cosLat * cosD * cosH;
  float geometric = asin(clamp(sinE, -1.0, 1.0)) / DEG;
  float e = geometric + refraction(geometric);
  if (e < NIGHT) {
    fragColor = vec4(0.0);
    return;
  }
  float east = -sinH * cosD;
  float northward = cosLat * sinD - sinLat * cosD * cosH;
  float norm = sqrt(east * east + northward * northward);
  if (norm == 0.0) norm = 1.0;
  vec2 dir = vec2(east, -northward) / norm;

  float twilight = e < 0.0 ? smoothstep(NIGHT, 0.0, e) : 1.0;
  float sunE = max(e, SUN_RADIUS);
  ivec2 m = u_inner + ivec2(col, row);
  float z0 = height(m);
  float relief = u_zmax - z0;
  float horizonTan = -FAR;
  if (relief > 0.0) {
    float mpp = u_metres * cosLat;
    float limit = min(relief / tan(max(sunE, MIN_ELEVATION) * DEG), u_max_distance);
    float hiddenTan = sunE + SUN_RADIUS >= 90.0 ? FAR : tan((sunE + SUN_RADIUS) * DEG);
    float mattersTan = tan((sunE - SUN_RADIUS) * DEG);
    ivec2 lastTexel = u_size - 1;
    vec2 last = vec2(lastTexel);
    vec2 origin = vec2(m);
    float step = 1.0;
    float travelled = 0.0;
    for (int i = 0; i < MAX_STEPS; i++) {
      travelled += step;
      step *= STEP_GROWTH;
      float d = travelled * mpp;
      if (d > limit) break;
      vec2 p = origin + dir * travelled;
      if (p.x < 0.0 || p.y < 0.0 || p.x > last.x || p.y > last.y) break;
      float drop = d * d * u_inverse_2r;
      ivec2 p0 = ivec2(p);
      vec2 f = p - vec2(p0);
      ivec2 p1 = min(p0 + 1, lastTexel);
      float a = height(p0);
      float b = height(ivec2(p1.x, p0.y));
      float c = height(ivec2(p0.x, p1.y));
      float g = height(p1);
      float top = a + (b - a) * f.x;
      float z = top + (c + (g - c) * f.x - top) * f.y;
      float t = (z - drop - z0) / d;
      if (t > horizonTan) {
        horizonTan = t;
        if (horizonTan >= hiddenTan) break;
      }
      if ((u_zmax - drop - z0) / d <= max(horizonTan, mattersTan)) break;
    }
  }
  float horizon = atan(horizonTan) / DEG;
  float lit = smoothstep(-SUN_RADIUS, SUN_RADIUS, sunE - horizon) * twilight;
  fragColor = vec4(floor(lit * 255.0 + 0.5) / 255.0, 0.0, 0.0, 1.0);
}`;

// The canvas is unpremultiplied (premultipliedAlpha: false), so this writes
// the same straight-alpha pixels the CPU path puts in its ImageData.
const INK = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;

uniform sampler2D u_lit;
uniform int u_height;
uniform vec4 u_ink;

out vec4 fragColor;

void main() {
  // The canvas's bottom row is the picture's bottom row; the mask's row 0
  // is north.
  ivec2 p = ivec2(gl_FragCoord.xy);
  p.y = u_height - 1 - p.y;
  float lit = texelFetch(u_lit, p, 0).r;
  fragColor = vec4(u_ink.rgb, (1.0 - lit) * u_ink.a);
}`;

export interface ShadowInk {
  rgb: readonly [number, number, number];
  alpha: number;
}

export interface GpuMarch {
  /** `marchLit`'s mask: inner width × height bytes, north row first. */
  lit: Uint8Array;
  image?: ImageBitmap;
}

type Uniforms = Record<string, WebGLUniformLocation | null>;

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("could not create a shader");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS) && !gl.isContextLost()) {
    throw new Error(`shadow shader: ${gl.getShaderInfoLog(shader) ?? "compile failed"}`);
  }
  return shader;
}

function link(gl: WebGL2RenderingContext, fragment: string, names: string[]): { program: WebGLProgram; uniforms: Uniforms } {
  const program = gl.createProgram();
  if (!program) throw new Error("could not create a program");
  const vs = compile(gl, gl.VERTEX_SHADER, VERTEX);
  const fs = compile(gl, gl.FRAGMENT_SHADER, fragment);
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(`shadow program: ${gl.getProgramInfoLog(program) ?? "link failed"}`);
  }
  const uniforms: Uniforms = {};
  for (const name of names) uniforms[name] = gl.getUniformLocation(program, name);
  return { program, uniforms };
}

function nearestTexture(gl: WebGL2RenderingContext): WebGLTexture {
  const texture = gl.createTexture();
  if (!texture) throw new Error("could not create a texture");
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return texture;
}

export class ShadowGL {
  private readonly canvas: OffscreenCanvas;
  private readonly gl: WebGL2RenderingContext;
  private readonly marcher: { program: WebGLProgram; uniforms: Uniforms };
  private readonly ink: { program: WebGLProgram; uniforms: Uniforms };
  private readonly elevation: WebGLTexture;
  private readonly litTexture: WebGLTexture;
  private readonly framebuffer: WebGLFramebuffer;
  private readonly vao: WebGLVertexArrayObject;
  private readonly maxSize: number;
  /** Whether RED / UNSIGNED_BYTE reads straight off the R8 target, a quarter
   * of the bytes of the always-allowed RGBA read. */
  private readonly readRed: boolean;
  private litWidth = 0;
  private litHeight = 0;
  private lost = false;

  /** Throws when the worker has no WebGL2 or the shaders do not build. */
  constructor() {
    if (typeof OffscreenCanvas === "undefined") throw new Error("no OffscreenCanvas");
    this.canvas = new OffscreenCanvas(1, 1);
    this.canvas.addEventListener("webglcontextlost", () => {
      this.lost = true;
    });
    const gl = this.canvas.getContext("webgl2", {
      alpha: true,
      premultipliedAlpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      preserveDrawingBuffer: false,
    });
    if (!gl) throw new Error("no WebGL2 context");
    this.gl = gl;
    this.marcher = link(gl, MARCH, [
      "u_elevation",
      "u_size",
      "u_inner",
      "u_zmax",
      "u_declination",
      "u_h0",
      "u_dh",
      "u_arg0",
      "u_darg",
      "u_metres",
      "u_max_distance",
      "u_inverse_2r",
    ]);
    this.ink = link(gl, INK, ["u_lit", "u_height", "u_ink"]);
    this.elevation = nearestTexture(gl);
    this.litTexture = nearestTexture(gl);
    const framebuffer = gl.createFramebuffer();
    const vao = gl.createVertexArray();
    if (!framebuffer || !vao) throw new Error("could not create GL objects");
    this.framebuffer = framebuffer;
    this.vao = vao;
    this.maxSize = Math.min(
      gl.getParameter(gl.MAX_TEXTURE_SIZE) as number,
      gl.getParameter(gl.MAX_RENDERBUFFER_SIZE) as number,
    );
    this.resizeLit(1, 1);
    gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
    this.readRed =
      gl.getParameter(gl.IMPLEMENTATION_COLOR_READ_FORMAT) === gl.RED &&
      gl.getParameter(gl.IMPLEMENTATION_COLOR_READ_TYPE) === gl.UNSIGNED_BYTE;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.check();
  }

  private check(): void {
    if (this.lost || this.gl.isContextLost()) {
      this.lost = true;
      throw new Error("WebGL context lost");
    }
  }

  private resizeLit(width: number, height: number): void {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.litTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, width, height, 0, gl.RED, gl.UNSIGNED_BYTE, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.framebuffer);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.litTexture, 0);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE && !gl.isContextLost()) {
      throw new Error("the shadow mask target is incomplete");
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.litWidth = width;
    this.litHeight = height;
  }

  /**
   * `marchLit(elevation, geometry, timeMs)` on the GPU, plus the inked
   * picture when `ink` is given. Null when this mosaic is beyond the GPU
   * path (too large a texture, or a march longer than the loop's bound);
   * throws when the GPU itself fails.
   */
  march(elevation: Float32Array, geometry: MosaicGeometry, timeMs: number, ink?: ShadowInk): GpuMarch | null {
    this.check();
    const { width, height, inner } = geometry;
    if (Math.max(width, height) > this.maxSize) return null;
    if (marchStepBound(width, height) > MAX_MARCH_STEPS) return null;
    const gl = this.gl;

    let zMax = -Infinity;
    for (let i = 0; i < elevation.length; i++) {
      const z = elevation[i] as number;
      if (z > zMax) zMax = z;
    }
    const u = marchUniforms(geometry, timeMs);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.elevation);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, width, height, 0, gl.RED, gl.FLOAT, elevation);
    if (this.litWidth !== inner.width || this.litHeight !== inner.height) this.resizeLit(inner.width, inner.height);

    gl.bindVertexArray(this.vao);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.framebuffer);
    gl.viewport(0, 0, inner.width, inner.height);
    gl.disable(gl.BLEND);
    const m = this.marcher;
    gl.useProgram(m.program);
    gl.uniform1i(m.uniforms.u_elevation ?? null, 0);
    gl.uniform2i(m.uniforms.u_size ?? null, width, height);
    gl.uniform2i(m.uniforms.u_inner ?? null, inner.x, inner.y);
    gl.uniform1f(m.uniforms.u_zmax ?? null, zMax);
    gl.uniform2f(m.uniforms.u_declination ?? null, u.sinDeclination, u.cosDeclination);
    gl.uniform1f(m.uniforms.u_h0 ?? null, u.hourAngle0);
    gl.uniform1f(m.uniforms.u_dh ?? null, u.hourAngleStep);
    gl.uniform1f(m.uniforms.u_arg0 ?? null, u.mercatorArg0);
    gl.uniform1f(m.uniforms.u_darg ?? null, u.mercatorArgStep);
    gl.uniform1f(m.uniforms.u_metres ?? null, u.metresPerPixel);
    gl.uniform1f(m.uniforms.u_max_distance ?? null, u.maxDistance);
    gl.uniform1f(m.uniforms.u_inverse_2r ?? null, u.inverse2R);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    const lit = new Uint8Array(inner.width * inner.height);
    if (this.readRed) {
      gl.pixelStorei(gl.PACK_ALIGNMENT, 1);
      gl.readPixels(0, 0, inner.width, inner.height, gl.RED, gl.UNSIGNED_BYTE, lit);
    } else {
      const rgba = new Uint8Array(inner.width * inner.height * 4);
      gl.readPixels(0, 0, inner.width, inner.height, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
      for (let i = 0, j = 0; i < lit.length; i++, j += 4) lit[i] = rgba[j] as number;
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.check();
    if (!ink) return { lit };

    if (this.canvas.width !== inner.width) this.canvas.width = inner.width;
    if (this.canvas.height !== inner.height) this.canvas.height = inner.height;
    gl.viewport(0, 0, inner.width, inner.height);
    const k = this.ink;
    gl.useProgram(k.program);
    gl.bindTexture(gl.TEXTURE_2D, this.litTexture);
    gl.uniform1i(k.uniforms.u_lit ?? null, 0);
    gl.uniform1i(k.uniforms.u_height ?? null, inner.height);
    gl.uniform4f(k.uniforms.u_ink ?? null, ink.rgb[0] / 255, ink.rgb[1] / 255, ink.rgb[2] / 255, ink.alpha);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    const image = this.canvas.transferToImageBitmap();
    this.check();
    return { lit, image };
  }

  dispose(): void {
    const gl = this.gl;
    if (!gl.isContextLost()) {
      gl.deleteTexture(this.elevation);
      gl.deleteTexture(this.litTexture);
      gl.deleteFramebuffer(this.framebuffer);
      gl.deleteVertexArray(this.vao);
      gl.deleteProgram(this.marcher.program);
      gl.deleteProgram(this.ink.program);
    }
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  }
}
