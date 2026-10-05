// A radar reflectivity volume drawn as a 3D texture and raymarched: the
// `refl3d` bundle's constant-altitude levels stacked into one texture per
// frame, the ray from the camera through each pixel of the box the volume
// fills, front to back with a reflectivity transfer function.
//
// Flat Web Mercator only: the box is a Mercator box whose height is the
// altitude scaled by one exaggerated metres-to-Mercator factor, so on the
// globe the layer draws nothing (the field's own 2D layer is what a globe
// shows of it). No terrain occlusion: the volume floats over the relief
// from sea level up.

import type { CustomLayerInterface, CustomRenderMethodInput, Map as MapLibreMap } from "maplibre-gl";
import { MercatorCoordinate } from "maplibre-gl";
import type { BundleVariable, LinearQuantization } from "./manifest";
import { isGlobe } from "./projection";

/** Bundles drawn as a volume, by manifest id, with their level count: the
 * encoders' volume kind (`VOLUME_BUNDLES` in xuebuild/binconvert.py). The
 * count is what a tier is weighed by before the bundle is opened; the
 * levels themselves are read off its variables. */
export const VOLUME_BUNDLE_LEVELS: ReadonlyMap<string, number> = new Map([["refl3d", 33]]);

/** The vertical exaggeration a volume is drawn at unless the URL asks for
 * another (`?vexag=`): CONUS is 7000 km wide and its echoes reach 19 km, so
 * at true scale the volume is a film on the map. */
export const DEFAULT_VERTICAL_EXAGGERATION = 10;

/** Reflectivity below which the transfer function is clear and at which it
 * is fully dense, in dBZ: light stratiform echo reads as a haze, a
 * convective core as a solid. */
const CLEAR_DBZ = 18;
const DENSE_DBZ = 60;

/** How much one voxel of the densest echo occludes: well under one, so a
 * stratiform shield hundreds of voxels deep reads as a veil and the cores
 * inside it still show. */
const VOXEL_OPACITY = 0.2;

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
out vec4 out_color;

const float PI = 3.141592653589793;
const int MAX_STEPS = ${MAX_STEPS};

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
  // would composite the same column twice.
  if (hit < leave * (1.0 - 1e-4)) discard;
  enter = max(enter, 0.0);
  float span = leave - enter;
  if (span <= 0.0) discard;
  int steps = int(clamp(ceil(span / u_voxel), 1.0, float(MAX_STEPS)));
  float dt = span / float(steps);
  // Opacity per sample follows the step's length in voxels, so the picture
  // does not change with how many samples a ray takes.
  float stride = dt / u_voxel;
  vec4 sum = vec4(0.0);
  for (int i = 0; i < MAX_STEPS; i += 1) {
    if (i >= steps) break;
    vec3 p = u_camera + dir * (enter + (float(i) + 0.5) * dt);
    float lon = p.x * 360.0 - 180.0;
    float lat = degrees(atan(sinh(PI * (1.0 - 2.0 * p.y))));
    vec2 uv = vec2((lon - u_grid.x) / u_grid.z, (u_grid.y - lat) / u_grid.w);
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) continue;
    float altitude = p.z / u_z_scale;
    float w = texture(u_levels, vec2(altitude / u_top, 0.5)).r;
    vec3 coordinate = vec3(uv, w);
    float code = mix(texture(u_volume_a, coordinate).r, texture(u_volume_b, coordinate).r, u_mix) * 255.0;
    if (code > u_codebook.z + 0.5) continue;
    float dbz = u_codebook.x + code * u_codebook.y;
    float density = smoothstep(u_dbz.x, u_dbz.y, dbz) * u_dbz.z;
    if (density <= 0.0) continue;
    float alpha = 1.0 - pow(1.0 - min(density, 0.999), stride);
    // Brighter with height: the one depth cue a volume without lighting has.
    vec3 color = texture(u_palette, vec2((code + 0.5) / 256.0, 0.5)).rgb * (0.7 + 0.3 * w);
    sum += (1.0 - sum.a) * vec4(color * alpha, alpha);
    if (sum.a > 0.97) break;
  }
  if (sum.a <= 0.0) discard;
  out_color = sum;
}
`;

interface VolumeSlot {
  texture: WebGLTexture;
  planes: readonly Uint8Array[] | null;
}

interface VolumeGrid {
  width: number;
  height: number;
  firstLongitude: number;
  firstLatitude: number;
  longitudeStep: number;
  latitudeStep: number;
}

/** The radar volume as a MapLibre custom layer. Fed the level planes of
 * two frames and the weight between them, as the field layer is. */
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
  private exaggeration: number;
  private mixWeight = 0;
  private visible = false;
  private boxDirty = true;
  private levelsDirty = true;
  private storageDirty = true;

  constructor(
    private readonly onUnsupported: (message: string) => void,
    exaggeration = DEFAULT_VERTICAL_EXAGGERATION,
    id = "radar-volume",
  ) {
    this.id = id;
    this.exaggeration = exaggeration;
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

  setExaggeration(exaggeration: number): void {
    if (exaggeration === this.exaggeration) return;
    this.exaggeration = exaggeration;
    this.boxDirty = true;
    this.map?.triggerRepaint();
  }

  setVisible(visible: boolean): void {
    if (visible === this.visible) return;
    this.visible = visible;
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
    this.program = linkProgram(gl, VOLUME_VERTEX_SHADER, VOLUME_FRAGMENT_SHADER);
    if (!this.program) {
      this.onUnsupported("The radar volume shader failed to compile.");
      return;
    }
    for (const name of [
      "u_matrix", "u_camera", "u_box_min", "u_box_max", "u_grid", "u_z_scale", "u_top",
      "u_volume_a", "u_volume_b", "u_mix", "u_palette", "u_levels", "u_codebook", "u_dbz", "u_voxel",
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
    }
    this.slots = [];
    this.gl = null;
    this.map = null;
  }

  render(context: WebGLRenderingContext | WebGL2RenderingContext, input: CustomRenderMethodInput): void {
    const gl = context as WebGL2RenderingContext;
    if (!this.visible || !this.program || !this.grid || !this.slots[0]?.planes || !this.palette) return;
    if (isGlobe(input)) return;
    const matrix = input.defaultProjectionData.mainMatrix as ArrayLike<number>;
    const camera = cameraFromMatrix(matrix);
    if (!camera) return;
    if (this.boxDirty) this.rebuildBox(gl);
    if (this.levelsDirty) this.uploadLevels(gl);
    const box = this.box!;

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

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.depthMask(false);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this.vao);
    gl.drawElements(gl.TRIANGLES, BOX_INDICES.length, gl.UNSIGNED_SHORT, 0);
    gl.bindVertexArray(null);
    gl.activeTexture(gl.TEXTURE0);
  }

  private rebuildBox(gl: WebGL2RenderingContext): void {
    if (!this.grid || this.altitudes.length === 0) return;
    this.box = volumeBox(this.grid, volumeTop(this.altitudes), this.exaggeration);
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
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, table.length, 1, 0, gl.RED, gl.FLOAT, table);
    setLinearClamp(gl, gl.TEXTURE_2D);
    this.levelsDirty = false;
  }

  private uploadPalette(gl: WebGL2RenderingContext): void {
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 1, gl.RGBA, gl.UNSIGNED_BYTE, this.palette);
  }

  /** (Re)allocate both 3D textures for the grid, halving a dimension the
   * device cannot hold in one 3D texture. */
  private ensureStorage(gl: WebGL2RenderingContext): void {
    if (!this.storageDirty || !this.grid) return;
    const limit = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE) as number;
    let [width, height] = [this.grid.width, this.grid.height];
    this.planeSize = [width, height];
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
    gl.bindTexture(gl.TEXTURE_3D, slot.texture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    const [width, height] = this.textureSize;
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
