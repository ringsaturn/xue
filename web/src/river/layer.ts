/**
 * The river reaches as lines: a MapLibre custom WebGL2 layer in the manner
 * of `layer.ts`, drawing a network case's reaches coloured by their flow
 * against their usual flow and wide by their flow (`palette.ts`).
 *
 * Geometry is uploaded once per GeoParquet row group: every segment of
 * every reach is one instance of a four-vertex strip, carrying its two
 * ends in Mercator units and its reach's feature index; the vertex stage
 * projects both ends through MapLibre's projection prelude (`projectTile`,
 * so the plane and the globe both work), and pushes the strip out across
 * the segment in device pixels. The values change per frame and are one
 * RG32F texture of the reach's flow and usual flow, read by feature with
 * `texelFetch`, so a frame step uploads 10 000 pairs and rebuilds nothing.
 *
 * Segments overlap by half a width at each end, which stands in for joins.
 *
 * With the 3D relief on, MapLibre does not drape a custom layer, and a line
 * left on the sea-level plane shows under a pitched camera far from the
 * valley it follows. So, as the field and the particles do
 * (`projection.ts`), the segments are drawn once per terrain tile: each is
 * clipped to the tile, both clipped ends are lifted onto the tile's DEM, and
 * a segment whose end the terrain's depth says is behind a ridge is left
 * undrawn.
 */

import type { CustomLayerInterface, CustomRenderMethodInput, Map as MaplibreMap } from "maplibre-gl";

import { mercatorX, mercatorY } from "../mercator";
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
} from "../projection";
import {
  RIVER_BASEFLOW_FLOOR,
  RIVER_NODATA_COLOR,
  RIVER_RATIO_BREAKS,
  RIVER_WIDTH_MAX_PX,
  RIVER_WIDTH_MIN_PX,
  riverClassColors,
} from "./palette";

export const RIVER_LAYER_ID = "river-reaches";

/** Texels per row of the value texture. */
const VALUE_TEXTURE_WIDTH = 4096;
/** Floats per segment instance: start x, y, end x, y, feature. */
const INSTANCE_FLOATS = 5;

const NODATA_COLOR = new Float32Array(RIVER_NODATA_COLOR);

const CORNER_ATTRIBUTE = 0;
const START_ATTRIBUTE = 1;
const END_ATTRIBUTE = 2;
const FEATURE_ATTRIBUTE = 3;

/** Unit 0 carries the values; the terrain's DEM and depth ride above. */
const DEM_TEXTURE_UNIT = 1;
const DEPTH_TEXTURE_UNIT = 2;

function vertexShader(prelude: string): string {
  return `#version 300 es
precision highp float;
${prelude}
in vec2 a_corner;
in vec2 a_start;
in vec2 a_end;
in float a_feature;
uniform highp sampler2D u_values;
uniform vec2 u_viewport;
// A world copy's offset in the flat pass; the tile's Mercator origin (wrap
// included) and side on the terrain.
uniform vec4 u_tile;
uniform vec4 u_colors[4];
uniform vec4 u_nodata;
uniform vec3 u_breaks;
uniform vec2 u_width;
uniform float u_floor;
out vec4 v_color;
out float v_dist;
out float v_half;
#ifdef XUE_TERRAIN
${TERRAIN_VERTEX_GLSL}
${TERRAIN_DEPTH_GLSL}
out float v_hidden;
#endif

void main() {
  int feature = int(a_feature + 0.5);
  vec2 sample_ = texelFetch(u_values, ivec2(feature % ${VALUE_TEXTURE_WIDTH}, feature / ${VALUE_TEXTURE_WIDTH}), 0).rg;
  float flow = sample_.r;
  // No value is written as a negative flow: NaN does not survive every
  // driver's comparisons.
  bool nodata = flow < 0.0;
  float width = nodata ? u_width.x : clamp(u_width.x + log(max(flow, 1.0)) / log(10.0), u_width.x, u_width.y);
  if (nodata) {
    v_color = u_nodata;
  } else {
    float ratio = flow / max(sample_.g, u_floor);
    int index = int(ratio >= u_breaks.x) + int(ratio >= u_breaks.y) + int(ratio >= u_breaks.z);
    v_color = u_colors[index];
  }
#ifdef XUE_TERRAIN
  // The segment in tile-local units, clipped to the tile (Liang-Barsky), so
  // each end is lifted by the DEM that covers it and the pieces of a
  // segment drawn by two tiles meet at the tile's edge.
  vec2 origin = vec2(u_tile.x - floor(u_tile.x), u_tile.y);
  vec2 l0 = (a_start - origin) / u_tile.z;
  vec2 l1 = (a_end - origin) / u_tile.z;
  vec2 delta = l1 - l0;
  float t0 = 0.0;
  float t1 = 1.0;
  for (int axis = 0; axis < 2; axis++) {
    if (abs(delta[axis]) < 1e-9) {
      if (l0[axis] < 0.0 || l0[axis] > 1.0) t0 = 2.0;
    } else {
      float ta = -l0[axis] / delta[axis];
      float tb = (1.0 - l0[axis]) / delta[axis];
      t0 = max(t0, min(ta, tb));
      t1 = min(t1, max(ta, tb));
    }
  }
  if (t0 > t1) {
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    v_color = vec4(0.0);
    v_dist = 0.0;
    v_half = 0.0;
    v_hidden = 1.0;
    return;
  }
  vec2 c0 = clamp(l0 + delta * t0, 0.0, 1.0) * 8192.0;
  vec2 c1 = clamp(l0 + delta * t1, 0.0, 1.0) * 8192.0;
  // The far edge read from the last mesh cell, not the one past it.
  vec4 p0 = projectTileFor3D(c0, surfaceMeshElevation(min(c0, vec2(8191.0))));
  vec4 p1 = projectTileFor3D(c1, surfaceMeshElevation(min(c1, vec2(8191.0))));
  v_hidden = surfaceOccluded(a_corner.x < 0.5 ? p0 : p1) ? 1.0 : 0.0;
  // An end cut by the tile's edge carries no cap: the next tile's piece
  // starts there, and two caps would double the alpha across the seam.
  float cap0 = t0 > 0.0 ? 0.0 : 1.0;
  float cap1 = t1 < 1.0 ? 0.0 : 1.0;
#else
  vec4 p0 = projectTile(a_start + vec2(u_tile.x, 0.0));
  vec4 p1 = projectTile(a_end + vec2(u_tile.x, 0.0));
  float cap0 = 1.0;
  float cap1 = 1.0;
#endif
  vec2 half_viewport = u_viewport * 0.5;
  vec2 s0 = p0.xy / p0.w * half_viewport;
  vec2 s1 = p1.xy / p1.w * half_viewport;
  vec2 along = s1 - s0;
  float len = length(along);
  vec2 dir = len > 1e-6 ? along / len : vec2(1.0, 0.0);
  vec2 normal = vec2(-dir.y, dir.x);
  float half_width = width * 0.5;
  // Half a pixel of fringe for the antialiasing ramp.
  float reach = half_width + 0.5;
  vec4 position = a_corner.x < 0.5 ? p0 : p1;
  vec2 offset = normal * a_corner.y * reach + dir * (a_corner.x < 0.5 ? -half_width * cap0 : half_width * cap1);
  position.xy += offset / half_viewport * position.w;
  gl_Position = position;
  v_dist = a_corner.y * reach;
  v_half = half_width;
}`;
}

const FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in vec4 v_color;
in float v_dist;
in float v_half;
#ifdef XUE_TERRAIN
in float v_hidden;
#endif
out vec4 fragColor;
void main() {
#ifdef XUE_TERRAIN
  if (v_hidden > 0.5) discard;
#endif
  float coverage = clamp(v_half + 0.5 - abs(v_dist), 0.0, 1.0);
  float alpha = v_color.a * coverage;
  fragColor = vec4(v_color.rgb * alpha, alpha);
}`;

const UNIFORM_NAMES = [
  "u_values",
  "u_viewport",
  "u_tile",
  "u_colors",
  "u_nodata",
  "u_breaks",
  "u_width",
  "u_floor",
  ...PROJECTION_UNIFORM_NAMES,
  ...TERRAIN_UNIFORM_NAMES,
] as const;

interface ProgramInfo {
  program: WebGLProgram;
  uniforms: Record<string, WebGLUniformLocation | null>;
}

interface GroupMesh {
  /** The instances, kept so the GL objects can be rebuilt after a context
   * swap (a layer removed and added again). */
  instances: Float32Array;
  buffer: WebGLBuffer | null;
  vertexArray: WebGLVertexArrayObject | null;
  count: number;
  /** The instances' Mercator bounds, `[minX, minY, maxX, maxY]`, so a
   * terrain tile skips a row group nowhere near it. */
  bounds: [number, number, number, number];
}

/** Every segment of a row group's reaches as instances: `lines[i]` is the
 * reach `rowStart + i`, interleaved `[lon, lat, …]`. */
export function segmentInstances(rowStart: number, lines: readonly Float64Array[]): Float32Array {
  let segments = 0;
  for (const line of lines) segments += Math.max(0, line.length / 2 - 1);
  const out = new Float32Array(segments * INSTANCE_FLOATS);
  let at = 0;
  lines.forEach((line, position) => {
    const feature = rowStart + position;
    let x0 = mercatorX(line[0]!);
    let y0 = mercatorY(line[1]!);
    for (let point = 1; point < line.length / 2; point += 1) {
      const x1 = mercatorX(line[point * 2]!);
      const y1 = mercatorY(line[point * 2 + 1]!);
      out[at] = x0;
      out[at + 1] = y0;
      out[at + 2] = x1;
      out[at + 3] = y1;
      out[at + 4] = feature;
      at += INSTANCE_FLOATS;
      x0 = x1;
      y0 = y1;
    }
  });
  return out;
}

/** The Mercator bounds of a row group's segments, `[minX, minY, maxX,
 * maxY]` (empty, min above max, when there are none). */
export function instanceBounds(instances: Float32Array): [number, number, number, number] {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let at = 0; at < instances.length; at += INSTANCE_FLOATS) {
    for (const offset of [0, 2]) {
      const x = instances[at + offset]!;
      const y = instances[at + offset + 1]!;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }
  return [minX, minY, maxX, maxY];
}

/** Whether bounds meet a terrain tile, given as its Mercator origin (wrap
 * included, as `SurfaceTile` carries it) and side. */
export function boundsMeetTile(bounds: readonly number[], x: number, y: number, size: number): boolean {
  const originX = x - Math.floor(x);
  return bounds[0]! <= originX + size && bounds[2]! >= originX && bounds[1]! <= y + size && bounds[3]! >= y;
}

/** The value texture's texels: flow (negative for no value) and usual flow
 * (0 where unknown, which the floor then stands in for). */
export function packValues(flow: Float32Array, baseflow: Float32Array | null, out: Float32Array): Float32Array {
  for (let feature = 0; feature < flow.length; feature += 1) {
    const value = flow[feature]!;
    const usual = baseflow?.[feature] ?? Number.NaN;
    out[feature * 2] = Number.isFinite(value) ? value : -1;
    out[feature * 2 + 1] = Number.isFinite(usual) ? usual : 0;
  }
  return out;
}

export class RiverLayer implements CustomLayerInterface {
  readonly id = RIVER_LAYER_ID;
  readonly type = "custom" as const;
  readonly renderingMode = "2d" as const;

  private map: MaplibreMap | null = null;
  private gl: WebGL2RenderingContext | null = null;
  private programs = new Map<string, ProgramInfo>();
  private corners: WebGLBuffer | null = null;
  private texture: WebGLTexture | null = null;
  private readonly groups = new Map<number, GroupMesh>();
  private readonly texels: Float32Array;
  private readonly textureRows: number;
  private baseflow: Float32Array | null = null;
  /** Whether the texels changed since the last upload. */
  private dirty = true;
  /** Nothing is drawn before a track's first frame, or while off. */
  private visible = false;
  private readonly colors = new Float32Array(riverClassColors().flat());

  constructor(private readonly featureCount: number) {
    this.textureRows = Math.max(1, Math.ceil(featureCount / VALUE_TEXTURE_WIDTH));
    this.texels = new Float32Array(VALUE_TEXTURE_WIDTH * this.textureRows * 2).fill(-1);
  }

  onAdd(map: MaplibreMap, gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    this.map = map;
    this.gl = gl as WebGL2RenderingContext;
    this.programs.clear();
    this.corners = null;
    this.texture = null;
    for (const mesh of this.groups.values()) {
      mesh.buffer = null;
      mesh.vertexArray = null;
    }
    this.dirty = true;
  }

  onRemove(): void {
    const gl = this.gl;
    if (gl) {
      for (const mesh of this.groups.values()) {
        if (mesh.buffer) gl.deleteBuffer(mesh.buffer);
        if (mesh.vertexArray) gl.deleteVertexArray(mesh.vertexArray);
        mesh.buffer = null;
        mesh.vertexArray = null;
      }
      if (this.corners) gl.deleteBuffer(this.corners);
      if (this.texture) gl.deleteTexture(this.texture);
      for (const info of this.programs.values()) gl.deleteProgram(info.program);
    }
    this.programs.clear();
    this.corners = null;
    this.texture = null;
    this.gl = null;
    this.map = null;
  }

  /** Whether a row group's reaches are already uploaded. */
  hasGroup(index: number): boolean {
    return this.groups.has(index);
  }

  /** Upload a row group's reaches once. */
  setGroup(index: number, rowStart: number, lines: readonly Float64Array[]): void {
    const instances = segmentInstances(rowStart, lines);
    this.groups.set(index, {
      instances,
      buffer: null,
      vertexArray: null,
      count: instances.length / INSTANCE_FLOATS,
      bounds: instanceBounds(instances),
    });
    this.map?.triggerRepaint();
  }

  /** Each reach's usual flow, once per track. */
  setBaseflow(baseflow: Float32Array | null): void {
    this.baseflow = baseflow;
  }

  /** The flows on screen, one per feature (NaN for no value), or null to
   * draw nothing (before the track's first frame). */
  setValues(flow: Float32Array | null): void {
    this.visible = flow !== null;
    if (flow) {
      packValues(flow.subarray(0, this.featureCount), this.baseflow, this.texels);
      this.dirty = true;
    }
    this.map?.triggerRepaint();
  }

  /** The program per projection variant and terrain mode. */
  private programFor(gl: WebGL2RenderingContext, args: CustomRenderMethodInput, terrain: boolean): ProgramInfo {
    const key = `${args.shaderData.variantName}:${terrain ? "terrain" : "flat"}`;
    const cached = this.programs.get(key);
    if (cached) return cached;
    const defines = terrain ? ["XUE_TERRAIN"] : [];
    const program = gl.createProgram();
    for (const [kind, source] of [
      [gl.VERTEX_SHADER, withDefines(vertexShader(projectionPrelude(args)), defines)],
      [gl.FRAGMENT_SHADER, withDefines(FRAGMENT_SHADER, defines)],
    ] as const) {
      const shader = gl.createShader(kind);
      if (!shader) throw new Error("river: failed to create shader");
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(`river shader compile failed: ${gl.getShaderInfoLog(shader) ?? "unknown"}`);
      }
      gl.attachShader(program, shader);
    }
    gl.bindAttribLocation(program, CORNER_ATTRIBUTE, "a_corner");
    gl.bindAttribLocation(program, START_ATTRIBUTE, "a_start");
    gl.bindAttribLocation(program, END_ATTRIBUTE, "a_end");
    gl.bindAttribLocation(program, FEATURE_ATTRIBUTE, "a_feature");
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`river program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
    }
    const uniforms: Record<string, WebGLUniformLocation | null> = {};
    for (const name of UNIFORM_NAMES) uniforms[name] = gl.getUniformLocation(program, name);
    const info = { program, uniforms };
    this.programs.set(key, info);
    return info;
  }

  private ensureCorners(gl: WebGL2RenderingContext): WebGLBuffer {
    if (this.corners) return this.corners;
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    // A strip across the segment: start left, start right, end left, end right.
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, -1, 0, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    this.corners = buffer;
    return buffer;
  }

  private ensureMesh(gl: WebGL2RenderingContext, mesh: GroupMesh): WebGLVertexArrayObject {
    if (mesh.vertexArray) return mesh.vertexArray;
    const corners = this.ensureCorners(gl);
    const vertexArray = gl.createVertexArray();
    gl.bindVertexArray(vertexArray);
    gl.bindBuffer(gl.ARRAY_BUFFER, corners);
    gl.enableVertexAttribArray(CORNER_ATTRIBUTE);
    gl.vertexAttribPointer(CORNER_ATTRIBUTE, 2, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(CORNER_ATTRIBUTE, 0);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, mesh.instances, gl.STATIC_DRAW);
    const stride = INSTANCE_FLOATS * 4;
    for (const [location, size, offset] of [
      [START_ATTRIBUTE, 2, 0],
      [END_ATTRIBUTE, 2, 8],
      [FEATURE_ATTRIBUTE, 1, 16],
    ] as const) {
      gl.enableVertexAttribArray(location);
      gl.vertexAttribPointer(location, size, gl.FLOAT, false, stride, offset);
      gl.vertexAttribDivisor(location, 1);
    }
    gl.bindVertexArray(null);
    mesh.buffer = buffer;
    mesh.vertexArray = vertexArray;
    return vertexArray;
  }

  private ensureTexture(gl: WebGL2RenderingContext): WebGLTexture {
    if (!this.texture) {
      this.texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      this.dirty = true;
    }
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    if (this.dirty) {
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG32F, VALUE_TEXTURE_WIDTH, this.textureRows, 0, gl.RG, gl.FLOAT, this.texels);
      this.dirty = false;
    }
    return this.texture;
  }

  render(glContext: WebGLRenderingContext | WebGL2RenderingContext, args: unknown): void {
    const gl = this.gl;
    const input = args as CustomRenderMethodInput;
    if (!gl || glContext !== gl || !this.visible || this.groups.size === 0) return;
    if (!input?.shaderData || !input.defaultProjectionData || !this.map) return;
    const tiles = surfaceTiles(this.map, input);
    const info = this.programFor(gl, input, tiles !== null);
    gl.useProgram(info.program);
    gl.activeTexture(gl.TEXTURE0);
    this.ensureTexture(gl);
    const u = info.uniforms;
    gl.uniform1i(u.u_values!, 0);
    gl.uniform2f(u.u_viewport!, gl.drawingBufferWidth, gl.drawingBufferHeight);
    gl.uniform4fv(u.u_colors!, this.colors);
    gl.uniform4fv(u.u_nodata!, NODATA_COLOR);
    gl.uniform3f(u.u_breaks!, RIVER_RATIO_BREAKS[0], RIVER_RATIO_BREAKS[1], RIVER_RATIO_BREAKS[2]);
    gl.uniform2f(u.u_width!, RIVER_WIDTH_MIN_PX, RIVER_WIDTH_MAX_PX);
    gl.uniform1f(u.u_floor!, RIVER_BASEFLOW_FLOOR);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    if (tiles) {
      for (const tile of tiles) {
        bindSurfaceTile(gl, u, tile, DEM_TEXTURE_UNIT, null, DEPTH_TEXTURE_UNIT);
        for (const mesh of this.groups.values()) {
          if (mesh.count === 0 || !boundsMeetTile(mesh.bounds, tile.x, tile.y, tile.size)) continue;
          gl.bindVertexArray(this.ensureMesh(gl, mesh));
          gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, mesh.count);
        }
      }
    } else {
      setProjectionUniforms(gl, u, input.defaultProjectionData);
      // Three world copies on the plane, one on the globe, as the field.
      const offsets = isGlobe(input) ? [0] : [-1, 0, 1];
      for (const mesh of this.groups.values()) {
        if (mesh.count === 0) continue;
        gl.bindVertexArray(this.ensureMesh(gl, mesh));
        for (const offset of offsets) {
          gl.uniform4f(u.u_tile!, offset, 0, 1, 0);
          gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, mesh.count);
        }
      }
    }
    gl.bindVertexArray(null);
    gl.activeTexture(gl.TEXTURE0);
  }
}
