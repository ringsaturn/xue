import type { CustomLayerInterface, Map as MaplibreMap } from "maplibre-gl";

import { t } from "./i18n";
import type { BundleMetadata } from "./manifest";

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
 */

const VERTEX_SHADER = `#version 300 es
in vec2 a_position;
uniform mat4 u_matrix;
out vec2 v_mercator;
void main() {
  v_mercator = a_position;
  gl_Position = u_matrix * vec4(a_position, 0.0, 1.0);
}`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;
in vec2 v_mercator;
uniform sampler2D u_data;
uniform sampler2D u_data_b;
uniform sampler2D u_palette;
uniform vec2 u_first;
uniform vec2 u_step;
uniform vec2 u_size;
uniform float u_mix;
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
      float value = texture(data, texel).r;
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

void main() {
  float longitude = fract(v_mercator.x) * 360.0 - 180.0;
  float latitude = 90.0 - (360.0 / PI) * atan(exp((v_mercator.y * 2.0 - 1.0) * PI));
  float u = ((longitude - u_first.x) / u_step.x + 0.5) / u_size.x;
  float v = ((latitude - u_first.y) / u_step.y + 0.5) / u_size.y;
  float code = sampleCode(u_data, vec2(u, v));
  if (u_mix > 0.0) {
    code = mix(code, sampleCode(u_data_b, vec2(u, v)), u_mix);
  }
  vec4 color = texture(u_palette, vec2((code * 255.0 + 0.5) / 256.0, 0.5));
  out_color = vec4(color.rgb * color.a, color.a);
}`;

export class ForecastLayer implements CustomLayerInterface {
  readonly id = "forecast-plane";
  readonly type = "custom" as const;
  readonly renderingMode = "2d" as const;

  private map: MaplibreMap | null = null;
  private gl: WebGL2RenderingContext | null = null;
  private program: WebGLProgram | null = null;
  private vertexArray: WebGLVertexArrayObject | null = null;
  private dataTextures: [WebGLTexture, WebGLTexture] | null = null;
  private paletteTexture: WebGLTexture | null = null;
  private uniforms: Record<string, WebGLUniformLocation | null> = {};
  private mixWeight = 0;
  /** Planes currently uploaded to slot A / slot B, compared by identity so
   * redundant per-rAF uploads are skipped during blend sweeps. */
  private uploadedPlanes: [Uint8Array | null, Uint8Array | null] = [null, null];

  private width = 0;
  private height = 0;
  private firstLongitude = -180;
  private firstLatitude = 90;
  private longitudeStep = 0.25;
  private latitudeStep = -0.25;
  private hasFrame = false;
  /** Hidden while the wind particle layer owns the screen. */
  private visible = true;

  // Pending state survives context loss and is re-applied in onAdd.
  private pendingPlaneA: Uint8Array | null = null;
  private pendingPlaneB: Uint8Array | null = null;
  private pendingPalette: Uint8Array | null = null;

  constructor(private readonly onUnsupported: (message: string) => void) {}

  configureGrid(metadata: BundleMetadata): void {
    const grid = metadata.grid as Record<string, number>;
    this.width = grid.width ?? 0;
    this.height = grid.height ?? 0;
    this.firstLongitude = grid.firstLongitude ?? -180;
    this.firstLatitude = grid.firstLatitude ?? 90;
    this.longitudeStep = grid.longitudeStep ?? 0.25;
    this.latitudeStep = grid.latitudeStep ?? -0.25;
    this.hasFrame = false;
    // Texture dimensions changed; every plane must be re-uploaded.
    this.uploadedPlanes = [null, null];
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
    const program = gl.createProgram();
    for (const [kind, source] of [
      [gl.VERTEX_SHADER, VERTEX_SHADER],
      [gl.FRAGMENT_SHADER, FRAGMENT_SHADER],
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
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
    }
    this.program = program;
    for (const name of ["u_matrix", "u_data", "u_data_b", "u_palette", "u_first", "u_step", "u_size", "u_mix"]) {
      this.uniforms[name] = gl.getUniformLocation(program, name);
    }

    // One quad spanning three world copies so wrapped views stay covered.
    this.vertexArray = gl.createVertexArray();
    gl.bindVertexArray(this.vertexArray);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, 0, 2, 0, -1, 1, -1, 1, 2, 0, 2, 1]),
      gl.STATIC_DRAW,
    );
    const positionLocation = gl.getAttribLocation(program, "a_position");
    gl.enableVertexAttribArray(positionLocation);
    gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    this.dataTextures = [this.createDataTexture(gl), this.createDataTexture(gl)];
    this.paletteTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.paletteTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);

    this.hasFrame = false;
    this.uploadedPlanes = [null, null];
    if (this.pendingPalette) this.setPalette(this.pendingPalette);
    if (this.pendingPlaneA) this.setBlend(this.pendingPlaneA, this.pendingPlaneB, this.mixWeight);
  }

  private createDataTexture(gl: WebGL2RenderingContext): WebGLTexture {
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    // 1440 columns cover the full 360 degrees, so REPEAT blends across the
    // antimeridian; rows end exactly at the poles, so T clamps.
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return texture;
  }

  onRemove(): void {
    this.gl = null;
    this.map = null;
    this.program = null;
    this.dataTextures = null;
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

  /** Show a single plane (slot A, blend weight 0). */
  setFrame(plane: Uint8Array): void {
    this.setBlend(plane, null, 0);
  }

  /** Show `mix`-weighted blend between plane A (slot A) and plane B (slot B).
   * Uploads are skipped when a slot already holds the given plane, so calling
   * this every animation frame with a sweeping weight is cheap. */
  setBlend(planeA: Uint8Array, planeB: Uint8Array | null, mix: number): void {
    this.pendingPlaneA = planeA;
    this.pendingPlaneB = planeB;
    this.mixWeight = planeB ? Math.min(1, Math.max(0, mix)) : 0;
    const gl = this.gl;
    if (!gl || !this.dataTextures || !this.width || !this.height) return;
    // A frame step promotes the upcoming plane to the current one; swap the
    // texture slots so the promotion costs a pointer flip, not a re-upload of
    // megabytes of texels inside one animation frame.
    if (this.uploadedPlanes[0] !== planeA && this.uploadedPlanes[1] === planeA) {
      this.dataTextures = [this.dataTextures[1]!, this.dataTextures[0]!];
      this.uploadedPlanes = [this.uploadedPlanes[1], this.uploadedPlanes[0]];
    }
    this.uploadPlane(gl, 0, planeA);
    if (planeB) this.uploadPlane(gl, 1, planeB);
    this.hasFrame = true;
    this.map?.triggerRepaint();
  }

  private uploadPlane(gl: WebGL2RenderingContext, slot: 0 | 1, plane: Uint8Array): void {
    if (this.uploadedPlanes[slot] === plane) return;
    gl.bindTexture(gl.TEXTURE_2D, this.dataTextures![slot]!);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, this.width, this.height, 0, gl.RED, gl.UNSIGNED_BYTE, plane);
    this.uploadedPlanes[slot] = plane;
  }

  render(gl: WebGLRenderingContext | WebGL2RenderingContext, args: unknown): void {
    if (!(gl instanceof WebGL2RenderingContext)) return;
    if (!this.visible || !this.program || !this.dataTextures || !this.hasFrame || !this.pendingPalette) return;
    const matrix = extractMatrix(args);
    if (!matrix) return;

    gl.useProgram(this.program);
    gl.uniformMatrix4fv(this.uniforms.u_matrix!, false, matrix);
    gl.uniform2f(this.uniforms.u_first!, this.firstLongitude, this.firstLatitude);
    gl.uniform2f(this.uniforms.u_step!, this.longitudeStep, this.latitudeStep);
    gl.uniform2f(this.uniforms.u_size!, this.width, this.height);
    gl.uniform1f(this.uniforms.u_mix!, this.uploadedPlanes[1] ? this.mixWeight : 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.dataTextures[0]!);
    gl.uniform1i(this.uniforms.u_data!, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.dataTextures[1]!);
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
