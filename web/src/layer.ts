import type { CustomLayerInterface, Map as MaplibreMap } from "maplibre-gl";

import { t } from "./i18n";
import type { BundleMetadata } from "./manifest";
import { WHOLE_PLANE_COVERAGE, type CoverageBox } from "./tiles";

/**
 * MapLibre custom layer that renders one quantized R8 forecast plane on the
 * shared WebGL2 context. The fragment shader applies inverse Web Mercator,
 * converts longitude and latitude to grid coordinates, samples the R8 data
 * texture, and colors codes through a 256x1 palette texture.
 *
 * Two data textures are held: slot A carries the displayed frame and slot B
 * the following one, and `u_mix` blends the two reconstructed CODE fields
 * before the palette lookup (WeatherLayers
 * `imageWeight` style). Scrubbing shows a single frame (mix 0); playback
 * sweeps mix 0->1 between frames so 12 fps reads as continuous motion and
 * streaming arrival jitter is masked. No raster opacity is ever animated.
 *
 * One layer also draws a two-channel field: the 10 m wind arrives as a u
 * plane and a v plane, and what the palette colors is their magnitude. That
 * is the same projection, the same blend and the same coverage clip, so it is
 * a mode of this layer rather than a second one — the data texture becomes
 * RG8 (u codes in red, v in green, the interleaving the particle layer
 * already builds), each channel is reconstructed on its own, and the palette
 * is looked up by speed / `maxMagnitude` instead of by code. Every scalar
 * field takes the single-channel path exactly as before.
 *
 * A plane need not be whole. A container v2 bundle can be decoded for just
 * the tiles a viewport covers, and everything outside them is stale bytes
 * from whatever the decoder held before — so `u_cover` names the part of the
 * texture that is real and the shader discards the rest. Showing nothing
 * there is the honest answer: the alternative is painting another frame's
 * data. A whole plane sets the box to the whole texture, which passes every
 * sample and costs one comparison.
 *
 * The pressure family is drawn as contour lines from the same plane, and a
 * contour is where 8-bit quantization shows: sea level pressure is stored in
 * 1 hPa codes and drawn every 4 hPa, so in a weak gradient one code covers
 * several cells and a line traced through the raw codes walks the edges of
 * those plateaus as a staircase, with specks wherever the field sits on a
 * code boundary. The real field is smooth, and a local mean over a few cells
 * recovers what the quantization threw away. So when contours are on, the
 * layer runs a separable Gaussian over each uploaded plane in *grid* space —
 * an offscreen pass in `prerender`, once per plane rather than per pixel —
 * into a 16-bit fixed-point RG8 texture, and the contour shader samples that
 * instead of the codes. The pass renormalises its kernel by coverage, so a
 * partial plane's edge and the poles get a one-sided mean rather than a bleed
 * of stale texels. Filled fields never touch it; the probe still reads the
 * original codes.
 */

// Exported so a headless WebGL2 test can compile and sample the real shader:
// the antimeridian seam this clips at is a pixel-level property no
// application-level test sees.
export const VERTEX_SHADER = `#version 300 es
in vec2 a_position;
uniform mat4 u_matrix;
out vec2 v_mercator;
void main() {
  v_mercator = a_position;
  gl_Position = u_matrix * vec4(a_position, 0.0, 1.0);
}`;

export const FRAGMENT_SHADER = `#version 300 es
precision highp float;
in vec2 v_mercator;
uniform sampler2D u_data;
uniform sampler2D u_data_b;
uniform sampler2D u_palette;
uniform vec2 u_first;
uniform vec2 u_step;
uniform vec2 u_size;
uniform float u_mix;
// 1.0 when the grid's columns cover the full 360 degrees. A regional grid
// (a showcase case) covers only part of the world, and every longitude
// outside it has no data to show.
uniform float u_wrap;
// The part of the data texture the plane actually filled, as
// (uStart, uEnd, vStart, vEnd). uStart > uEnd means the box wraps the
// antimeridian, which a viewport straddling it produces.
uniform vec4 u_cover;
// Contour drawing, off (interval 0) for every filled field. The pressure
// family is drawn as lines instead: u_decode turns a sampled code back into
// its physical value, u_contour is (interval, emphasis interval, half width
// in px, emphasis half width), u_contour_values holds up to four particular
// contours drawn heavy (the 5880 / 5840 gpm pair at 500 hPa) with
// u_contour_value_count saying how many are real, and u_fill_alpha scales
// the palette fill under the lines — 0 draws lines alone.
uniform vec2 u_decode;
uniform vec4 u_contour;
uniform vec4 u_contour_values;
uniform float u_contour_value_count;
uniform vec4 u_line_color;
uniform float u_fill_alpha;
// Magnitude mode, off (0) for every scalar field. The data texture then holds
// two fields rather than one — u codes in red, v in green — and each channel
// carries its own linear codebook: value = offset + code * 255 * scale.
// u_vector_max is the magnitude the palette's last entry stands for, and
// u_vector_nodata the codebooks' reserved no-data code as a texture value: a
// cell carrying it in either channel is painted as nothing, the way a scalar
// palette leaves its reserved entries transparent.
uniform float u_vector;
uniform vec2 u_vector_offset;
uniform vec2 u_vector_scale;
uniform float u_vector_max;
uniform float u_vector_nodata;
out vec4 out_color;
const float PI = 3.141592653589793;

// Catmull-Rom weights for the four taps around a sample at fraction t.
vec4 cubicWeights(float t) {
  float t2 = t * t;
  float t3 = t2 * t;
  return vec4(
    -0.5 * t3 + t2 - 0.5 * t,
    1.5 * t3 - 2.5 * t2 + 1.0,
    -1.5 * t3 + 2.0 * t2 + 0.5 * t,
    0.5 * t3 - 0.5 * t2
  );
}

// One texel's code. A raw plane is an R8 texture, whose green channel reads
// as zero; a smoothed plane is RG8 with the fraction of a code in green, so
// one dot product decodes both without a branch or a uniform.
float fetchCode(sampler2D data, vec2 texel) {
  return dot(texture(data, texel).rg, vec2(1.0, 1.0 / 255.0));
}

// Bicubic Catmull-Rom reconstruction of the code field. Taps sit exactly on
// texel centers, so the texture's own REPEAT/CLAMP wrap modes keep handling
// the antimeridian and the poles. Catmull-Rom rings at sharp edges, and an
// overshot code would color a peak with a rain class the data never reached,
// so the result is clamped to the value range of the central 2x2 texels —
// the same envelope bilinear filtering can produce.
float sampleCode(sampler2D data, vec2 uv) {
  vec2 position = uv * u_size - 0.5;
  vec2 base = floor(position);
  vec2 fraction = position - base;
  vec4 wx = cubicWeights(fraction.x);
  vec4 wy = cubicWeights(fraction.y);
  float code = 0.0;
  float lo = 1.0;
  float hi = 0.0;
  for (int row = 0; row < 4; row += 1) {
    float rowSum = 0.0;
    for (int column = 0; column < 4; column += 1) {
      vec2 texel = (base + vec2(float(column - 1), float(row - 1)) + 0.5) / u_size;
      float value = fetchCode(data, texel);
      rowSum += wx[column] * value;
      if (row >= 1 && row <= 2 && column >= 1 && column <= 2) {
        lo = min(lo, value);
        hi = max(hi, value);
      }
    }
    code += wy[row] * rowSum;
  }
  return clamp(code, lo, hi);
}

// The same reconstruction for a vector plane, whose two channels are two
// fields (u codes in red, v in green) rather than one code in sixteen bits.
// Each channel is filtered and clamped on its own, and the two share every
// tap: one texture read per texel serves both, which is what keeps magnitude
// mode at the scalar path's fetch count rather than double it.
//
// A reserved code is caught on the texels themselves, not on the result: a
// cell next to a no-data cell reconstructs to something between the two,
// and in mediump even a lone 255 comes back a fraction of a code short, so
// a threshold on the interpolated value would miss the very cell it is for.
// The central 2x2 is the bilinear support, and a no-data texel there makes
// the sample missing once its bilinear weight is more than a sliver — so the
// half of a data cell that faces a no-data neighbour is eroded, and the
// cell's own center still paints. The sliver is wider than the position
// error mediump makes on a production grid, or a cell center itself could
// fall on either side of it.
vec2 sampleCodes(sampler2D data, vec2 uv, out bool missing) {
  vec2 position = uv * u_size - 0.5;
  vec2 base = floor(position);
  vec2 fraction = position - base;
  vec4 wx = cubicWeights(fraction.x);
  vec4 wy = cubicWeights(fraction.y);
  vec2 codes = vec2(0.0);
  vec2 lo = vec2(1.0);
  vec2 hi = vec2(0.0);
  missing = false;
  for (int row = 0; row < 4; row += 1) {
    vec2 rowSum = vec2(0.0);
    for (int column = 0; column < 4; column += 1) {
      vec2 texel = (base + vec2(float(column - 1), float(row - 1)) + 0.5) / u_size;
      vec2 value = texture(data, texel).rg;
      rowSum += wx[column] * value;
      if (row >= 1 && row <= 2 && column >= 1 && column <= 2) {
        lo = min(lo, value);
        hi = max(hi, value);
        // An 8-bit texel is exact to well within half a code.
        float weight = (column == 1 ? 1.0 - fraction.x : fraction.x) * (row == 1 ? 1.0 - fraction.y : fraction.y);
        if (weight > 1.0 / 64.0 && abs(max(value.x, value.y) - u_vector_nodata) < 0.5 / 255.0) missing = true;
      }
    }
    codes += wy[row] * rowSum;
  }
  return clamp(codes, lo, hi);
}

// Coverage of one contour line, anti-aliased to a pixel.
//
// "distance" is how far this fragment is from the line in the field's own
// unit; dividing by the field's screen gradient turns that into pixels, so a
// line keeps its width at every zoom without a second texture or any CPU
// work. A gradient of zero means a flat neighbourhood — no line passes
// through it, whatever the value happens to be.
float lineCoverage(float distance, float gradient, float halfWidth) {
  if (gradient <= 0.0) return 0.0;
  float pixels = distance / gradient;
  return 1.0 - smoothstep(halfWidth - 0.5, halfWidth + 0.5, pixels);
}

// The whole family of contours at multiples of one interval.
//
// Where the field climbs a whole interval in a couple of pixels — a jet
// stream at a zoomed-out view — the family is finer than the screen can
// resolve, and drawing it anyway turns the gradient into a solid band. So
// the family fades out as its spacing approaches a pixel. Showing nothing
// is the honest answer, the same one u_cover gives outside the data; a
// named contour is a single line and never needs it.
float contourCoverage(float value, float gradient, float interval, float halfWidth) {
  if (interval <= 0.0 || gradient <= 0.0) return 0.0;
  float steps = value / interval;
  // Distance to the nearest multiple, back in the field's unit.
  float distance = abs(fract(steps + 0.5) - 0.5) * interval;
  float spacingPixels = interval / gradient;
  return lineCoverage(distance, gradient, halfWidth) * smoothstep(2.0, 6.0, spacingPixels);
}

void main() {
  float longitude = fract(v_mercator.x) * 360.0 - 180.0;
  float latitude = 90.0 - (360.0 / PI) * atan(exp((v_mercator.y * 2.0 - 1.0) * PI));
  // Degrees east of the grid origin, wrapped into [0, 360): a global grid
  // indexes from -180 as before, and a regional window that crosses the
  // antimeridian stays contiguous instead of splitting in two.
  float delta = mod(longitude - u_first.x, 360.0);
  float u = (delta / u_step.x + 0.5) / u_size.x;
  float v = ((latitude - u_first.y) / u_step.y + 0.5) / u_size.y;
  // Outside the grid there is no data. The pole-to-pole production grids
  // never leave the vertical range, so only a cropped grid is ever clipped;
  // without this the edge texels would smear across the whole map.
  if (v < 0.0 || v > 1.0 || (u_wrap < 0.5 && (u < 0.0 || u > 1.0))) discard;
  // The coverage test runs on the wrapped coordinate. On a global grid the
  // half-cell before the antimeridian lands just past u = 1 (the cell center
  // is half a step east of 360 degrees) and the texture's own REPEAT resolves
  // it; comparing the raw u against the box would discard that sliver and
  // leave a hairline gap down the dateline.
  float cu = u_wrap > 0.5 ? fract(u) : u;
  bool coveredU = u_cover.x <= u_cover.y
    ? (cu >= u_cover.x && cu <= u_cover.y)
    : (cu >= u_cover.x || cu <= u_cover.y);
  if (!coveredU || v < u_cover.z || v > u_cover.w) discard;
  float code = 0.0;
  vec4 color;
  if (u_vector > 0.5) {
    // u and v are reconstructed separately and only then combined, so what
    // the bicubic filter interpolates is the wind vector rather than a speed:
    // two opposing 10 m/s cells read as the calm between them, which is what
    // the field does, instead of as a uniform 10 m/s.
    // A reserved code in either channel is no wind at all, not the fastest
    // wind the codebook can spell.
    bool missing = false;
    vec2 codes = sampleCodes(u_data, vec2(u, v), missing);
    if (missing) discard;
    if (u_mix > 0.0) {
      bool missingB = false;
      vec2 codesB = sampleCodes(u_data_b, vec2(u, v), missingB);
      if (missingB) discard;
      codes = mix(codes, codesB, u_mix);
    }
    vec2 wind = u_vector_offset + codes * 255.0 * u_vector_scale;
    float speed = clamp(length(wind) / u_vector_max, 0.0, 1.0);
    color = texture(u_palette, vec2((speed * 255.0 + 0.5) / 256.0, 0.5));
  } else {
    code = sampleCode(u_data, vec2(u, v));
    if (u_mix > 0.0) {
      code = mix(code, sampleCode(u_data_b, vec2(u, v)), u_mix);
    }
    color = texture(u_palette, vec2((code * 255.0 + 0.5) / 256.0, 0.5));
  }
  if (u_contour.x > 0.0) {
    // Codes mix linearly and the pressure family's codebooks are linear, so
    // dequantizing after the frame blend is the same as blending the values.
    float value = u_decode.x + code * 255.0 * u_decode.y;
    float gradient = fwidth(value);
    float lines = contourCoverage(value, gradient, u_contour.x, u_contour.z);
    lines = max(lines, contourCoverage(value, gradient, u_contour.y, u_contour.w));
    // Four is the length of the vec4 the named contours travel in; see
    // MAX_NAMED_CONTOURS.
    for (int index = 0; index < 4; index += 1) {
      if (float(index) >= u_contour_value_count) break;
      lines = max(
        lines,
        lineCoverage(abs(value - u_contour_values[index]), gradient, u_contour.w)
      );
    }
    color.a *= u_fill_alpha;
    color = mix(color, u_line_color, lines);
  }
  out_color = vec4(color.rgb * color.a, color.a);
}`;

/** How many particular contours one level may name; the shader holds them in
 * a vec4 because two (the 5880 / 5840 pair at 500 hPa) is what a chart wants
 * and a loop over a texture would cost far more than it bought. */
export const MAX_NAMED_CONTOURS = 4;

/** The widest half-kernel the smoothing pass compiles, in cells: three
 * standard deviations of the widest smoothing a level asks for. */
export const MAX_SMOOTHING_RADIUS = 9;

// The smoothing pass: one axis of a separable Gaussian over a plane, drawn
// with the viewport set to the plane's own size so every fragment is one
// texel. The quad it draws is the same one the map pass uses; clip space
// keeps the [0, 1] part.
export const SMOOTH_VERTEX_SHADER = `#version 300 es
in vec2 a_position;
void main() {
  gl_Position = vec4(a_position * 2.0 - 1.0, 0.0, 1.0);
}`;

export const SMOOTH_FRAGMENT_SHADER = `#version 300 es
precision highp float;
uniform sampler2D u_source;
uniform vec2 u_size;
// The direction of this pass in texels: (1, 0) then (0, 1).
uniform vec2 u_axis;
uniform float u_wrap;
uniform vec4 u_cover;
// Gaussian weights by distance from the center, u_radius of them in use.
uniform int u_radius;
uniform float u_weights[${MAX_SMOOTHING_RADIUS + 1}];
out vec4 out_color;

float fetchCode(vec2 texel) {
  return dot(texture(u_source, texel).rg, vec2(1.0, 1.0 / 255.0));
}

// Whether a tap lands on real data: inside the grid (past the poles, or past
// the edge of a cropped grid, there is nothing) and inside the coverage box,
// tested on the wrapped coordinate exactly as the map pass tests it.
bool covered(vec2 uv) {
  if (uv.y < 0.0 || uv.y > 1.0) return false;
  if (u_wrap < 0.5 && (uv.x < 0.0 || uv.x > 1.0)) return false;
  float cu = u_wrap > 0.5 ? fract(uv.x) : uv.x;
  bool coveredU = u_cover.x <= u_cover.y
    ? (cu >= u_cover.x && cu <= u_cover.y)
    : (cu >= u_cover.x || cu <= u_cover.y);
  return coveredU && uv.y >= u_cover.z && uv.y <= u_cover.w;
}

// A code in [0, 1] as sixteen fixed-point bits across two channels: the
// whole codes in red, the fraction of one in green. Reading it back is the
// same dot product the map pass applies to every plane.
vec2 pack(float code) {
  float scaled = clamp(code, 0.0, 1.0) * 255.0;
  float whole = floor(scaled);
  return vec2(whole / 255.0, scaled - whole);
}

void main() {
  vec2 texel = floor(gl_FragCoord.xy);
  float sum = 0.0;
  float weight = 0.0;
  for (int offset = -${MAX_SMOOTHING_RADIUS}; offset <= ${MAX_SMOOTHING_RADIUS}; offset += 1) {
    if (abs(offset) > u_radius) continue;
    vec2 uv = (texel + float(offset) * u_axis + 0.5) / u_size;
    if (!covered(uv)) continue;
    float w = u_weights[abs(offset)];
    sum += w * fetchCode(uv);
    weight += w;
  }
  // Renormalising by the weight that landed makes the mean one-sided at an
  // edge instead of pulling it toward whatever lies beyond. A texel outside
  // the coverage gathers nothing and is discarded by the map pass anyway.
  out_color = vec4(pack(weight > 0.0 ? sum / weight : 0.0), 0.0, 1.0);
}`;

/** Contour drawing for one variable, all of it in the variable's own physical
 * unit. Widths are half-widths in device pixels. */
export interface ContourStyle {
  /** Dequantization: `value = offset + code * scale`, straight off the
   * bundle's linear codebook. */
  offset: number;
  scale: number;
  /** Ordinary contour interval; 0 turns contour drawing off entirely. */
  interval: number;
  /** A regular sub-family drawn heavier (every 20 hPa on a surface chart), or
   * 0 for none. */
  emphasisInterval: number;
  /** Particular contours drawn heavier, at most MAX_NAMED_CONTOURS of them. */
  values: readonly number[];
  lineWidth: number;
  emphasisWidth: number;
  lineColor: readonly [number, number, number, number];
  /** Opacity of the palette fill beneath the lines; 0 draws lines alone. */
  fillAlpha: number;
  /** Standard deviation, in grid cells, of the Gaussian the plane is
   * smoothed with before contouring; 0 contours the raw codes. */
  smoothing: number;
}

/** A two-channel field drawn as its magnitude: the 10 m wind, whose plane
 * carries the u codes in red and the v codes in green. Each channel has its
 * own linear codebook, and `maxMagnitude` is the value the palette's last
 * entry stands for — the palette is indexed by magnitude / maxMagnitude
 * rather than by code, so it is a ramp in the field's own unit. */
export interface VectorField {
  /** Dequantization per channel: `value = offset + code * scale`, straight
   * off each component's linear codebook. */
  offset: readonly [number, number];
  scale: readonly [number, number];
  /** The reserved no-data code (the same in both channels); a cell carrying
   * it in either is not painted. */
  nodataCode: number;
  maxMagnitude: number;
}

/** The two textures one frame slot owns, and what each currently holds. The
 * raw plane is what the decoder produced; the smoothed one is derived from
 * it by the prerender pass, and is only as current as its bookkeeping says. */
interface FrameSlot {
  raw: WebGLTexture;
  smooth: WebGLTexture;
  /** Plane uploaded to `raw`, compared by identity so redundant per-rAF
   * uploads are skipped during blend sweeps. */
  plane: Uint8Array | null;
  /** Plane `smooth` was built from, and the coverage and kernel it was built
   * with; a mismatch on either means the pass has to run again. */
  smoothedPlane: Uint8Array | null;
  smoothedKey: string;
}

/** A Gaussian's weights out to three standard deviations, unnormalised: the
 * pass divides by the weight that actually lands. */
export function gaussianWeights(sigma: number): number[] {
  const radius = Math.min(MAX_SMOOTHING_RADIUS, Math.ceil(3 * sigma));
  const weights: number[] = [];
  for (let offset = 0; offset <= radius; offset += 1) {
    weights.push(Math.exp(-(offset * offset) / (2 * sigma * sigma)));
  }
  return weights;
}

export class ForecastLayer implements CustomLayerInterface {
  readonly type = "custom" as const;
  readonly renderingMode = "2d" as const;

  private map: MaplibreMap | null = null;
  private gl: WebGL2RenderingContext | null = null;
  private program: WebGLProgram | null = null;
  private vertexArray: WebGLVertexArrayObject | null = null;
  /** Slot A carries the displayed frame, slot B the following one. */
  private slots: [FrameSlot, FrameSlot] | null = null;
  private paletteTexture: WebGLTexture | null = null;
  private uniforms: Record<string, WebGLUniformLocation | null> = {};
  // The smoothing pass: its program, the texture the horizontal pass writes
  // and the vertical one reads, and the framebuffer both draw through.
  private smoothProgram: WebGLProgram | null = null;
  private smoothUniforms: Record<string, WebGLUniformLocation | null> = {};
  private scratchTexture: WebGLTexture | null = null;
  private framebuffer: WebGLFramebuffer | null = null;
  /** Grid size the smoothed and scratch textures were last allocated for. */
  private smoothSize: [number, number] = [0, 0];
  private mixWeight = 0;
  /** The part of the texture the displayed plane actually filled. */
  private coverage: CoverageBox = WHOLE_PLANE_COVERAGE;
  /** Contour drawing, off by default: every filled field renders exactly as
   * it did before this existed. */
  private contours: ContourStyle | null = null;
  /** Magnitude mode, off by default: a plane is one code per cell unless a
   * vector field says otherwise. */
  private vector: VectorField | null = null;

  private width = 0;
  private height = 0;
  private firstLongitude = -180;
  private firstLatitude = 90;
  private longitudeStep = 0.25;
  private latitudeStep = -0.25;
  /** Whether the columns cover the full 360 degrees. Drives both the
   * horizontal texture wrap mode and the shader's out-of-grid clip. */
  private wraps = true;
  private hasFrame = false;
  /** Hidden while no weather layer is on screen. */
  private visible = true;

  // Pending state survives context loss and is re-applied in onAdd.
  private pendingPlaneA: Uint8Array | null = null;
  private pendingPlaneB: Uint8Array | null = null;
  private pendingPalette: Uint8Array | null = null;

  /** One instance per raster slot on the map — the filled field and the
   * contour lines are two planes from two bundles — so the MapLibre layer
   * id is the caller's to name. */
  constructor(
    private readonly onUnsupported: (message: string) => void,
    readonly id: string = "forecast-plane",
  ) {}

  configureGrid(metadata: BundleMetadata): void {
    const grid = metadata.grid as Record<string, number | boolean>;
    this.width = (grid.width as number) ?? 0;
    this.height = (grid.height as number) ?? 0;
    this.firstLongitude = (grid.firstLongitude as number) ?? -180;
    this.firstLatitude = (grid.firstLatitude as number) ?? 90;
    this.longitudeStep = (grid.longitudeStep as number) ?? 0.25;
    this.latitudeStep = (grid.latitudeStep as number) ?? -0.25;
    this.wraps = (grid.wrapLongitude as boolean) ?? Math.abs(this.width * this.longitudeStep - 360) < 1e-6;
    this.hasFrame = false;
    // Texture dimensions changed; every plane must be re-uploaded.
    this.forgetPlanes();
    this.coverage = WHOLE_PLANE_COVERAGE;
    this.pendingPlaneA = null;
    this.pendingPlaneB = null;
    this.mixWeight = 0;
  }

  onAdd(map: MaplibreMap, gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    if (!(gl instanceof WebGL2RenderingContext)) {
      this.onUnsupported(t("webglUnavailable"));
      return;
    }
    this.map = map;
    this.gl = gl;
    const program = buildProgram(gl, VERTEX_SHADER, FRAGMENT_SHADER);
    this.program = program;
    for (const name of [
      "u_matrix", "u_data", "u_data_b", "u_palette", "u_first", "u_step", "u_size",
      "u_mix", "u_wrap", "u_cover", "u_decode", "u_contour", "u_contour_values",
      "u_contour_value_count", "u_line_color", "u_fill_alpha",
      "u_vector", "u_vector_offset", "u_vector_scale", "u_vector_max", "u_vector_nodata",
    ]) {
      this.uniforms[name] = gl.getUniformLocation(program, name);
    }
    const smoothProgram = buildProgram(gl, SMOOTH_VERTEX_SHADER, SMOOTH_FRAGMENT_SHADER);
    this.smoothProgram = smoothProgram;
    for (const name of ["u_source", "u_size", "u_axis", "u_wrap", "u_cover", "u_radius", "u_weights"]) {
      this.smoothUniforms[name] = gl.getUniformLocation(smoothProgram, name);
    }

    // One quad spanning three world copies so wrapped views stay covered.
    // Both programs bind a_position to attribute 0, so the smoothing pass
    // draws the same array; its clip space keeps the middle copy.
    this.vertexArray = gl.createVertexArray();
    gl.bindVertexArray(this.vertexArray);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, 0, 2, 0, -1, 1, -1, 1, 2, 0, 2, 1]),
      gl.STATIC_DRAW,
    );
    gl.enableVertexAttribArray(POSITION_ATTRIBUTE);
    gl.vertexAttribPointer(POSITION_ATTRIBUTE, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    this.slots = [this.createSlot(gl), this.createSlot(gl)];
    this.scratchTexture = this.createDataTexture(gl);
    this.framebuffer = gl.createFramebuffer();
    this.smoothSize = [0, 0];
    this.paletteTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);

    this.hasFrame = false;
    if (this.pendingPalette) this.setPalette(this.pendingPalette);
    if (this.pendingPlaneA) {
      this.setBlend(this.pendingPlaneA, this.pendingPlaneB, this.mixWeight, this.coverage);
    }
  }

  private createSlot(gl: WebGL2RenderingContext): FrameSlot {
    return {
      raw: this.createDataTexture(gl),
      smooth: this.createDataTexture(gl),
      plane: null,
      smoothedPlane: null,
      smoothedKey: "",
    };
  }

  /** Forget what the slots hold, so every plane is uploaded and smoothed
   * afresh: after a grid change, and after the context comes back. */
  private forgetPlanes(): void {
    for (const slot of this.slots ?? []) {
      slot.plane = null;
      slot.smoothedPlane = null;
      slot.smoothedKey = "";
    }
  }

  private createDataTexture(gl: WebGL2RenderingContext): WebGLTexture {
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    // A global grid's columns cover the full 360 degrees, so REPEAT blends
    // across the antimeridian; a cropped grid's do not, and uploadPlane
    // switches S to CLAMP_TO_EDGE for it. Rows end at the poles either way,
    // so T always clamps.
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return texture;
  }

  onRemove(): void {
    this.gl = null;
    this.map = null;
    this.program = null;
    this.smoothProgram = null;
    this.slots = null;
    this.scratchTexture = null;
    this.framebuffer = null;
    this.paletteTexture = null;
  }

  setVisible(visible: boolean): void {
    if (this.visible === visible) return;
    this.visible = visible;
    this.map?.triggerRepaint();
  }

  /** Upload a palette (256x1 RGBA). */
  setPalette(palette: Uint8Array): void {
    this.pendingPalette = palette;
    const gl = this.gl;
    if (!gl || !this.paletteTexture) return;
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 1, gl.RGBA, gl.UNSIGNED_BYTE, palette);
    this.map?.triggerRepaint();
  }

  /** Draw this plane as contour lines instead of (or over) a filled field,
   * or pass null to go back to plain fill. The style carries the variable's
   * own dequantization, so the layer never needs the bundle metadata for it.
   */
  setContours(style: ContourStyle | null): void {
    this.contours = style;
    this.map?.triggerRepaint();
  }

  /** Draw planes as the magnitude of two interleaved channels instead of as
   * single codes, or pass null to go back to the scalar path. The mode
   * changes the data texture's format, so the slots forget what they hold:
   * the caller feeds the next plane in the new shape.
   *
   * Between the two calls there is nothing valid to draw — an RG plane read
   * as R8 is half a world of noise — so the layer holds its fire until that
   * plane arrives, the same as it does before the first frame of a session. */
  setVectorField(field: VectorField | null): void {
    if (this.vector === field) return;
    const formatChanged = (this.vector === null) !== (field === null);
    this.vector = field;
    if (formatChanged) {
      this.forgetPlanes();
      this.hasFrame = false;
    }
    this.map?.triggerRepaint();
  }

  /** Show a single plane (slot A, blend weight 0). Interleaved RG bytes while
   * a vector field is set, one code per cell otherwise. */
  setFrame(plane: Uint8Array, coverage: CoverageBox = WHOLE_PLANE_COVERAGE): void {
    this.setBlend(plane, null, 0, coverage);
  }

  /** Show `mix`-weighted blend between plane A (slot A) and plane B (slot B).
   * Uploads are skipped when a slot already holds the given plane, so calling
   * this every animation frame with a sweeping weight is cheap. */
  setBlend(
    planeA: Uint8Array,
    planeB: Uint8Array | null,
    mix: number,
    coverage: CoverageBox = WHOLE_PLANE_COVERAGE,
  ): void {
    this.pendingPlaneA = planeA;
    this.pendingPlaneB = planeB;
    this.mixWeight = planeB ? Math.min(1, Math.max(0, mix)) : 0;
    this.coverage = coverage;
    const gl = this.gl;
    if (!gl || !this.slots || !this.width || !this.height) return;
    // A frame step promotes the upcoming plane to the current one; swap the
    // slots so the promotion costs a pointer flip, not a re-upload of
    // megabytes of texels (and a re-smoothing) inside one animation frame.
    if (this.slots[0].plane !== planeA && this.slots[1].plane === planeA) {
      this.slots = [this.slots[1], this.slots[0]];
    }
    this.uploadPlane(gl, this.slots[0], planeA);
    if (planeB) this.uploadPlane(gl, this.slots[1], planeB);
    this.hasFrame = true;
    this.map?.triggerRepaint();
  }

  private uploadPlane(gl: WebGL2RenderingContext, slot: FrameSlot, plane: Uint8Array): void {
    if (slot.plane === plane) return;
    gl.bindTexture(gl.TEXTURE_2D, slot.raw);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, this.wraps ? gl.REPEAT : gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    // Two bytes per cell in magnitude mode (u, v), one otherwise.
    const [internal, format] = this.vector ? [gl.RG8, gl.RG] : [gl.R8, gl.RED];
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, this.width, this.height, 0, format, gl.UNSIGNED_BYTE, plane);
    slot.plane = plane;
  }

  /** What the smoothing pass would be built from right now, or null when
   * contours are off or unsmoothed. Coverage is part of it because the
   * kernel is renormalised against the box: a plane re-covered by a wider
   * decode has different edges. */
  private smoothingKey(): string | null {
    const sigma = this.contours?.smoothing ?? 0;
    if (!this.contours || this.contours.interval <= 0 || sigma <= 0) return null;
    const box = this.coverage;
    return `${sigma}|${box.uStart},${box.uEnd},${box.vStart},${box.vEnd}`;
  }

  /** The smoothing pass, run by the map before the layers draw. It costs one
   * plane-sized draw per axis per slot, and only for a slot whose raw plane
   * (or coverage, or kernel) changed since it last ran, which during
   * playback is one slot per frame step. */
  prerender(gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    if (!(gl instanceof WebGL2RenderingContext)) return;
    if (!this.visible || !this.hasFrame || !this.slots || !this.smoothProgram || !this.framebuffer) return;
    const key = this.smoothingKey();
    if (key === null) return;
    const stale = this.slots.filter(
      (slot) => slot.plane && (slot.smoothedPlane !== slot.plane || slot.smoothedKey !== key),
    );
    if (stale.length === 0) return;

    if (this.smoothSize[0] !== this.width || this.smoothSize[1] !== this.height) {
      for (const texture of [this.scratchTexture!, this.slots[0].smooth, this.slots[1].smooth]) {
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, this.wraps ? gl.REPEAT : gl.CLAMP_TO_EDGE);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG8, this.width, this.height, 0, gl.RG, gl.UNSIGNED_BYTE, null);
      }
      this.smoothSize = [this.width, this.height];
    }

    const previousFramebuffer = gl.getParameter(gl.FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
    const previousViewport = gl.getParameter(gl.VIEWPORT) as Int32Array;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.framebuffer);
    gl.viewport(0, 0, this.width, this.height);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.STENCIL_TEST);
    gl.disable(gl.SCISSOR_TEST);
    gl.useProgram(this.smoothProgram);
    gl.bindVertexArray(this.vertexArray);
    const weights = gaussianWeights(this.contours!.smoothing);
    gl.uniform2f(this.smoothUniforms.u_size!, this.width, this.height);
    gl.uniform1f(this.smoothUniforms.u_wrap!, this.wraps ? 1 : 0);
    gl.uniform4f(
      this.smoothUniforms.u_cover!,
      this.coverage.uStart,
      this.coverage.uEnd,
      this.coverage.vStart,
      this.coverage.vEnd,
    );
    gl.uniform1i(this.smoothUniforms.u_radius!, weights.length - 1);
    const padded = new Float32Array(MAX_SMOOTHING_RADIUS + 1);
    padded.set(weights);
    gl.uniform1fv(this.smoothUniforms.u_weights!, padded);
    gl.uniform1i(this.smoothUniforms.u_source!, 0);
    gl.activeTexture(gl.TEXTURE0);
    for (const slot of stale) {
      // Rows first into the scratch texture, then columns into the slot's own.
      for (const [source, target, axis] of [
        [slot.raw, this.scratchTexture!, [1, 0]],
        [this.scratchTexture!, slot.smooth, [0, 1]],
      ] as const) {
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, target, 0);
        gl.bindTexture(gl.TEXTURE_2D, source);
        gl.uniform2f(this.smoothUniforms.u_axis!, axis[0], axis[1]);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      }
      slot.smoothedPlane = slot.plane;
      slot.smoothedKey = key;
    }
    gl.bindVertexArray(null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, previousFramebuffer);
    gl.viewport(previousViewport[0]!, previousViewport[1]!, previousViewport[2]!, previousViewport[3]!);
  }

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: unknown): void {
    if (!(gl instanceof WebGL2RenderingContext)) return;
    if (!this.visible || !this.program || !this.slots || !this.hasFrame || !this.pendingPalette) return;
    const matrix = extractMatrix(args);
    if (!matrix) return;

    // A slot reads through its smoothed texture only when that is current;
    // prerender runs first in the same frame, so it is, but a raw plane is
    // the right fallback rather than a stale one.
    const key = this.smoothingKey();
    const textureOf = (slot: FrameSlot): WebGLTexture =>
      key !== null && slot.smoothedPlane === slot.plane && slot.smoothedKey === key ? slot.smooth : slot.raw;

    gl.useProgram(this.program);
    gl.uniformMatrix4fv(this.uniforms.u_matrix!, false, matrix);
    gl.uniform2f(this.uniforms.u_first!, this.firstLongitude, this.firstLatitude);
    gl.uniform2f(this.uniforms.u_step!, this.longitudeStep, this.latitudeStep);
    gl.uniform2f(this.uniforms.u_size!, this.width, this.height);
    gl.uniform1f(this.uniforms.u_mix!, this.slots[1].plane ? this.mixWeight : 0);
    gl.uniform1f(this.uniforms.u_wrap!, this.wraps ? 1 : 0);
    gl.uniform4f(
      this.uniforms.u_cover!,
      this.coverage.uStart,
      this.coverage.uEnd,
      this.coverage.vStart,
      this.coverage.vEnd,
    );
    const contours = this.contours;
    gl.uniform2f(this.uniforms.u_decode!, contours?.offset ?? 0, contours?.scale ?? 0);
    gl.uniform4f(
      this.uniforms.u_contour!,
      contours?.interval ?? 0,
      contours?.emphasisInterval ?? 0,
      contours?.lineWidth ?? 0,
      contours?.emphasisWidth ?? 0,
    );
    const values = contours?.values ?? [];
    gl.uniform4f(
      this.uniforms.u_contour_values!,
      values[0] ?? 0,
      values[1] ?? 0,
      values[2] ?? 0,
      values[3] ?? 0,
    );
    gl.uniform1f(this.uniforms.u_contour_value_count!, Math.min(values.length, MAX_NAMED_CONTOURS));
    const line = contours?.lineColor ?? [1, 1, 1, 1];
    gl.uniform4f(this.uniforms.u_line_color!, line[0], line[1], line[2], line[3]);
    gl.uniform1f(this.uniforms.u_fill_alpha!, contours?.fillAlpha ?? 1);
    const vector = this.vector;
    gl.uniform1f(this.uniforms.u_vector!, vector ? 1 : 0);
    gl.uniform2f(this.uniforms.u_vector_offset!, vector?.offset[0] ?? 0, vector?.offset[1] ?? 0);
    gl.uniform2f(this.uniforms.u_vector_scale!, vector?.scale[0] ?? 0, vector?.scale[1] ?? 0);
    // Never zero: it divides the magnitude.
    gl.uniform1f(this.uniforms.u_vector_max!, vector?.maxMagnitude || 1);
    // Off the code space entirely when no field is set, so nothing matches.
    gl.uniform1f(this.uniforms.u_vector_nodata!, vector ? vector.nodataCode / 255 : -1);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, textureOf(this.slots[0]));
    gl.uniform1i(this.uniforms.u_data!, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, textureOf(this.slots[1]));
    gl.uniform1i(this.uniforms.u_data_b!, 1);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.uniform1i(this.uniforms.u_palette!, 2);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this.vertexArray);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
  }
}

/** Attribute index both programs bind `a_position` to, so one vertex array
 * serves the map pass and the smoothing pass alike. */
const POSITION_ATTRIBUTE = 0;

function buildProgram(gl: WebGL2RenderingContext, vertexSource: string, fragmentSource: string): WebGLProgram {
  const program = gl.createProgram();
  for (const [kind, source] of [
    [gl.VERTEX_SHADER, vertexSource],
    [gl.FRAGMENT_SHADER, fragmentSource],
  ] as const) {
    const shader = gl.createShader(kind);
    if (!shader) throw new Error("failed to create shader");
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(`shader compile failed: ${gl.getShaderInfoLog(shader) ?? "unknown"}`);
    }
    gl.attachShader(program, shader);
  }
  gl.bindAttribLocation(program, POSITION_ATTRIBUTE, "a_position");
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(`program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
  }
  return program;
}

/** MapLibre v4 passed a mat4 directly; v5 wraps it in projection data. */
export function extractMatrix(args: unknown): Float32Array | number[] | null {
  if (Array.isArray(args) || args instanceof Float32Array || args instanceof Float64Array) {
    return args instanceof Float64Array ? new Float32Array(args) : args;
  }
  if (typeof args === "object" && args !== null) {
    const record = args as Record<string, unknown>;
    const projection = record.defaultProjectionData as Record<string, unknown> | undefined;
    const candidate = projection?.mainMatrix ?? record.modelViewProjectionMatrix ?? record.projectionMatrix;
    if (Array.isArray(candidate) || candidate instanceof Float32Array) return candidate;
    if (candidate instanceof Float64Array) return new Float32Array(candidate);
  }
  return null;
}
