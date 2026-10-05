/** The single-site radar on the map: one sweep in the radar's own beam and
 * gate coordinates, drawn by a MapLibre custom layer.
 *
 * Nothing is resampled. The sweep's codes go up as they are, an `R8UI`
 * texture of `gates × 720`, and every fragment finds its own beam and gate:
 * Mercator → latitude and longitude, then the great-circle distance and
 * bearing from the antenna (the `atan2` form, well conditioned near it), and
 * the ground distance carried to slant range along a standard-refraction
 * beam (4/3 earth radius). A flat local approximation is ~12 km off at the
 * 460 km edge, so the sphere is not optional. Smoothing is a four-tap
 * bilinear over valid codes only: the reserved codes (0 no echo, 1 range
 * folded) are states, never blended into a value (`docs/zarr-profile.md`,
 * "Polar store").
 *
 * The mesh covers the disc's latitude-longitude box and is fine enough to
 * bend on the globe; the projection is MapLibre's own prelude
 * (`projection.ts`), so plane and globe come for free. It is not draped on
 * 3D terrain. */

import type { CustomLayerInterface, CustomRenderMethodInput, Map as MaplibreMap } from "maplibre-gl";

import { PROJECTION_UNIFORM_NAMES, mercatorYUnclamped, projectionPrelude, setProjectionUniforms } from "../projection";
import { BEAMS, GATE_KM, type RadarProduct } from "./schema";

const EARTH_RADIUS_KM = 6371.0088;
const REFRACTION = 4 / 3;
const MESH = 48;

const VERTEX = (prelude: string) => `#version 300 es
precision highp float;
${prelude}
in vec2 a_position;
out vec2 v_mercator;
void main() {
  v_mercator = a_position;
  gl_Position = projectTile(a_position);
}`;

const FRAGMENT = `#version 300 es
precision highp float;
precision highp usampler2D;
in vec2 v_mercator;
out vec4 o;
uniform usampler2D u_codes;
uniform vec2 u_site;      // antenna latitude, longitude, radians
uniform vec2 u_geometry;  // gates, elevation (radians)
uniform float u_product;  // 0 reflectivity, 1 velocity
uniform float u_opacity;
uniform float u_smooth;
const float PI = 3.141592653589793;
const float EARTH = ${EARTH_RADIUS_KM.toFixed(4)};
const float BEAM_EARTH = ${(EARTH_RADIUS_KM * REFRACTION).toFixed(4)};
const float GATE = ${GATE_KM.toFixed(3)};
const int BEAMS = ${BEAMS};

vec3 reflectivity(float dbz) {
  vec3 ramp[8] = vec3[8](vec3(0.39, 0.92, 0.92), vec3(0.0, 0.6, 0.95), vec3(0.0, 0.85, 0.0), vec3(0.0, 0.55, 0.0),
                         vec3(1.0, 0.9, 0.0), vec3(1.0, 0.55, 0.0), vec3(0.9, 0.0, 0.0), vec3(0.8, 0.0, 0.8));
  float t = clamp((dbz - 5.0) / 70.0 * 7.0, 0.0, 6.999);
  int i = int(t);
  return mix(ramp[i], ramp[i + 1], fract(t));
}

// Toward the radar is negative and green, away is positive and red, the
// convention every velocity display shares.
vec3 velocity(float speed) {
  float a = clamp(abs(speed) / 30.0, 0.0, 1.0);
  return speed < 0.0 ? mix(vec3(0.72, 0.85, 0.72), vec3(0.0, 0.45, 0.0), a)
                     : mix(vec3(0.88, 0.74, 0.74), vec3(0.62, 0.0, 0.0), a);
}

float value(float code) {
  return u_product < 0.5 ? -33.0 + 0.5 * code : -64.5 + 0.5 * code;
}

// Range-folded gates carry no value: a quiet grey-violet at half strength,
// so a wide folded band reads as "unknown" rather than as the strongest
// colour on the map.
const vec4 FOLDED = vec4(0.62, 0.58, 0.70, 1.0) * 0.45;

void main() {
  float longitude = (v_mercator.x * 2.0 - 1.0) * PI;
  float latitude = 2.0 * atan(exp(PI * (1.0 - 2.0 * v_mercator.y))) - 0.5 * PI;
  float dl = longitude - u_site.y;
  float sp1 = sin(u_site.x), cp1 = cos(u_site.x), sp2 = sin(latitude), cp2 = cos(latitude);
  float east = cp2 * sin(dl);
  float north = cp1 * sp2 - sp1 * cp2 * cos(dl);
  float angle = atan(sqrt(east * east + north * north), sp1 * sp2 + cp1 * cp2 * cos(dl));
  float bearing = atan(east, north);
  float a = EARTH * angle / BEAM_EARTH;
  float slant = BEAM_EARTH * sin(a) / cos(u_geometry.y + a);
  float gate = slant / GATE;
  if (gate < 0.0 || gate >= u_geometry.x) discard;
  float beam = mod(degrees(bearing), 360.0) * 2.0;
  float code;
  if (u_smooth < 0.5) {
    uint k = texelFetch(u_codes, ivec2(int(gate), int(beam) % BEAMS), 0).r;
    if (k == 0u) discard;
    if (k == 1u) { o = FOLDED * u_opacity; return; }
    code = float(k);
  } else {
    float gx = gate - 0.5, by = beam - 0.5;
    int g0 = int(floor(gx)), b0 = int(floor(by));
    float fx = gx - float(g0), fy = by - float(b0);
    float sum = 0.0, weight = 0.0, folded = 0.0;
    for (int j = 0; j < 2; j++) for (int i = 0; i < 2; i++) {
      int gi = clamp(g0 + i, 0, int(u_geometry.x) - 1);
      int bi = ((b0 + j) % BEAMS + BEAMS) % BEAMS;
      float w = (i == 0 ? 1.0 - fx : fx) * (j == 0 ? 1.0 - fy : fy);
      uint k = texelFetch(u_codes, ivec2(gi, bi), 0).r;
      if (k >= 2u) { sum += w * float(k); weight += w; } else if (k == 1u) folded += w;
    }
    if (weight < 0.5 && folded < 0.5) discard;
    if (folded >= weight) { o = FOLDED * u_opacity; return; }
    code = sum / weight;
  }
  float v = value(code);
  vec3 color;
  if (u_product < 0.5) {
    if (v < 5.0) discard;
    color = reflectivity(v);
  } else {
    color = velocity(v);
  }
  o = vec4(color, 1.0) * u_opacity;
}`;

const UNIFORMS = ["u_codes", "u_site", "u_geometry", "u_product", "u_opacity", "u_smooth", ...PROJECTION_UNIFORM_NAMES] as const;

interface Program {
  program: WebGLProgram;
  uniforms: Record<string, WebGLUniformLocation | null>;
}

export interface RadarDraw {
  latitude: number;
  longitude: number;
  product: RadarProduct;
  gates: number;
  /** Degrees. */
  elevation: number;
  codes: Uint8Array;
  /** Identifies the sweep, so the same one is not uploaded twice. */
  key: string;
  opacity: number;
}

function destination(latitude: number, longitude: number, km: number, bearing: number): [number, number] {
  const p1 = (latitude * Math.PI) / 180;
  const l1 = (longitude * Math.PI) / 180;
  const d = km / EARTH_RADIUS_KM;
  const b = (bearing * Math.PI) / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [(l2 * 180) / Math.PI, (p2 * 180) / Math.PI];
}

/** The disc's latitude-longitude box as a mesh in Mercator units. The box
 * comes from the disc's extremes, since Mercator scale changes across it. */
export function discMesh(latitude: number, longitude: number, radiusKm: number): Float32Array {
  let west = Infinity, east = -Infinity, south = Infinity, north = -Infinity;
  for (let bearing = 0; bearing < 360; bearing += 2) {
    const [x, y] = destination(latitude, longitude, radiusKm + 2, bearing);
    west = Math.min(west, x);
    east = Math.max(east, x);
    south = Math.min(south, y);
    north = Math.max(north, y);
  }
  const positions = new Float32Array((MESH + 1) * (MESH + 1) * 2);
  let at = 0;
  for (let j = 0; j <= MESH; j += 1) {
    for (let i = 0; i <= MESH; i += 1) {
      positions[at++] = (west + ((east - west) * i) / MESH + 180) / 360;
      positions[at++] = mercatorYUnclamped(north + ((south - north) * j) / MESH);
    }
  }
  return positions;
}

function meshIndices(): Uint16Array {
  const indices = new Uint16Array(MESH * MESH * 6);
  let at = 0;
  for (let j = 0; j < MESH; j += 1) {
    for (let i = 0; i < MESH; i += 1) {
      const a = j * (MESH + 1) + i;
      indices.set([a, a + 1, a + MESH + 1, a + 1, a + MESH + 2, a + MESH + 1], at);
      at += 6;
    }
  }
  return indices;
}

export class RadarLayer implements CustomLayerInterface {
  readonly id = "radar-sweep";
  readonly type = "custom" as const;
  readonly renderingMode = "2d" as const;
  private map: MaplibreMap | null = null;
  private gl: WebGL2RenderingContext | null = null;
  private programs = new Map<string, Program>();
  private vao: WebGLVertexArrayObject | null = null;
  private vertices: WebGLBuffer | null = null;
  private texture: WebGLTexture | null = null;
  private uploaded: string | null = null;
  private meshFor: string | null = null;
  private draw: RadarDraw | null = null;
  smooth = true;

  onAdd(map: MaplibreMap, gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    this.map = map;
    this.gl = gl as WebGL2RenderingContext;
    this.programs.clear();
    this.uploaded = null;
    this.meshFor = null;
  }

  onRemove(): void {
    const gl = this.gl;
    if (gl) {
      for (const { program } of this.programs.values()) gl.deleteProgram(program);
      if (this.texture) gl.deleteTexture(this.texture);
      if (this.vertices) gl.deleteBuffer(this.vertices);
      if (this.vao) gl.deleteVertexArray(this.vao);
    }
    this.programs.clear();
    this.texture = null;
    this.vertices = null;
    this.vao = null;
    this.map = null;
    this.gl = null;
  }

  /** What to draw next frame; null draws nothing. */
  set(draw: RadarDraw | null): void {
    this.draw = draw;
    this.map?.triggerRepaint();
  }

  private program(gl: WebGL2RenderingContext, args: CustomRenderMethodInput): Program {
    const variant = args.shaderData.variantName;
    const cached = this.programs.get(variant);
    if (cached) return cached;
    const compile = (type: number, source: string) => {
      const shader = gl.createShader(type)!;
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS))
        throw new Error(`radar shader compile failed: ${gl.getShaderInfoLog(shader) ?? "unknown"}`);
      return shader;
    };
    const program = gl.createProgram()!;
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX(projectionPrelude(args))));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.bindAttribLocation(program, 0, "a_position");
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS))
      throw new Error(`radar program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
    const uniforms: Record<string, WebGLUniformLocation | null> = {};
    for (const name of UNIFORMS) uniforms[name] = gl.getUniformLocation(program, name);
    const entry = { program, uniforms };
    this.programs.set(variant, entry);
    return entry;
  }

  private ensureMesh(gl: WebGL2RenderingContext, draw: RadarDraw): void {
    const key = `${draw.latitude},${draw.longitude},${draw.gates}`;
    if (this.meshFor === key && this.vao) return;
    if (!this.vao) {
      this.vao = gl.createVertexArray();
      gl.bindVertexArray(this.vao);
      this.vertices = gl.createBuffer();
      const indices = gl.createBuffer();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indices);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, meshIndices(), gl.STATIC_DRAW);
    } else {
      gl.bindVertexArray(this.vao);
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vertices);
    gl.bufferData(gl.ARRAY_BUFFER, discMesh(draw.latitude, draw.longitude, draw.gates * GATE_KM), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    this.meshFor = key;
  }

  private ensureTexture(gl: WebGL2RenderingContext, draw: RadarDraw): void {
    if (this.uploaded === draw.key && this.texture) return;
    if (!this.texture) this.texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8UI, draw.gates, BEAMS, 0, gl.RED_INTEGER, gl.UNSIGNED_BYTE, draw.codes);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.uploaded = draw.key;
  }

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: unknown): void {
    const draw = this.draw;
    if (!draw || draw.opacity <= 0) return;
    const gl2 = gl as WebGL2RenderingContext;
    const input = args as CustomRenderMethodInput;
    const { program, uniforms } = this.program(gl2, input);
    this.ensureMesh(gl2, draw);
    this.ensureTexture(gl2, draw);
    gl2.useProgram(program);
    setProjectionUniforms(gl2, uniforms, input.defaultProjectionData);
    gl2.uniform2f(uniforms.u_site!, (draw.latitude * Math.PI) / 180, (draw.longitude * Math.PI) / 180);
    gl2.uniform2f(uniforms.u_geometry!, draw.gates, (draw.elevation * Math.PI) / 180);
    gl2.uniform1f(uniforms.u_product!, draw.product === "n0b" ? 0 : 1);
    gl2.uniform1f(uniforms.u_opacity!, draw.opacity);
    gl2.uniform1f(uniforms.u_smooth!, this.smooth ? 1 : 0);
    gl2.activeTexture(gl2.TEXTURE0);
    gl2.bindTexture(gl2.TEXTURE_2D, this.texture);
    gl2.uniform1i(uniforms.u_codes!, 0);
    gl2.enable(gl2.BLEND);
    gl2.blendFunc(gl2.ONE, gl2.ONE_MINUS_SRC_ALPHA);
    gl2.bindVertexArray(this.vao);
    gl2.drawElements(gl2.TRIANGLES, MESH * MESH * 6, gl2.UNSIGNED_SHORT, 0);
    gl2.bindVertexArray(null);
  }
}
