/**
 * Pixel-level tests for the forecast layer's fragment shader.
 *
 * The shader decides, per pixel, whether there is data to paint. Getting that
 * wrong by one texel shows up as a hairline gap down the map — the kind of
 * thing every application-level test walks straight past and a viewer spots
 * immediately. So this compiles the real shader in a real WebGL2 context,
 * draws one strip of the world, and reads the pixels back.
 *
 * The grid here is a coarse 8 x 4 stand-in for a production one: what matters
 * is that its columns cover the full 360 degrees, which is what puts the last
 * half-cell before the antimeridian past u = 1.
 */

import { expect, test } from "@playwright/test";

import { FRAGMENT_SHADER, VERTEX_SHADER } from "../../web/src/layer";

const GRID_WIDTH = 8;
const GRID_HEIGHT = 4;

/** Alpha of every pixel across one horizontal strip of a world copy. */
async function renderStripAlpha(
  page: import("@playwright/test").Page,
  options: {
    wrap: boolean;
    cover: [number, number, number, number];
    /** Longitude of the first column, and degrees per column. Defaults to a
     * global grid: -180 and a full turn spread over its columns. */
    firstLongitude?: number;
    longitudeStep?: number;
  },
): Promise<number[]> {
  return page.evaluate(
    ({ vertexSource, fragmentSource, width, height, wrap, cover, firstLongitude, longitudeStep }) => {
      const canvas = document.createElement("canvas");
      canvas.width = 256;
      canvas.height = 16;
      const gl = canvas.getContext("webgl2", { antialias: false, preserveDrawingBuffer: true });
      if (!gl) throw new Error("no WebGL2 context");

      const program = gl.createProgram()!;
      for (const [kind, source] of [
        [gl.VERTEX_SHADER, vertexSource],
        [gl.FRAGMENT_SHADER, fragmentSource],
      ] as const) {
        const shader = gl.createShader(kind)!;
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
          throw new Error(`compile failed: ${gl.getShaderInfoLog(shader)}`);
        }
        gl.attachShader(program, shader);
      }
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(`link failed: ${gl.getProgramInfoLog(program)}`);
      }
      gl.useProgram(program);

      // One world copy across the canvas: v_mercator spans [0, 1] on both
      // axes, which is the whole Mercator square.
      const quad = new Float32Array([0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1]);
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
      const position = gl.getAttribLocation(program, "a_position");
      gl.enableVertexAttribArray(position);
      gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

      const uniform = (name: string) => gl.getUniformLocation(program, name);
      // v_mercator is passed through untouched, so an identity matrix maps the
      // quad's [0, 1] x range onto clip space [-1, 1].
      gl.uniformMatrix4fv(uniform("u_matrix"), false, [2, 0, 0, 0, 0, 2, 0, 0, 0, 0, 1, 0, -1, -1, 0, 1]);
      gl.uniform2f(uniform("u_first"), firstLongitude, 90);
      gl.uniform2f(uniform("u_step"), longitudeStep, -180 / (height - 1));
      gl.uniform2f(uniform("u_size"), width, height);
      gl.uniform1f(uniform("u_mix"), 0);
      gl.uniform1f(uniform("u_wrap"), wrap ? 1 : 0);
      gl.uniform4f(uniform("u_cover"), cover[0], cover[1], cover[2], cover[3]);

      // A data plane of mid codes, and a palette that is opaque everywhere, so
      // a transparent pixel can only mean the shader discarded it.
      const data = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, data);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap ? gl.REPEAT : gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      const plane = new Uint8Array(width * height).fill(128);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, width, height, 0, gl.RED, gl.UNSIGNED_BYTE, plane);
      gl.uniform1i(uniform("u_data"), 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, data);
      gl.uniform1i(uniform("u_data_b"), 1);

      const palette = gl.createTexture();
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, palette);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      const colors = new Uint8Array(256 * 4).fill(255);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, colors);
      gl.uniform1i(uniform("u_palette"), 2);

      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      const pixels = new Uint8Array(canvas.width * 4);
      // One row through the middle of the band.
      gl.readPixels(0, canvas.height / 2, canvas.width, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      return Array.from({ length: canvas.width }, (_, x) => pixels[x * 4 + 3]!);
    },
    {
      vertexSource: VERTEX_SHADER,
      fragmentSource: FRAGMENT_SHADER,
      width: GRID_WIDTH,
      height: GRID_HEIGHT,
      wrap: options.wrap,
      cover: options.cover,
      firstLongitude: options.firstLongitude ?? -180,
      longitudeStep: options.longitudeStep ?? 360 / GRID_WIDTH,
    },
  );
}

test.beforeEach(async ({ page }) => {
  await page.goto("about:blank");
});

test("a global grid paints every longitude, the antimeridian included", async ({ page }) => {
  // The half-cell before 180 degrees lands past u = 1 — the cell center is
  // half a step east of a full turn — and the texture's REPEAT resolves it.
  // A coverage test on the raw u would discard exactly that sliver, leaving a
  // hairline gap down the dateline.
  const alpha = await renderStripAlpha(page, { wrap: true, cover: [0, 1, 0, 1] });
  expect(alpha.filter((value) => value === 0)).toEqual([]);
});

test("a coverage box clips to itself and still wraps at the antimeridian", async ({ page }) => {
  // The eastern quarter of the world plus the western eighth: the box the two
  // rectangles of a viewport straddling the dateline collapse to.
  const alpha = await renderStripAlpha(page, { wrap: true, cover: [0.75, 0.125, 0, 1] });
  const painted = alpha.map((value) => value > 0);
  // Painted at both edges of the strip, blank in the middle.
  expect(painted[0]).toBe(true);
  expect(painted[alpha.length - 1]).toBe(true);
  expect(painted[Math.floor(alpha.length / 2)]).toBe(false);
  // One run of blank pixels, not two: the box is contiguous across the seam.
  const runs = painted.filter((value, index) => index > 0 && value !== painted[index - 1]).length;
  expect(runs).toBe(2);
});

test("a cropped grid paints only its own window", async ({ page }) => {
  // Eight columns of five degrees from 100 E: a showcase-shaped window over
  // one region, and nothing outside it has data to show. The wrapped
  // coordinate must not sneak those longitudes back in.
  const alpha = await renderStripAlpha(page, {
    wrap: false,
    cover: [0, 1, 0, 1],
    firstLongitude: 100,
    longitudeStep: 5,
  });
  const painted = alpha.map((value) => value > 0);
  // The window is 40 of 360 degrees, a ninth of the strip, and contiguous.
  const count = painted.filter(Boolean).length;
  expect(count).toBeGreaterThan(alpha.length / 12);
  expect(count).toBeLessThan(alpha.length / 6);
  const runs = painted.filter((value, index) => index > 0 && value !== painted[index - 1]).length;
  expect(runs).toBe(2);
});
